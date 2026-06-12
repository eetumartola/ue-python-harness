"""CLI harness for executing Python in a running UE 5.4 editor instance."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from vendor.remote_execution_ue54 import (  # noqa: E402
    MODE_EVAL_STATEMENT,
    MODE_EXEC_FILE,
    MODE_EXEC_STATEMENT,
    DiscoveryTimeoutError,
    OperationTimeoutError,
    ProtocolError,
    RemoteExecutionClient,
    RemoteExecutionConfig,
    RemoteExecutionError,
    TransportError,
)

EXIT_SUCCESS = 0
EXIT_DISCOVERY_FAILURE = 2
EXIT_TARGET_SELECTION_FAILURE = 3
EXIT_REMOTE_EXCEPTION = 4
EXIT_TRANSPORT_PROTOCOL_FAILURE = 5
EXIT_TIMEOUT = 6
EXIT_INVALID_ARGS = 7

ERROR_DISCOVERY_TIMEOUT = "DISCOVERY_TIMEOUT"
ERROR_NO_MATCHING_NODE = "NO_MATCHING_NODE"
ERROR_AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
ERROR_TRANSPORT_IO = "TRANSPORT_IO"
ERROR_INVALID_RESPONSE = "INVALID_RESPONSE"
ERROR_TIMEOUT = "TIMEOUT"
ERROR_REMOTE_EXCEPTION = "REMOTE_EXCEPTION"
ERROR_INVALID_ARGS = "INVALID_ARGUMENTS"

DEFAULT_RUN_DISCOVERY_SETTLE_SEC = 0.2


class HarnessArgumentError(Exception):
    """Raised for command-line argument parse errors."""


class HarnessArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # noqa: D401
        raise HarnessArgumentError(message)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _duration_ms(start_monotonic: float) -> int:
    return int((time.monotonic() - start_monotonic) * 1000)


def _remaining_timeout_sec(deadline_monotonic: float) -> float:
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise OperationTimeoutError("Timed out before Unreal Python command could be sent")
    return remaining


def _parse_endpoint(value: str, arg_name: str) -> Tuple[str, int]:
    parts = value.rsplit(":", 1)
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"{arg_name} must be in ip:port format")

    host = parts[0].strip()
    if not host:
        raise argparse.ArgumentTypeError(f"{arg_name} host cannot be empty")

    try:
        port = int(parts[1].strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{arg_name} port must be an integer") from exc

    if port < 0 or port > 65535:
        raise argparse.ArgumentTypeError(f"{arg_name} port must be between 0 and 65535")

    return host, port


def _build_config(args: argparse.Namespace) -> RemoteExecutionConfig:
    multicast_group = _parse_endpoint(args.multicast_group, "--multicast-group")
    command_endpoint = (args.command_ip, int(args.command_port))

    return RemoteExecutionConfig(
        multicast_group=multicast_group,
        bind_address=args.bind_address,
        multicast_ttl=int(args.multicast_ttl),
        command_endpoint=command_endpoint,
    )


def _normalize_nodes(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for node in nodes:
        out.append(
            {
                "node_id": node.get("node_id"),
                "project_name": node.get("project_name"),
                "project_root": node.get("project_root"),
                "machine": node.get("machine"),
                "user": node.get("user"),
                "engine_version": node.get("engine_version"),
                "engine_root": node.get("engine_root"),
            }
        )
    return out


def _select_target(nodes: List[Dict[str, Any]], args: argparse.Namespace) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    candidates = list(nodes)

    if args.target_node_id:
        candidates = [n for n in candidates if n.get("node_id") == args.target_node_id]

    if args.target_project:
        candidates = [n for n in candidates if n.get("project_name") == args.target_project]

    if args.target_machine:
        candidates = [n for n in candidates if n.get("machine") == args.target_machine]

    if args.target_user:
        candidates = [n for n in candidates if n.get("user") == args.target_user]

    candidates = sorted(candidates, key=lambda n: str(n.get("node_id", "")))

    if not candidates:
        return None, {
            "code": ERROR_NO_MATCHING_NODE,
            "message": "No Unreal node matched the provided target filters",
            "details": {
                "filters": {
                    "target_node_id": args.target_node_id,
                    "target_project": args.target_project,
                    "target_machine": args.target_machine,
                    "target_user": args.target_user,
                },
                "discovered_count": len(nodes),
            },
        }

    if len(candidates) > 1 and not args.allow_multiple:
        return None, {
            "code": ERROR_AMBIGUOUS_TARGET,
            "message": "Multiple Unreal nodes matched target filters; refine filters or pass --allow-multiple",
            "details": {
                "matches": _normalize_nodes(candidates),
            },
        }

    return candidates[0], None


def _normalize_execution_data(raw_data: Dict[str, Any]) -> Dict[str, Any]:
    logs = []
    for entry in raw_data.get("output", []) or []:
        log_type = entry.get("type") if isinstance(entry, dict) else None
        log_message = entry.get("output") if isinstance(entry, dict) else None
        logs.append(
            {
                "type": str(log_type or "Info"),
                "message": str(log_message or ""),
            }
        )

    log_text = "\n".join(f"[{entry['type']}] {entry['message']}" for entry in logs)

    return {
        "success": bool(raw_data.get("success", False)),
        "command_result": str(raw_data.get("result", "")),
        "logs": logs,
        "log_text": log_text,
    }


def _emit_json(payload: Dict[str, Any], pretty: bool) -> None:
    if pretty:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def _make_result(
    *,
    ok: bool,
    phase: str,
    started_unix_ms: int,
    duration_ms: int,
    target: Optional[Dict[str, Any]] = None,
    nodes: Optional[List[Dict[str, Any]]] = None,
    execution: Optional[Dict[str, Any]] = None,
    error: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "ok": ok,
        "phase": phase,
        "target": target,
        "nodes": nodes,
        "execution": execution,
        "meta": {
            "started_unix_ms": started_unix_ms,
            "duration_ms": duration_ms,
            "transport": "ue_python_remote_execution",
            "protocol_version": 1,
        },
        "error": error,
    }
    return result


def _run_discover(args: argparse.Namespace) -> Tuple[int, Dict[str, Any]]:
    started_unix_ms = _now_ms()
    started_mono = time.monotonic()

    try:
        client = RemoteExecutionClient(_build_config(args))
        nodes = client.discover(timeout_sec=float(args.timeout_sec))
        if not nodes:
            raise DiscoveryTimeoutError("No UE nodes discovered")

        return (
            EXIT_SUCCESS,
            _make_result(
                ok=True,
                phase="discover",
                started_unix_ms=started_unix_ms,
                duration_ms=_duration_ms(started_mono),
                nodes=_normalize_nodes(nodes),
                error=None,
            ),
        )
    except DiscoveryTimeoutError as exc:
        return (
            EXIT_DISCOVERY_FAILURE,
            _make_result(
                ok=False,
                phase="discover",
                started_unix_ms=started_unix_ms,
                duration_ms=_duration_ms(started_mono),
                nodes=[],
                error={"code": ERROR_DISCOVERY_TIMEOUT, "message": str(exc)},
            ),
        )
    except OperationTimeoutError as exc:
        return (
            EXIT_TIMEOUT,
            _make_result(
                ok=False,
                phase="discover",
                started_unix_ms=started_unix_ms,
                duration_ms=_duration_ms(started_mono),
                nodes=[],
                error={"code": ERROR_TIMEOUT, "message": str(exc)},
            ),
        )
    except TransportError as exc:
        return (
            EXIT_TRANSPORT_PROTOCOL_FAILURE,
            _make_result(
                ok=False,
                phase="discover",
                started_unix_ms=started_unix_ms,
                duration_ms=_duration_ms(started_mono),
                nodes=[],
                error={"code": ERROR_TRANSPORT_IO, "message": str(exc)},
            ),
        )
    except ProtocolError as exc:
        return (
            EXIT_TRANSPORT_PROTOCOL_FAILURE,
            _make_result(
                ok=False,
                phase="discover",
                started_unix_ms=started_unix_ms,
                duration_ms=_duration_ms(started_mono),
                nodes=[],
                error={"code": ERROR_INVALID_RESPONSE, "message": str(exc)},
            ),
        )
    except RemoteExecutionError as exc:
        return (
            EXIT_TRANSPORT_PROTOCOL_FAILURE,
            _make_result(
                ok=False,
                phase="discover",
                started_unix_ms=started_unix_ms,
                duration_ms=_duration_ms(started_mono),
                nodes=[],
                error={"code": ERROR_TRANSPORT_IO, "message": str(exc)},
            ),
        )


def _read_script_file(path_value: str) -> str:
    path = Path(path_value)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Script file not found: {path}")
    return path.read_text(encoding="utf-8")


def _run_execute(args: argparse.Namespace, command_text: str) -> Tuple[int, Dict[str, Any]]:
    started_unix_ms = _now_ms()
    started_mono = time.monotonic()
    deadline_mono = started_mono + float(args.timeout_sec)

    try:
        client = RemoteExecutionClient(_build_config(args))
        nodes = client.discover(
            timeout_sec=float(args.timeout_sec),
            settle_sec=DEFAULT_RUN_DISCOVERY_SETTLE_SEC,
        )
        if not nodes:
            raise DiscoveryTimeoutError("No UE nodes discovered")
        target_node, select_error = _select_target(nodes, args)

        if select_error is not None:
            return (
                EXIT_TARGET_SELECTION_FAILURE,
                _make_result(
                    ok=False,
                    phase="run",
                    started_unix_ms=started_unix_ms,
                    duration_ms=_duration_ms(started_mono),
                    nodes=_normalize_nodes(nodes),
                    error=select_error,
                ),
            )

        assert target_node is not None

        exec_mode = args.exec_mode
        unattended = not args.attended

        raw_data = client.run_command(
            remote_node_id=str(target_node["node_id"]),
            command=command_text,
            timeout_sec=_remaining_timeout_sec(deadline_mono),
            unattended=unattended,
            exec_mode=exec_mode,
        )
        execution = _normalize_execution_data(raw_data)

        target = {
            "node_id": target_node.get("node_id"),
            "project_name": target_node.get("project_name"),
            "machine": target_node.get("machine"),
            "user": target_node.get("user"),
            "engine_version": target_node.get("engine_version"),
        }

        if not execution["success"]:
            return (
                EXIT_REMOTE_EXCEPTION,
                _make_result(
                    ok=False,
                    phase="run",
                    started_unix_ms=started_unix_ms,
                    duration_ms=_duration_ms(started_mono),
                    target=target,
                    nodes=_normalize_nodes(nodes),
                    execution=execution,
                    error={
                        "code": ERROR_REMOTE_EXCEPTION,
                        "message": "Remote Unreal Python command failed",
                    },
                ),
            )

        return (
            EXIT_SUCCESS,
            _make_result(
                ok=True,
                phase="run",
                started_unix_ms=started_unix_ms,
                duration_ms=_duration_ms(started_mono),
                target=target,
                nodes=_normalize_nodes(nodes),
                execution=execution,
                error=None,
            ),
        )

    except DiscoveryTimeoutError as exc:
        return (
            EXIT_DISCOVERY_FAILURE,
            _make_result(
                ok=False,
                phase="run",
                started_unix_ms=started_unix_ms,
                duration_ms=_duration_ms(started_mono),
                error={"code": ERROR_DISCOVERY_TIMEOUT, "message": str(exc)},
            ),
        )
    except OperationTimeoutError as exc:
        return (
            EXIT_TIMEOUT,
            _make_result(
                ok=False,
                phase="run",
                started_unix_ms=started_unix_ms,
                duration_ms=_duration_ms(started_mono),
                error={"code": ERROR_TIMEOUT, "message": str(exc)},
            ),
        )
    except TransportError as exc:
        return (
            EXIT_TRANSPORT_PROTOCOL_FAILURE,
            _make_result(
                ok=False,
                phase="run",
                started_unix_ms=started_unix_ms,
                duration_ms=_duration_ms(started_mono),
                error={"code": ERROR_TRANSPORT_IO, "message": str(exc)},
            ),
        )
    except ProtocolError as exc:
        return (
            EXIT_TRANSPORT_PROTOCOL_FAILURE,
            _make_result(
                ok=False,
                phase="run",
                started_unix_ms=started_unix_ms,
                duration_ms=_duration_ms(started_mono),
                error={"code": ERROR_INVALID_RESPONSE, "message": str(exc)},
            ),
        )
    except FileNotFoundError as exc:
        return (
            EXIT_INVALID_ARGS,
            _make_result(
                ok=False,
                phase="run",
                started_unix_ms=started_unix_ms,
                duration_ms=_duration_ms(started_mono),
                error={"code": ERROR_INVALID_ARGS, "message": str(exc)},
            ),
        )
    except UnicodeDecodeError as exc:
        return (
            EXIT_INVALID_ARGS,
            _make_result(
                ok=False,
                phase="run",
                started_unix_ms=started_unix_ms,
                duration_ms=_duration_ms(started_mono),
                error={
                    "code": ERROR_INVALID_ARGS,
                    "message": f"Failed reading script file as UTF-8: {exc}",
                },
            ),
        )
    except RemoteExecutionError as exc:
        return (
            EXIT_TRANSPORT_PROTOCOL_FAILURE,
            _make_result(
                ok=False,
                phase="run",
                started_unix_ms=started_unix_ms,
                duration_ms=_duration_ms(started_mono),
                error={"code": ERROR_TRANSPORT_IO, "message": str(exc)},
            ),
        )


def _build_parser() -> argparse.ArgumentParser:
    common_root = argparse.ArgumentParser(add_help=False)
    common_root.add_argument("--json-pretty", action="store_true", help="Pretty-print JSON output")
    common_root.add_argument("--timeout-sec", type=float, default=10.0, help="Overall timeout for discovery/run")
    common_root.add_argument(
        "--multicast-group",
        default="239.0.0.1:6766",
        help="Remote execution multicast group endpoint (ip:port)",
    )
    common_root.add_argument("--bind-address", default="127.0.0.1", help="Local UDP bind address")
    common_root.add_argument("--multicast-ttl", type=int, default=0, help="Multicast TTL")
    common_root.add_argument("--command-ip", default="127.0.0.1", help="TCP listen IP for command channel")
    common_root.add_argument("--command-port", type=int, default=0, help="TCP listen port for command channel (0=ephemeral)")

    common_sub = argparse.ArgumentParser(add_help=False)
    common_sub.add_argument("--json-pretty", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common_sub.add_argument("--timeout-sec", type=float, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common_sub.add_argument("--multicast-group", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common_sub.add_argument("--bind-address", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common_sub.add_argument("--multicast-ttl", type=int, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common_sub.add_argument("--command-ip", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common_sub.add_argument("--command-port", type=int, default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    parser = HarnessArgumentParser(
        prog="ue_py_harness",
        description="Run Python in a live UE 5.4 editor and return structured output",
        parents=[common_root],
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("discover", parents=[common_sub], help="Discover available UE remote Python nodes")

    run_common = argparse.ArgumentParser(add_help=False)
    run_common.add_argument("--target-node-id")
    run_common.add_argument("--target-project")
    run_common.add_argument("--target-machine")
    run_common.add_argument("--target-user")
    run_common.add_argument("--allow-multiple", action="store_true")
    run_common.add_argument("--attended", action="store_true", help="Disable unattended execution")
    run_common.add_argument(
        "--exec-mode",
        default=MODE_EXEC_FILE,
        choices=[MODE_EXEC_FILE, MODE_EXEC_STATEMENT, MODE_EVAL_STATEMENT],
        help="UE Python command execution mode",
    )

    run_file = sub.add_parser(
        "run-file",
        parents=[common_sub, run_common],
        help="Run local Python file content remotely",
    )
    run_file.add_argument("script_path", help="Path to local Python script file")

    run_code = sub.add_parser(
        "run-code",
        parents=[common_sub, run_common],
        help="Run inline Python code remotely",
    )
    run_code.add_argument("--code", required=True, help="Python code to execute remotely")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()

    try:
        args = parser.parse_args(argv)
    except HarnessArgumentError as exc:
        payload = _make_result(
            ok=False,
            phase="parse",
            started_unix_ms=_now_ms(),
            duration_ms=0,
            error={"code": ERROR_INVALID_ARGS, "message": str(exc)},
        )
        _emit_json(payload, pretty=False)
        return EXIT_INVALID_ARGS

    if args.timeout_sec <= 0:
        payload = _make_result(
            ok=False,
            phase="parse",
            started_unix_ms=_now_ms(),
            duration_ms=0,
            error={"code": ERROR_INVALID_ARGS, "message": "--timeout-sec must be > 0"},
        )
        _emit_json(payload, pretty=args.json_pretty)
        return EXIT_INVALID_ARGS

    if args.command_port < 0 or args.command_port > 65535:
        payload = _make_result(
            ok=False,
            phase="parse",
            started_unix_ms=_now_ms(),
            duration_ms=0,
            error={"code": ERROR_INVALID_ARGS, "message": "--command-port must be between 0 and 65535"},
        )
        _emit_json(payload, pretty=args.json_pretty)
        return EXIT_INVALID_ARGS

    if args.command == "discover":
        exit_code, payload = _run_discover(args)
        _emit_json(payload, pretty=args.json_pretty)
        return exit_code

    if args.command == "run-file":
        try:
            command_text = _read_script_file(args.script_path)
        except Exception as exc:  # noqa: BLE001
            payload = _make_result(
                ok=False,
                phase="run",
                started_unix_ms=_now_ms(),
                duration_ms=0,
                error={"code": ERROR_INVALID_ARGS, "message": str(exc)},
            )
            _emit_json(payload, pretty=args.json_pretty)
            return EXIT_INVALID_ARGS

        exit_code, payload = _run_execute(args, command_text)
        _emit_json(payload, pretty=args.json_pretty)
        return exit_code

    if args.command == "run-code":
        exit_code, payload = _run_execute(args, args.code)
        _emit_json(payload, pretty=args.json_pretty)
        return exit_code

    payload = _make_result(
        ok=False,
        phase="parse",
        started_unix_ms=_now_ms(),
        duration_ms=0,
        error={"code": ERROR_INVALID_ARGS, "message": f"Unsupported command: {args.command}"},
    )
    _emit_json(payload, pretty=args.json_pretty)
    return EXIT_INVALID_ARGS


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
