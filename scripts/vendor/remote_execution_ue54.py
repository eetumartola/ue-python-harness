"""UE 5.4 Python remote execution client with robust TCP response handling.

This module implements the protocol used by Unreal's PythonScriptPlugin
(PythonScriptRemoteExecution.cpp).
"""

from __future__ import annotations

import json
import socket
import time
import uuid
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any, Dict, List, Optional, Tuple


PROTOCOL_VERSION = 1
PROTOCOL_MAGIC = "ue_py"

TYPE_PING = "ping"
TYPE_PONG = "pong"
TYPE_OPEN_CONNECTION = "open_connection"
TYPE_CLOSE_CONNECTION = "close_connection"
TYPE_COMMAND = "command"
TYPE_COMMAND_RESULT = "command_result"

MODE_EXEC_FILE = "ExecuteFile"
MODE_EXEC_STATEMENT = "ExecuteStatement"
MODE_EVAL_STATEMENT = "EvaluateStatement"

DEFAULT_MULTICAST_GROUP = ("239.0.0.1", 6766)
DEFAULT_BIND_ADDRESS = "127.0.0.1"
DEFAULT_MULTICAST_TTL = 0
DEFAULT_COMMAND_ENDPOINT = ("127.0.0.1", 0)

DEFAULT_UDP_RECV_BYTES = 65535
DEFAULT_TCP_RECV_CHUNK = 4096
DEFAULT_TCP_MAX_MESSAGE_BYTES = 8 * 1024 * 1024
DEFAULT_PING_INTERVAL_SEC = 1.0
DEFAULT_ACCEPT_RETRY_SEC = 1.0


class RemoteExecutionError(Exception):
    """Base exception for remote execution failures."""


class DiscoveryTimeoutError(RemoteExecutionError):
    """No nodes discovered within timeout."""


class TransportError(RemoteExecutionError):
    """Socket or transport failure."""


class ProtocolError(RemoteExecutionError):
    """Protocol parse/validation failure."""


class OperationTimeoutError(RemoteExecutionError):
    """Operation timed out."""


@dataclass(frozen=True)
class RemoteExecutionConfig:
    multicast_group: Tuple[str, int] = DEFAULT_MULTICAST_GROUP
    bind_address: str = DEFAULT_BIND_ADDRESS
    multicast_ttl: int = DEFAULT_MULTICAST_TTL
    command_endpoint: Tuple[str, int] = DEFAULT_COMMAND_ENDPOINT


@dataclass(frozen=True)
class RemoteMessage:
    type_: str
    source: str
    dest: Optional[str]
    data: Optional[Any]

    def to_json_bytes(self) -> bytes:
        if not self.type_:
            raise ProtocolError("'type' cannot be empty")
        if not self.source:
            raise ProtocolError("'source' cannot be empty")

        payload: Dict[str, Any] = {
            "version": PROTOCOL_VERSION,
            "magic": PROTOCOL_MAGIC,
            "type": self.type_,
            "source": self.source,
        }
        if self.dest:
            payload["dest"] = self.dest
        if self.data is not None:
            payload["data"] = self.data
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def from_json_bytes(raw: bytes) -> "RemoteMessage":
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ProtocolError(f"Failed to decode JSON message: {exc}") from exc
        return RemoteMessage.from_payload(payload)

    @staticmethod
    def from_payload(payload: Dict[str, Any]) -> "RemoteMessage":
        try:
            version = payload["version"]
            magic = payload["magic"]
            msg_type = payload["type"]
            source = payload["source"]
        except KeyError as exc:
            raise ProtocolError(f"Missing required field: {exc}") from exc

        if version != PROTOCOL_VERSION:
            raise ProtocolError(f"Protocol version mismatch: got {version}, expected {PROTOCOL_VERSION}")
        if magic != PROTOCOL_MAGIC:
            raise ProtocolError(f"Protocol magic mismatch: got {magic}, expected {PROTOCOL_MAGIC}")
        if not isinstance(msg_type, str) or not msg_type:
            raise ProtocolError("Invalid 'type' field")
        if not isinstance(source, str) or not source:
            raise ProtocolError("Invalid 'source' field")

        dest = payload.get("dest")
        if dest is not None and not isinstance(dest, str):
            raise ProtocolError("Invalid 'dest' field")

        data = payload.get("data")
        return RemoteMessage(type_=msg_type, source=source, dest=dest, data=data)

    def passes_receive_filter(self, local_node_id: str) -> bool:
        return self.source != local_node_id and (not self.dest or self.dest == local_node_id)


