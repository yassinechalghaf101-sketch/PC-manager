# pc-manager

Autonomous Windows PC manager driven by a local Ollama model (`qwen3.5:4b` by default).
The model runs a ReAct loop: reason, call one tool, observe the result, repeat until
`finish()`. No pip dependencies; Python 3.10+.

## Setup

```powershell
ollama pull qwen3.5:4b
```

## Run

```powershell
python pc_manager.py
python pc_manager.py --self-check
python pc_manager.py --model qwen3.5:9b --max-steps 60 --think
```

Banner shows the model, allowed root, session time, and session log path. Type a task;
`/exit` quits; Ctrl+C aborts the current task and returns to the prompt.

## Look

```text
  ██████╗  ██████╗    M A N A G E R
  ██╔══██╗██╔════╝    AUTONOMOUS DESKTOP AGENT
  ██████╔╝██║         ────────────────────────────────────
  ██╔═══╝ ██║         MODEL      qwen3.5:4b
  ██║     ╚██████╗    ROOT       C:\Users\you
  ╚═╝      ╚═════╝    SESSION    2026-11-11 · 21:54
                      LOG        …\sessions\2026-11-11T21-54-04.jsonl

  Type a request.  /exit quits.  Ctrl+C aborts.

YOU › list the 5 biggest files in Downloads

    STEP 01                                            2.3s
    SHELL   Get-ChildItem -Recurse ...                 0.41s
    │ exit 0 · 5 lines
    │ FullName                            Length
    │ ...

    STEP 02                                            1.1s
    EDIT    notes.txt                                  0.02s
    │ - beta
    │ + gamma

  ──────────────────────────────────────────────────
  ✓ DONE    8.4s · 2 steps · 812 tokens
    Listed the 5 largest files. Nothing changed.
```

Steps and tools show durations. `update_todo` renders as a checklist and `edit_file`
shows a capped `- old` / `+ new` diff. Tool output is indented, ANSI-stripped, capped
at 15 lines, and points to the session log for the rest. A spinner runs during model
calls on a real terminal. Colors switch off automatically when output is piped, so
logs and tests stay plain; `--color always` forces them on.

| Flag | Default | Meaning |
|---|---|---|
| `--model` | `qwen3.5:4b` | Any Ollama model with tool support |
| `--api` | `http://127.0.0.1:11434` | Ollama server |
| `--root` | user home | Boundary for file tools only |
| `--max-steps` | 40 | Model turns per task |
| `--think` | off | Enable model reasoning (slower) |
| `--color` | `auto` | `auto`, `always`, or `never`; `NO_COLOR` is respected |
| `--session-dir` | `./sessions` | JSONL logs |

## Tools

`shell` (PowerShell, any command), `list_dir`, `read_file`, `write_file`,
`edit_file`, `search_files`, `update_todo`, `finish`.

## Safety model

- No confirmation prompts. The model executes what it emits, as your user.
- Desktop, Documents, Downloads, and Pictures are resolved with the Windows
  known-folder API and passed to the model verbatim, so OneDrive-redirected
  folders are used instead of a guessed `<home>\Desktop`.
- File tools are clamped to `--root`; `shell` is not clamped and can reach anything
  your account can. Edits and deletes are permanent.
- Session logs record every task, tool call, and result, but they are not a backup.
- No elevation: admin-only commands fail unless the harness itself runs as admin.

## Adding or removing a feature

1. Write `tool_your_name(h, args)` above `TOOL_FUNCS`.
2. Add it to `TOOL_FUNCS` and add its JSON schema to `TOOL_SCHEMAS`.
3. Update `CAPABILITIES` / `LIMITATIONS` at the top of `pc_manager.py`.
4. Optionally tune the constants at the top (timeouts, caps, history window).
