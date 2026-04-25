---
name: ue-python-harness
description: Execute Python scripts in a running Unreal Engine 5.4 editor instance and return structured per-command output logs/results for LLM workflows. Use when a task requires running or validating Unreal Python code in a live editor without manual log copy-paste.
---

# UE Python Harness Skill

Use this skill to run Unreal Python code remotely and capture command-scoped output.
The skill is self-contained: runtime scripts and vendor transport code are in `scripts/`.

## Workflow
1. Discover nodes:
   - `python scripts/run_harness.py discover --timeout-sec 2`
2. Run a script file:
   - `python scripts/run_harness.py run-file --timeout-sec 15 --target-project PaxDei path/to/script.py`
3. Run inline code:
   - `python scripts/run_harness.py run-code --timeout-sec 15 --target-project PaxDei --code "import unreal; unreal.log('hello')"`

Prefer `--target-node-id` when multiple editor instances are running.

## Parsing contract
Read JSON output from stdout and use:
- `ok`
- `execution.success`
- `execution.logs`
- `execution.command_result`
- `error.code` and `error.message`

Treat non-zero process exit code as failure and inspect `error`.

## Target selection rules
Use filters in this order:
1. `--target-node-id`
2. `--target-project`
3. `--target-machine` and/or `--target-user`

If multiple nodes match and `--allow-multiple` is not set, treat it as an error and refine filters.

## Common errors
See `references/troubleshooting.md`.

## Safety
Keep default transport settings (`127.0.0.1`, TTL 0) unless explicitly required otherwise.
