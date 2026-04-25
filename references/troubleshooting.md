# Troubleshooting

## No nodes discovered (`DISCOVERY_TIMEOUT`)
- Confirm Unreal Editor is running.
- Confirm Python plugin remote execution is enabled:
  - Editor Settings: Plugins > Python > Enable Remote Execution?
- Confirm multicast settings match harness flags:
  - Group endpoint default: `239.0.0.1:6766`
  - Bind address default: `127.0.0.1`
- If the editor changed bind settings, pass matching values to the harness.

## Multiple matching nodes (`AMBIGUOUS_TARGET`)
- Use `--target-node-id` from `discover` output.
- Or narrow with `--target-project`, `--target-machine`, `--target-user`.

## Remote script failed (`REMOTE_EXCEPTION`)
- Inspect `execution.command_result` for traceback.
- Inspect `execution.logs` for warnings/errors emitted before failure.

## Transport/protocol errors
- Retry once.
- If persistent, ensure editor and harness are both on same host and loopback is available.
- Avoid changing `--command-ip` unless needed; keep `127.0.0.1`.

## Large output concerns
- This harness includes robust response handling for payloads larger than 8192 bytes.
- If output is still too large, reduce script verbosity.