class RemoteExecutionClient:
    """Synchronous UE Python remote execution client."""

    def __init__(self, config: RemoteExecutionConfig):
        self._config = config
        self._local_node_id = str(uuid.uuid4())

    @property
    def local_node_id(self) -> str:
        return self._local_node_id

    def discover(self, timeout_sec: float, settle_sec: Optional[float] = None) -> List[Dict[str, Any]]:
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be > 0")
        if settle_sec is not None and settle_sec < 0:
            raise ValueError("settle_sec must be >= 0")

        nodes: Dict[str, Dict[str, Any]] = {}
        deadline = time.monotonic() + timeout_sec
        settle_deadline: Optional[float] = None
        next_ping = 0.0

        with self._open_udp_socket() as udp_sock:
            while True:
                now = time.monotonic()
                effective_deadline = settle_deadline if settle_deadline is not None else deadline
                if now >= effective_deadline:
                    break

                if now >= next_ping:
                    self._send_udp(
                        udp_sock,
                        RemoteMessage(type_=TYPE_PING, source=self._local_node_id, dest=None, data=None),
                    )
                    next_ping = now + DEFAULT_PING_INTERVAL_SEC

                wait_sec = max(0.0, min(0.2, effective_deadline - now))
                if wait_sec <= 0:
                    break

                try:
                    udp_sock.settimeout(wait_sec)
                    data, _addr = udp_sock.recvfrom(DEFAULT_UDP_RECV_BYTES)
                except socket.timeout:
                    continue
                except OSError as exc:
                    raise TransportError(f"UDP receive failed during discovery: {exc}") from exc

                try:
                    msg = RemoteMessage.from_json_bytes(data)
                except ProtocolError:
                    continue

                if not msg.passes_receive_filter(self._local_node_id):
                    continue
                if msg.type_ != TYPE_PONG:
                    continue
                if not isinstance(msg.data, dict):
                    continue

                node_id = msg.source
                entry = dict(msg.data)
                entry["node_id"] = node_id
                nodes[node_id] = entry
                if settle_sec is not None and settle_deadline is None:
                    settle_deadline = min(deadline, time.monotonic() + settle_sec)

        return sorted(nodes.values(), key=lambda node: str(node.get("node_id", "")))

    def run_command(
        self,
        remote_node_id: str,
        command: str,
        timeout_sec: float,
        unattended: bool = True,
        exec_mode: str = MODE_EXEC_FILE,
    ) -> Dict[str, Any]:
        if not remote_node_id:
            raise ValueError("remote_node_id is required")
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be > 0")
        if exec_mode not in {MODE_EXEC_FILE, MODE_EXEC_STATEMENT, MODE_EVAL_STATEMENT}:
            raise ValueError(f"Invalid exec_mode: {exec_mode}")

        with self._open_udp_socket() as udp_sock:
            listen_sock = self._open_command_listen_socket()
            try:
                command_ip, command_port = listen_sock.getsockname()

                command_channel = self._accept_command_channel(
                    udp_sock=udp_sock,
                    listen_sock=listen_sock,
                    remote_node_id=remote_node_id,
                    timeout_sec=timeout_sec,
                    command_endpoint=(command_ip, int(command_port)),
                )

                try:
                    command_msg = RemoteMessage(
                        type_=TYPE_COMMAND,
                        source=self._local_node_id,
                        dest=remote_node_id,
                        data={
                            "command": command,
                            "unattended": unattended,
                            "exec_mode": exec_mode,
                        },
                    )
                    self._send_tcp(command_channel, command_msg)

                    response_msg = self._recv_tcp_message(
                        command_channel,
                        expected_type=TYPE_COMMAND_RESULT,
                        timeout_sec=timeout_sec,
                        expected_source=remote_node_id,
                    )

                    if not isinstance(response_msg.data, dict):
                        raise ProtocolError("command_result data is not an object")

                    return response_msg.data
                finally:
                    try:
                        command_channel.close()
                    except OSError:
                        pass
            finally:
                try:
                    listen_sock.close()
                except OSError:
                    pass

                try:
                    self._send_udp(
                        udp_sock,
                        RemoteMessage(
                            type_=TYPE_CLOSE_CONNECTION,
                            source=self._local_node_id,
                            dest=remote_node_id,
                            data=None,
                        ),
                    )
                except RemoteExecutionError:
                    # Best-effort close notification.
                    pass

    def _open_udp_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            sock.bind((self._config.bind_address, self._config.multicast_group[1]))

            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, int(self._config.multicast_ttl))
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(self._config.bind_address),
            )
            membership = socket.inet_aton(self._config.multicast_group[0]) + socket.inet_aton(self._config.bind_address)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            sock.settimeout(0.2)
            return sock
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise

    def _send_udp(self, udp_sock: socket.socket, message: RemoteMessage) -> None:
        try:
            udp_sock.sendto(message.to_json_bytes(), self._config.multicast_group)
        except OSError as exc:
            raise TransportError(f"Failed to send UDP message '{message.type_}': {exc}") from exc

    def _open_command_listen_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        try:
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            sock.bind(self._config.command_endpoint)
            sock.listen(1)
            return sock
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise

    def _accept_command_channel(
        self,
        udp_sock: socket.socket,
        listen_sock: socket.socket,
        remote_node_id: str,
        timeout_sec: float,
        command_endpoint: Tuple[str, int],
    ) -> socket.socket:
        deadline = time.monotonic() + timeout_sec

        while True:
            now = time.monotonic()
            if now >= deadline:
                raise OperationTimeoutError("Timed out waiting for UE to connect command channel")

            self._send_udp(
                udp_sock,
                RemoteMessage(
                    type_=TYPE_OPEN_CONNECTION,
                    source=self._local_node_id,
                    dest=remote_node_id,
                    data={
                        "command_ip": command_endpoint[0],
                        "command_port": int(command_endpoint[1]),
                    },
                ),
            )

            remaining = max(0.0, deadline - time.monotonic())
            wait_sec = min(DEFAULT_ACCEPT_RETRY_SEC, remaining)
            if wait_sec <= 0:
                raise OperationTimeoutError("Timed out waiting for UE to connect command channel")

            listen_sock.settimeout(wait_sec)
            try:
                channel, _addr = listen_sock.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                raise TransportError(f"Failed accepting command channel: {exc}") from exc

            channel.setblocking(True)
            return channel

    def _send_tcp(self, channel: socket.socket, message: RemoteMessage) -> None:
        payload = message.to_json_bytes()
        try:
            channel.sendall(payload)
        except OSError as exc:
            raise TransportError(f"Failed to send TCP command message: {exc}") from exc

    def _recv_tcp_message(
        self,
        channel: socket.socket,
        expected_type: str,
        timeout_sec: float,
        expected_source: Optional[str] = None,
    ) -> RemoteMessage:
        deadline = time.monotonic() + timeout_sec
        decoder = json.JSONDecoder()
        buffer = bytearray()

        while True:
            now = time.monotonic()
            if now >= deadline:
                raise OperationTimeoutError("Timed out waiting for TCP command response")

            wait_sec = min(1.0, deadline - now)
            channel.settimeout(wait_sec)

            try:
                chunk = channel.recv(DEFAULT_TCP_RECV_CHUNK)
            except socket.timeout:
                continue
            except OSError as exc:
                raise TransportError(f"Failed receiving TCP command response: {exc}") from exc

            if not chunk:
                raise TransportError("TCP command channel closed before response completed")

            buffer.extend(chunk)
            if len(buffer) > DEFAULT_TCP_MAX_MESSAGE_BYTES:
                raise ProtocolError(
                    f"TCP response exceeds max size ({DEFAULT_TCP_MAX_MESSAGE_BYTES} bytes)"
                )

            try:
                text = buffer.decode("utf-8")
            except UnicodeDecodeError as exc:
                # Treat decode failure at buffer end as potentially incomplete UTF-8 and continue.
                if exc.end == len(buffer):
                    continue
                raise ProtocolError(f"Invalid UTF-8 in TCP response: {exc}") from exc

            try:
                payload, _idx = decoder.raw_decode(text)
            except JSONDecodeError:
                # Most likely partial JSON; continue reading.
                continue

            if not isinstance(payload, dict):
                raise ProtocolError("TCP response is not a JSON object")

            msg = RemoteMessage.from_payload(payload)
            if not msg.passes_receive_filter(self._local_node_id):
                raise ProtocolError("Received message does not match local node filter")
            if msg.type_ != expected_type:
                raise ProtocolError(
                    f"Unexpected response message type '{msg.type_}', expected '{expected_type}'"
                )
            if expected_source is not None and msg.source != expected_source:
                raise ProtocolError(
                    f"Unexpected response source '{msg.source}', expected '{expected_source}'"
                )
            return msg
