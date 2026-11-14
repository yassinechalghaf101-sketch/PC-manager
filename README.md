# deepcode

A small autonomous coding agent for Windows. Python standard library only.
It runs a tool loop against a local Ollama model by default, and can point at
any OpenAI-compatible API (DeepSeek, OpenRouter, LM Studio).

No web UI, no TUI, no pip dependencies.

## Requirements

- Python 3.10+
- Ollama running at `http://127.0.0.1:11434`
- A tool-capable model, default `qwen3.5:4b`

```powershell
ollama pull qwen3.5:4b
```

## Run

```powershell
cd C:\path\to\project
python C:\Users\zarzi\OneDrive\Bureau\opencode\Projects\deepcode\deepcode.py

# or pick a workspace explicitly
python deepcode.py --workspace C:\path\to\project
```

Type a request at `YOU ›`. `/new` starts a fresh conversation, `/resume` is a
launch flag, `/exit` quits, Ctrl+C aborts the current generation.

## Example

```text
YOU › Fix buggy.py so test_buggy.py passes. Run the test to verify.

  STEP 01   4.0s · 14 tok · 16.2/s
  LIST     .
  │ ...buggy.py  (32 bytes)
  │ ...test_buggy.py  (101 bytes)

  STEP 04   12s · 144 tok · 12.3/s
  SAY
  │ add() subtracts instead of adding.
  EDIT     ...\buggy.py
  │ -     return a - b
  │ +     return a + b

  STEP 06   4.0s · 48 tok · 14.4/s
  SHELL    python ...\test_buggy.py
  │ exit 0 · 1 line
  │ PASS

  ✓ DONE    36s · 7 steps · 385 tokens · 13.6 tok/s
    The test passes.
```

## Tools

| Tool | What it does |
|---|---|
| `shell` | PowerShell 5.1, full disk, optional `workdir` and `timeout` |
| `read_file` | line-numbered reads with `offset`/`limit` |
| `write_file` | create or overwrite, parents created |
| `edit_file` | exact replacement, optional `replace_all`, shown as a diff |
| `list_dir` | directory listing, optional recursive |
| `search_files` | case-sensitive regex content search with glob filter |
| `update_todo` | checklist for multi-step work |

A reply with no tool calls ends the turn. There is no `finish()` requirement.

`AGENTS.md` in the workspace is loaded into the system prompt automatically.
The real Desktop path is resolved through the registry, so "put it on my
desktop" works even when OneDrive has redirected known folders.

## Performance profile

Defaults are tuned for a laptop with limited VRAM:

| Flag | Default | Why |
|---|---|---|
| `--threads` | 8 | leaves CPU headroom; biggest heat lever |
| `--ctx` | 4096 | smaller KV cache and prompt |
| `--max-predict` | 1024 | caps runaway generations |
| `--max-minutes` | 20 | hard wall-clock stop per request; 0 disables |
| `--max-steps` | 60 | bounds total model turns |

The system prompt is static and the conversation is append-only, so Ollama can
reuse its prompt cache. History is trimmed before each turn from `--ctx` and
`--max-predict`, leaving room for the answer; raise `--ctx` to keep more
history. Tool output is capped at 6,000 characters (3,072 at the default 4k
context).

`--doctor` measures the real model once and prints load time, generation speed,
and the GPU/CPU split:

```text
BACKEND    ollama
MODEL      qwen3.5:4b
LOAD       0.01s
GEN        17.0 tok/s (16 tokens)
SETTINGS   ctx 4096 · 8 threads · max predict 1024
MEMORY     3.7 GB · GPU 50% · CPU 50%
! WARN     CPU fallback is active; expect heat.
```

On a 4 GB GPU, a 3.7 GB model is split across CPU and GPU. No harness setting
changes that. If it still runs hot, lower `--threads 6` (or 4), use a smaller
model such as `qwen3.5:2b`, or use a remote API.

## Remote API

The OpenAI-compatible path is selected automatically when `--api` ends in `/v1`:

```powershell
python deepcode.py `
  --api https://api.deepseek.com/v1 `
  --model deepseek-chat `
  --key $env:DEEPSEEK_API_KEY
```

`--key` can also come from `DEEPCODE_API_KEY`, `DEEPSEEK_API_KEY`, or
`OPENAI_API_KEY`. Remote calls are currently non-streaming; local Ollama calls
stream so Ctrl+C stops generation immediately.

## Sessions and resume

Every message and tool call is appended to `sessions/*.jsonl`.

```powershell
python deepcode.py --resume            # latest session
python deepcode.py --resume sessions\2026-11-13T22-00-01.jsonl
```

The workspace and model are taken from the current launch; the conversation is
restored. An interrupted tool-call tail is dropped on load, and `--resume`
without a path skips empty session files left behind by `/new`.

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--model` | `qwen3.5:4b` | Ollama model or API model |
| `--api` | `http://127.0.0.1:11434` | Native Ollama URL, or OpenAI base ending in `/v1` |
| `--key` | env | API key for remote backends |
| `--backend` | `auto` | `auto`, `ollama`, `openai` |
| `--workspace` | current dir | Base for relative paths |
| `--max-steps` | 60 | Model turns per request |
| `--max-minutes` | 20 | Wall-clock cap per request |
| `--threads` | 8 | Ollama CPU threads, 0 = auto |
| `--ctx` | 4096 | Context window in tokens |
| `--max-predict` | 1024 | Max generated tokens per call |
| `--temperature` | 0.2 | Sampling temperature |
| `--color` | `auto` | `auto`, `always`, `never` |
| `--session-dir` | `./sessions` | JSONL logs |
| `--resume` | off | Latest session or a log path |
| `--doctor` | off | Measure model speed and GPU split |
| `--self-check` | off | Local sanity checks, no model |

## Safety

- **No path clamp.** File tools reach the whole disk. Shell runs as you.
- **No confirmations.** The model executes what it emits.
- **No undo.** Overwrites and deletes are permanent. Use git for checkpoints.
- **Session logs are not backups.**
- Standard library only; no network calls except the configured model API.

## Self-check

```powershell
python deepcode.py --self-check
```

Checks path resolution, history trimming, tool schemas, OpenAI/Ollama message
conversion, streaming chunk merging, todo validation, colors, and the spinner.
