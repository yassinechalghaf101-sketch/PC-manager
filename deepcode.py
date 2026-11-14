#!/usr/bin/env python3
"""deepcode - a small autonomous coding agent for Windows.

Python stdlib only. Talks to a local Ollama server by default; any
OpenAI-compatible endpoint (DeepSeek, OpenRouter, LM Studio) also works.
"""

import argparse
import fnmatch
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------
DEFAULT_MODEL = "qwen3.5:4b"
DEFAULT_API = "http://127.0.0.1:11434"

MAX_STEPS = 60
MAX_MINUTES = 20                 # wall-clock cap per user request; 0 disables
MODEL_TIMEOUT = 300              # socket timeout for one model call
MAX_TOOL_CHARS = 6000            # tool output sent back to the model
MAX_TODO_ITEMS = 12
SHELL_TIMEOUT = 120
SHELL_TIMEOUT_MAX = 600
SEARCH_MAX_FILES = 5000
SEARCH_MAX_MATCHES = 100
SEARCH_MAX_BYTES = 1_000_000
LIST_MAX_ENTRIES = 500
READ_MAX_LINES = 300
READ_HARD_MAX = 2000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache",
             "site-packages", ".next", "dist", "build", "target"}

# Cool local profile. A 4B model on a 4 GB laptop GPU is split across CPU/GPU;
# capping threads and output is the only way to keep the CPU from cooking.
DEFAULT_THREADS = 8
DEFAULT_CTX = 4096
DEFAULT_MAX_PREDICT = 1024
DEFAULT_TEMPERATURE = 0.2
NUM_BATCH = 128
KEEP_ALIVE = "2m"


class ToolError(Exception):
    """A tool rejected its input; the message is fed back to the model."""


class TaskTimeout(Exception):
    """The per-request wall-clock budget ran out."""


# --------------------------------------------------------------------------
# Prompt and paths
# --------------------------------------------------------------------------
def read_agents_md(workspace: Path) -> str:
    path = workspace / "AGENTS.md"
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if len(text) > 8000:
        text = text[:8000] + "\n...[truncated]"
    return text


@lru_cache(maxsize=1)
def desktop_path() -> Path | None:
    """The real Desktop after OneDrive redirection; %USERPROFILE%\\Desktop may be stale."""
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "Desktop")
        return Path(os.path.expandvars(value))
    except (ImportError, OSError):
        return None


def build_system_prompt(cfg) -> str:
    parts = [
        "You are deepcode, an autonomous coding agent running on Windows.",
        f"Workspace: {cfg.workspace}",
        f"Model: {cfg.model}",
        f"OS: {platform.system()} {platform.release()}",
        f"Date: {datetime.now().strftime('%Y-%m-%d')}",
        "",
        "You work in a loop: call tools, read the results, and continue until the "
        "request is done. A reply with no tool calls ends your turn and is shown "
        "to the user. Multiple tool calls in one turn are allowed.",
        "",
        "WHAT YOU HAVE",
        "- shell: PowerShell 5.1 as the current user, full disk, can run builds, "
        "tests, git, and install packages.",
        "- list_dir, read_file, write_file, edit_file, search_files: full disk. "
        "Paths may be absolute or relative to the workspace.",
        "- update_todo: a checklist for multi-step work.",
        "",
        "HOW TO WORK",
        "1. Inspect before editing: read the file and nearby code first.",
        "2. Prefer edit_file with small exact snippets over rewriting whole files.",
        "3. Keep tool output small: use offset/limit and narrow searches.",
        "4. After changes, run the relevant test, build, or lint and read the result.",
        "5. Never invent command output or claim success without verification.",
        "6. If a command fails, read the error and change approach.",
        "7. Quote paths that contain spaces. Prefer absolute paths when unsure.",
        "8. Shell calls are separate processes: pass workdir or use absolute paths.",
        "9. Use update_todo for multi-step work and mark items done as you go.",
        "10. Answer directly when no tool is needed. Keep explanations short.",
    ]
    desktop = desktop_path()
    if desktop:
        parts.insert(1, f"Desktop: {desktop} (use this exact path for 'the desktop'; "
                        f"never use {Path.home() / 'Desktop'}, it is stale)")
    agents = read_agents_md(cfg.workspace)
    if agents:
        parts += ["", "PROJECT INSTRUCTIONS (AGENTS.md)", agents]
    return "\n".join(parts)


def resolve_path(workspace: Path, value) -> Path:
    """Expand ~ and env vars; relative paths are based at the workspace."""
    text = str(value or "").strip().strip('"')
    if not text:
        raise ToolError("path is required")
    text = re.sub(r"\$\{?env:(\w+)\}?", lambda m: os.environ.get(m.group(1), m.group(0)), text)
    text = os.path.expandvars(os.path.expanduser(text))
    path = Path(text)
    if not path.is_absolute():
        path = workspace / path
    path = path.resolve()

    # Models trained on the default layout keep writing to %USERPROFILE%\Desktop,
    # which is stale when OneDrive has redirected known folders.
    stale = Path.home() / "Desktop"
    real = desktop_path()
    if real and real != stale:
        try:
            path = real / path.relative_to(stale)
        except ValueError:
            pass
    return path


def normalize_path_args(h, name: str, args: dict) -> None:
    """Resolve known-folder aliases so summaries, diffs and logs show the real path."""
    if name in {"read_file", "write_file", "edit_file", "list_dir", "search_files"} and args.get("path"):
        try:
            args["path"] = str(resolve_path(h.cfg.workspace, args["path"]))
        except ToolError:
            pass


def remap_desktop_text(text: str) -> str:
    """Shell commands copied the stale %USERPROFILE%\\Desktop alias; point them at the real one."""
    stale = Path.home() / "Desktop"
    real = desktop_path()
    if real and real != stale:
        return re.sub(re.escape(str(stale)), lambda _: str(real), text, flags=re.IGNORECASE)
    return text


def render_todo(items: list[dict]) -> str:
    if not items:
        return "(empty)"
    marks = {"pending": "[ ]", "doing": "[>]", "done": "[x]"}
    return "\n".join(f"{marks[item['status']]} {item['task']}" for item in items)


def message_size(message: dict) -> int:
    total = len(str(message.get("content") or ""))
    for call in message.get("tool_calls") or []:
        total += len(str(call.get("name") or ""))
        total += len(json.dumps(call.get("arguments") or {}, ensure_ascii=False, default=str))
    return total


def trim_history(messages: list[dict], budget: int) -> None:
    """Keep whole user turns and valid tool-call groups; drop the oldest first."""
    def total() -> int:
        return sum(message_size(m) for m in messages)

    if total() > budget:
        for message in messages[1:]:
            content = str(message.get("content") or "")
            if message.get("role") == "tool" and len(content) > 1500:
                message["content"] = content[:800] + "\n...[trimmed]...\n" + content[-400:]
                if total() <= budget:
                    return
    while len(messages) > 2 and total() > budget:
        nxt = next((i for i in range(2, len(messages)) if messages[i].get("role") == "user"), None)
        if nxt is None:
            break
        del messages[1:nxt]


def merge_ollama_event(state: dict, data: dict) -> dict:
    msg = data.get("message") or {}
    state["content"] += msg.get("content") or ""
    state["thinking"] += msg.get("thinking") or ""
    for tool_call in msg.get("tool_calls") or []:
        function = tool_call.get("function") or {}
        name = function.get("name") or ""
        arguments = function.get("arguments")
        try:
            key = (name, json.dumps(arguments, sort_keys=True, ensure_ascii=False))
        except TypeError:
            key = (name, str(arguments))
        if key in state["seen"]:
            continue
        state["seen"].add(key)
        state["tool_calls"].append({"name": name, "arguments": arguments})
    if data.get("done"):
        state["done"] = True
        state["done_reason"] = data.get("done_reason") or ""
        state["eval_count"] = int(data.get("eval_count") or 0)
        state["prompt_eval_count"] = int(data.get("prompt_eval_count") or 0)
        state["eval_duration"] = float(data.get("eval_duration") or 0)
        state["load_duration"] = float(data.get("load_duration") or 0)
    return state

# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
def size_str(path: Path) -> str:
    try:
        return f"{path.stat().st_size:,} bytes"
    except OSError:
        return "unknown size"


def tool_shell(h, args: dict) -> str:
    command = str(args.get("command", "")).strip()
    if not command:
        raise ToolError("command is required")
    try:
        timeout = int(args.get("timeout", SHELL_TIMEOUT))
    except (TypeError, ValueError):
        timeout = SHELL_TIMEOUT
    timeout = max(1, min(timeout, SHELL_TIMEOUT_MAX))
    workdir = resolve_path(h.cfg.workspace, args.get("workdir") or ".")
    if not workdir.is_dir():
        raise ToolError(f"workdir is not a directory: {workdir}")
    command = remap_desktop_text(command)

    # Force UTF-8 on the child's redirected output, then decode it as UTF-8.
    wrapped = "$OutputEncoding=[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + command
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", wrapped],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # shortcut: only the shell is killed; grandchildren may survive.
        return f"ERROR: command timed out after {timeout}s and was killed."

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    parts = [f"exit_code: {proc.returncode}"]
    if out:
        parts.append("stdout:\n" + out)
    if err:
        parts.append("stderr:\n" + err)
    if not out and not err:
        parts.append("(no output)")
    return "\n".join(parts)


def tool_list_dir(h, args: dict) -> str:
    path = resolve_path(h.cfg.workspace, args.get("path") or ".")
    if not path.exists():
        raise ToolError(f"not found: {path}")
    if not path.is_dir():
        raise ToolError(f"not a directory: {path}")
    recursive = bool(args.get("recursive", False))
    entries: list[str] = []

    if recursive:
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            base = Path(dirpath)
            entries.extend(str(base / d) + os.sep for d in dirnames)
            entries.extend(f"{base / f}  ({size_str(base / f)})" for f in sorted(filenames))
            if len(entries) >= LIST_MAX_ENTRIES:
                break
    else:
        for child in sorted(path.iterdir(), key=lambda c: (c.is_file(), c.name.lower())):
            entries.append(f"{child}{os.sep}" if child.is_dir() else f"{child}  ({size_str(child)})")

    if not entries:
        return f"{path}: empty"
    note = f" (stopped at {LIST_MAX_ENTRIES} entries)" if len(entries) >= LIST_MAX_ENTRIES else ""
    return f"{path} ({len(entries)} entries){note}:\n" + "\n".join(entries[:LIST_MAX_ENTRIES])


def tool_read_file(h, args: dict) -> str:
    path = resolve_path(h.cfg.workspace, args.get("path", ""))
    if not path.is_file():
        raise ToolError(f"not a file: {path}")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ToolError(str(exc)) from exc

    lines = text.splitlines()
    total = len(lines)
    try:
        offset = int(args.get("offset", 1) or 1)
    except (TypeError, ValueError):
        offset = 1
    try:
        limit = int(args.get("limit", READ_MAX_LINES) or READ_MAX_LINES)
    except (TypeError, ValueError):
        limit = READ_MAX_LINES
    offset = max(1, offset)
    limit = max(1, min(limit, READ_HARD_MAX))
    if total and offset > total:
        raise ToolError(f"offset {offset} is past the end of the file ({total} lines)")

    chunk = lines[offset - 1: offset - 1 + limit]
    end = offset + len(chunk) - 1 if chunk else offset - 1
    header = f"{path} (lines {offset}-{end} of {total}; line numbers are reference only):"
    body = "\n".join(f"{number:>5}| {line}" for number, line in enumerate(chunk, offset))
    return header + "\n" + (body if chunk else "(empty file)")


def tool_write_file(h, args: dict) -> str:
    path = resolve_path(h.cfg.workspace, args.get("path", ""))
    content = args.get("content")
    if content is None:
        raise ToolError("content is required")
    if path.exists() and path.is_dir():
        raise ToolError(f"path is a directory: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
    except OSError as exc:
        raise ToolError(str(exc)) from exc
    return f"wrote {len(str(content))} chars to {path}"


def tool_edit_file(h, args: dict) -> str:
    path = resolve_path(h.cfg.workspace, args.get("path", ""))
    if not path.is_file():
        raise ToolError(f"not a file: {path}")
    old = args.get("old")
    new = args.get("new")
    if not isinstance(old, str) or not old:
        raise ToolError("old is required and must be non-empty (use write_file to create a file)")
    if not isinstance(new, str):
        raise ToolError("new is required (use an empty string to delete)")
    replace_all = bool(args.get("replace_all", False))
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ToolError(str(exc)) from exc
    count = text.count(old)
    if count == 0:
        raise ToolError(f"old text not found in {path}")
    if count > 1 and not replace_all:
        raise ToolError(f"old text occurs {count} times in {path}; add more context or set replace_all")
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    try:
        path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        raise ToolError(str(exc)) from exc
    return f"edited {path} ({count if replace_all else 1} replacement(s))"


def tool_search_files(h, args: dict) -> str:
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ToolError("pattern is required")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"invalid regex: {exc}") from exc

    base = resolve_path(h.cfg.workspace, args.get("path") or ".")
    if not base.exists():
        raise ToolError(f"not found: {base}")
    name_glob = str(args.get("glob") or "*").lower()
    matches: list[str] = []
    scanned = 0
    truncated = False

    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in filenames:
            if not fnmatch.fnmatch(name.lower(), name_glob):
                continue
            scanned += 1
            if scanned > SEARCH_MAX_FILES:
                truncated = True
                break
            file_path = Path(dirpath) / name
            try:
                if file_path.stat().st_size > SEARCH_MAX_BYTES:
                    continue
                raw = file_path.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:4096]:
                continue
            text = raw.decode("utf-8", "replace")
            for line_no, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    matches.append(f"{file_path}:{line_no}: {line.strip()[:300]}")
                    if len(matches) >= SEARCH_MAX_MATCHES:
                        truncated = True
                        break
            if truncated:
                break
        if truncated:
            break

    if not matches:
        return f"no matches for {pattern!r} under {base} (scanned {scanned} files)"
    header = f"{len(matches)} match(es) for {pattern!r} under {base}:"
    if truncated:
        header += f" (stopped at {SEARCH_MAX_FILES} files / {SEARCH_MAX_MATCHES} matches)"
    return header + "\n" + "\n".join(matches)


def tool_update_todo(h, args: dict) -> str:
    items = args.get("items")
    if not isinstance(items, list) or not items:
        raise ToolError("items must be a non-empty list")
    cleaned: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            raise ToolError("each item must be an object with task and status")
        task = str(item.get("task") or "").strip()
        status = str(item.get("status") or "pending").lower()
        if not task:
            raise ToolError("each item needs a non-empty task")
        if status not in {"pending", "doing", "done"}:
            raise ToolError(f"invalid status {status!r}; use pending, doing, or done")
        cleaned.append({"task": task, "status": status})
    if len(cleaned) > MAX_TODO_ITEMS:
        raise ToolError(f"too many todo items (max {MAX_TODO_ITEMS})")
    h.todo = cleaned
    return "todo updated:\n" + render_todo(cleaned)


TOOL_FUNCS = {
    "shell": tool_shell,
    "list_dir": tool_list_dir,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "search_files": tool_search_files,
    "update_todo": tool_update_todo,
}


def execute_tool(h, name: str, args: dict) -> str:
    func = TOOL_FUNCS.get(name)
    if func is None:
        return f"ERROR: unknown tool {name!r}. Available: {', '.join(TOOL_FUNCS)}"
    try:
        return clip(str(func(h, args)), h.tool_chars)
    except ToolError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:  # a broken tool must never kill the loop
        return f"ERROR: {type(exc).__name__}: {exc}"


def clip(text: str, limit: int = MAX_TOOL_CHARS) -> str:
    s = str(text)
    if len(s) <= limit:
        return s
    head = limit * 2 // 3
    tail = limit - head
    return s[:head] + f"\n...[truncated {len(s) - limit} chars]...\n" + s[-tail:]

# Add/remove a tool here and in TOOL_FUNCS, then update the prompt.
TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "shell",
        "description": "Run a PowerShell 5.1 command as the current user. Full disk access. Each call is a new process; cwd defaults to the workspace, or pass workdir. Killed after timeout seconds.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "PowerShell command to run."},
            "workdir": {"type": "string", "description": "Directory to run in. Defaults to the workspace."},
            "timeout": {"type": "integer", "description": f"Seconds before the command is killed (default {SHELL_TIMEOUT}, max {SHELL_TIMEOUT_MAX})."},
        }, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List a directory. Directories end with a path separator and files show their size.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory path, absolute or relative to the workspace. Defaults to the workspace."},
            "recursive": {"type": "boolean", "description": "Walk subdirectories (capped at 500 entries)."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a UTF-8 text file. Output lines are prefixed with line numbers for reference; do not include those prefixes in edit_file old text. Use offset/limit for large files.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path, absolute or relative to the workspace."},
            "offset": {"type": "integer", "description": "1-based line to start at (default 1)."},
            "limit": {"type": "integer", "description": f"Maximum lines to return (default {READ_MAX_LINES})."},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite a text file. Parent directories are created automatically. Overwrites are permanent.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path, absolute or relative to the workspace."},
            "content": {"type": "string", "description": "Full file content."},
        }, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace exact text in a file. Fails if old is not found, or occurs more than once unless replace_all is true.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path, absolute or relative to the workspace."},
            "old": {"type": "string", "description": "Exact text to replace (non-empty)."},
            "new": {"type": "string", "description": "Replacement text; empty string deletes it."},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence instead of requiring exactly one."},
        }, "required": ["path", "old", "new"]},
    }},
    {"type": "function", "function": {
        "name": "search_files",
        "description": "Search file contents with a case-sensitive regular expression. Prefix (?i) for case-insensitive. Skips .git, node_modules, caches, and binaries.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Regular expression to search for."},
            "path": {"type": "string", "description": "Directory to search, absolute or relative to the workspace. Defaults to the workspace."},
            "glob": {"type": "string", "description": "Filename filter such as '*.py'. Defaults to all files."},
        }, "required": ["pattern"]},
    }},
    {"type": "function", "function": {
        "name": "update_todo",
        "description": "Replace the task checklist. Use it for multi-step work and mark items done as you go.",
        "parameters": {"type": "object", "properties": {
            "items": {"type": "array", "description": "Full checklist, max 12 items.", "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "status": {"type": "string", "enum": ["pending", "doing", "done"]},
                },
                "required": ["task", "status"],
            }},
        }, "required": ["items"]},
    }},
]

# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

LOGO = (
    "██████╗  ██████╗",
    "██╔══██╗██╔════╝",
    "██║  ██║██║",
    "██║  ██║██║",
    "██████╔╝╚██████╗",
    "╚═════╝  ╚═════╝",
)
LOGO_WIDTH = max(len(row) for row in LOGO)
LOGO_INDENT = 2 + LOGO_WIDTH + 4
LOGO_STYLES = ("1", "1", "90", "90", "2", "2")

TOOL_LABELS = {
    "shell": "SHELL",
    "list_dir": "LIST",
    "read_file": "READ",
    "write_file": "WRITE",
    "edit_file": "EDIT",
    "search_files": "SEARCH",
    "update_todo": "TODO",
}


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", str(text))


def visible_len(text: str) -> int:
    return len(strip_ansi(text))


def ellipsize(text: str, width: int) -> str:
    text = strip_ansi(str(text))
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def shorten_left(text: str, width: int) -> str:
    text = strip_ansi(str(text))
    return text if len(text) <= width else "…" + text[-(width - 1):]


def fmt_time(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds:.2f}s"
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes}m {rest:02d}s"


def fmt_tool(name: str) -> str:
    return TOOL_LABELS.get(name, (name or "?").replace("_", " ").upper())


def one_line(value) -> str:
    return " ".join(str(value or "").split())


def parse_arguments(raw) -> dict:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError("arguments must be a JSON object")
    return raw


def args_summary(name: str, args: dict) -> str:
    def field(key: str) -> str:
        return one_line(args.get(key, ""))

    if name == "shell":
        text = field("command") or "(no command)"
        if args.get("workdir"):
            text += f"  (in {field('workdir')})"
        return text
    if name == "list_dir":
        return (field("path") or ".") + ("  recursive" if args.get("recursive") else "")
    if name == "read_file":
        extra = []
        if args.get("offset"):
            extra.append(f"from line {args['offset']}")
        if args.get("limit"):
            extra.append(f"{args['limit']} lines")
        return (field("path") or "(no path)") + (("  " + " · ".join(extra)) if extra else "")
    if name == "write_file":
        content = str(args.get("content") or "")
        lines = content.count("\n") + 1 if content else 0
        plural = "s" if lines != 1 else ""
        return f"{field('path') or '(no path)'}  ({len(content):,} chars · {lines} line{plural})"
    if name == "edit_file":
        suffix = "  all" if args.get("replace_all") else ""
        return (field("path") or "(no path)") + suffix
    if name == "search_files":
        text = f"/{field('pattern')}/"
        if args.get("glob"):
            text += f"  in {field('glob')}"
        return text + f"  under {field('path') or '.'}"
    if name == "update_todo":
        items = args.get("items") if isinstance(args.get("items"), list) else []
        return f"{len(items)} item{'s' if len(items) != 1 else ''}"
    return one_line(json.dumps(args, ensure_ascii=False, default=str))

class Spinner:
    """A tiny daemon-thread spinner. Silent unless stdout is a real terminal."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, ui: "UI", prefix: str, label: str = "THINKING"):
        self.ui = ui
        self.prefix = prefix
        self.label = label
        self.enabled = ui.spinner_enabled
        self.elapsed = 0.0
        self._start = 0.0
        self._width = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "Spinner":
        self._start = time.perf_counter()
        if self.enabled:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        index = 0
        while not self._stop.wait(0.08):
            frame = self.FRAMES[index % len(self.FRAMES)]
            elapsed = time.perf_counter() - self._start
            line = (
                f"{self.prefix}  {self.ui.blue(frame)} {self.ui.gray(self.label)}"
                f"   {self.ui.gray(fmt_time(elapsed))}"
            )
            width = visible_len(line)
            pad = " " * max(0, self._width - width)
            self.ui.stream.write("\r" + line + pad)
            self.ui.stream.flush()
            self._width = max(self._width, width)
            index += 1

    def __exit__(self, *exc) -> bool:
        self.elapsed = time.perf_counter() - self._start
        if self._thread:
            self._stop.set()
            self._thread.join(timeout=1)
            if self._width:
                self.ui.stream.write("\r" + " " * self._width + "\r")
                self.ui.stream.flush()
        return False


class UI:
    """Monochrome Apple-ish terminal rendering with one blue accent."""

    def __init__(self, color: str = "auto", stream=None):
        self.stream = stream or sys.stdout
        try:
            self.tty = bool(self.stream.isatty())
        except (AttributeError, ValueError):
            self.tty = False
        no_color = "NO_COLOR" in os.environ
        self.color = color == "always" or (color == "auto" and self.tty and not no_color)
        if color == "never":
            self.color = False
        self.spinner_enabled = self.tty
        try:
            columns = shutil.get_terminal_size((80, 24)).columns
        except OSError:
            columns = 80
        self.width = max(40, min(columns, 88))

    def _paint(self, text, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else str(text)

    def gray(self, text) -> str:
        return self._paint(text, "90")

    def strong(self, text) -> str:
        return self._paint(text, "1")

    def blue(self, text) -> str:
        return self._paint(text, "38;5;33")

    def green(self, text) -> str:
        return self._paint(text, "92")

    def red(self, text) -> str:
        return self._paint(text, "91")

    def amber(self, text) -> str:
        return self._paint(text, "93")

    def out(self, text: str = "") -> None:
        print(text, file=self.stream)

    def row(self, left: str, right: str = "", indent: str = "  ") -> str:
        if not right:
            return indent + left
        space = max(2, self.width - visible_len(indent) - visible_len(left) - visible_len(right))
        return indent + left + " " * space + right

    @staticmethod
    def _lines(text: str) -> list[str]:
        clean = strip_ansi(str(text)).replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.rstrip() for line in clean.split("\n")]
        while lines and not lines[-1]:
            lines.pop()
        return lines or [""]

    def prompt(self) -> str:
        return "\n" + self.gray("YOU") + " " + self.blue("›") + " "

    def spec(self, label: str, value: str) -> str:
        return self.gray(label.ljust(9)) + "  " + self.strong(value)

    def banner(self, cfg, session_path: Path) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d · %H:%M")
        threads = f"{cfg.threads} threads" if cfg.threads else "auto threads"
        info = [
            self.strong("D E E P C O D E"),
            self.gray("CODING AGENT"),
            self.blue("─" * 36),
            self.spec("MODEL", cfg.model),
            self.spec("BACKEND", f"{cfg.backend} · ctx {cfg.ctx} · {threads}"),
            self.spec("API", shorten_left(str(cfg.api), 46)),
            self.spec("WORKSPACE", shorten_left(str(cfg.workspace), 46)),
            self.spec("SESSION", stamp),
            self.spec("LOG", shorten_left(str(session_path), 46)),
        ]
        logo = [self._paint(row.ljust(LOGO_WIDTH), LOGO_STYLES[i]) for i, row in enumerate(LOGO)]
        if self.width >= 78:
            for row, line in zip(logo, info):
                self.out("  " + row + "    " + line)
            for line in info[len(logo):]:
                self.out(" " * LOGO_INDENT + line)
        else:
            for row in logo:
                self.out("  " + row)
            self.out()
            for line in info:
                self.out("  " + line)
        self.out()
        self.out(
            "  " + self.gray("Type a request.") + "  "
            + self.strong("/new") + self.gray(" resets.") + "  "
            + self.strong("/exit") + self.gray(" quits.") + "  "
            + self.strong("Ctrl+C") + self.gray(" aborts.")
        )

    def step_prefix(self, number: int) -> str:
        return "  " + self.blue("STEP") + " " + self.strong(f"{number:02d}")

    def step(self, number: int, seconds: float, generated: int = 0, rate: float = 0.0) -> None:
        bits = [fmt_time(seconds)]
        if generated:
            bits.append(f"{generated} tok")
        if rate > 0:
            bits.append(f"{rate:.1f}/s")
        self.out(self.row(self.step_prefix(number), self.gray(" · ".join(bits))))

    def tool_prefix(self, name: str, summary: str = "") -> str:
        indent = "    "
        label = self.gray(fmt_tool(name).ljust(7))
        room = self.width - len(indent) - 9 - 22
        text = ellipsize(summary, max(8, room))
        return indent + label + ("  " + self.strong(text) if text else "")

    def tool(self, name: str, summary: str, seconds: float | None = None) -> None:
        indent = "    "
        duration = self.gray(fmt_time(seconds)) if seconds is not None else ""
        room = self.width - len(indent) - 9 - visible_len(duration) - 2
        left = self.gray(fmt_tool(name).ljust(7)) + "  " + self.strong(ellipsize(summary, max(16, room)))
        self.out(self.row(left, duration, indent=indent))

    def note(self, label: str, text: str, paint) -> None:
        lines = self._lines(text)
        self.out("    " + self.gray(label))
        for line in lines[:8]:
            self.out("    │ " + paint(line))
        if len(lines) > 8:
            self.out("    │ " + self.gray(f"… +{len(lines) - 8} lines"))

    def thought(self, text: str) -> None:
        self.note("THINK", text, self.gray)

    def say(self, text: str) -> None:
        self.note("SAY", text, self.gray)

    def todo(self, items: list[dict]) -> None:
        marks = {
            "done": ("✓", self.green, self.gray),
            "doing": ("›", self.blue, self.strong),
            "pending": ("·", self.gray, self.gray),
        }
        for item in items:
            mark, paint, text_paint = marks.get(item.get("status"), marks["pending"])
            self.out("    │ " + paint(mark) + " " + text_paint(item.get("task", "")))

    def diff(self, args: dict) -> None:
        path = str(args.get("path") or "")
        if path:
            self.out("    │ " + self.gray("edited " + path))
        old = str(args.get("old") or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        new = str(args.get("new") or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        lines = [("-", line) for line in old] + [("+", line) for line in new]
        cap = 12
        for sign, line in lines[:cap]:
            paint = self.red if sign == "-" else self.green
            self.out("    │ " + paint(sign + " " + line))
        if len(lines) > cap:
            self.out("    │ " + self.gray(f"… +{len(lines) - cap} lines"))

    def _block(self, lines: list[str], cap: int, paint, header: str | None = None) -> None:
        if header is not None:
            self.out("    │ " + header)
            cap -= 1
        for line in lines[:max(0, cap)]:
            self.out(("    │ " + paint(line)) if line else "    │")
        if len(lines) > cap:
            self.out("    │ " + self.gray(f"… +{len(lines) - cap} lines · full output in session log"))

    def result(self, observation: str, error: bool = False) -> None:
        lines = self._lines(observation)
        if error:
            self._block(lines, 15, self.red)
            return
        header = None
        if lines[0].startswith("exit_code:"):
            code = lines[0].split(":", 1)[1].strip()
            body = [line for line in lines[1:] if line not in {"stdout:", "stderr:"} and line.strip()]
            count = len(body)
            label = f"exit {code}" + (f" · {count} line{'s' if count != 1 else ''}" if count else " · no output")
            header = self.green(label) if code == "0" else self.red(label)
            lines = body
        self._block(lines, 15, self.gray, header=header)

    def warn(self, message: str) -> None:
        self.out("  " + self.amber("! WARN") + "    " + self.strong(message))

    def error(self, message: str) -> None:
        self.out("  " + self.red("✗ ERROR") + "    " + self.strong(message))

    def stop(self, message: str) -> None:
        self.out("  " + self.amber("■ STOP") + "    " + self.strong(message))

    def aborted(self) -> None:
        self.out("  " + self.amber("■ ABORTED") + "    " + self.gray("returned to prompt"))

    def meta(self, text: str) -> None:
        self.out("  " + self.gray(text))

    def help(self) -> None:
        self.out()
        self.out("  " + self.gray("COMMANDS"))
        self.out("  " + self.strong("/new") + self.gray("    start a fresh conversation"))
        self.out("  " + self.strong("/exit") + self.gray("   quit (or /quit)"))
        self.out("  " + self.strong("/help") + self.gray("   this list"))
        self.out()

    def done(self, summary: str, seconds: float, steps: int, tokens: int,
             rate: float = 0.0, load: float = 0.0, gap: bool = True) -> None:
        bits = [fmt_time(seconds), f"{steps} step{'s' if steps != 1 else ''}"]
        if tokens:
            bits.append(f"{tokens:,} tokens")
        if rate > 0:
            bits.append(f"{rate:.1f} tok/s")
        if load >= 0.1:
            bits.append(f"load {fmt_time(load)}")
        if gap:
            self.out()
        self.out("  " + self.gray("─" * max(20, min(self.width - 4, 56))))
        self.out("  " + self.green("✓ DONE") + "    " + self.strong(" · ".join(bits)))
        lines = self._lines(summary)
        if summary.strip():
            for line in lines[:12]:
                self.out("    " + self.gray(line))
            if len(lines) > 12:
                self.out("    " + self.gray("…"))

# --------------------------------------------------------------------------
# Model clients
# --------------------------------------------------------------------------
def to_ollama_messages(messages: list[dict]) -> list[dict]:
    out = []
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            out.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": [{"function": {"name": call.get("name") or "",
                                             "arguments": call.get("arguments") or {}}}
                               for call in message["tool_calls"]],
            })
        elif role == "tool":
            out.append({"role": "tool", "tool_name": message.get("name") or "unknown",
                        "content": message.get("content") or ""})
        else:
            out.append({"role": role, "content": message.get("content") or ""})
    return out


def to_openai_messages(messages: list[dict]) -> list[dict]:
    out = []
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            calls = []
            for call in message["tool_calls"]:
                arguments = call.get("arguments") or {}
                text = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
                calls.append({"id": call.get("id") or "call_0", "type": "function",
                              "function": {"name": call.get("name") or "", "arguments": text}})
            out.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": message.get("tool_call_id") or "call_0",
                        "content": message.get("content") or ""})
        else:
            out.append({"role": role, "content": message.get("content") or ""})
    return out


def native_base(api: str) -> str:
    base = api.rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


class OllamaClient:
    def __init__(self, cfg):
        self.cfg = cfg

    def chat(self, messages: list[dict], deadline: float = 0.0) -> dict:
        cfg = self.cfg
        options = {
            "temperature": cfg.temperature,
            "num_ctx": cfg.ctx,
            "num_predict": cfg.max_predict,
            "num_batch": NUM_BATCH,
        }
        if cfg.threads:
            options["num_thread"] = cfg.threads
        payload = {
            "model": cfg.model,
            "messages": to_ollama_messages(messages),
            "tools": TOOL_SCHEMAS,
            "stream": True,
            "think": False,
            "keep_alive": KEEP_ALIVE,
            "options": options,
        }
        request = urllib.request.Request(
            native_base(cfg.api) + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        state = {"content": "", "thinking": "", "tool_calls": [], "seen": set(),
                 "done": False, "done_reason": "", "eval_count": 0, "prompt_eval_count": 0,
                 "eval_duration": 0.0, "load_duration": 0.0}
        try:
            with urllib.request.urlopen(request, timeout=MODEL_TIMEOUT) as response:
                for raw in response:
                    if deadline and time.perf_counter() > deadline:
                        raise TaskTimeout()
                    line = raw.strip()
                    if not line:
                        continue
                    merge_ollama_event(state, json.loads(line))
                    if state["done"]:
                        break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            if exc.code == 404:
                raise RuntimeError(
                    f"Ollama does not have model {cfg.model!r}. Pull it first: ollama pull {cfg.model}"
                ) from exc
            raise RuntimeError(f"Ollama HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"cannot reach Ollama at {native_base(cfg.api)} ({exc.reason}). Start it with: ollama serve"
            ) from exc

        calls = [{"id": f"call_{uuid.uuid4().hex[:8]}", "name": call["name"],
                  "arguments": call["arguments"]} for call in state["tool_calls"]]
        return {
            "content": state["content"],
            "thinking": state["thinking"],
            "tool_calls": calls,
            "completion_tokens": state["eval_count"],
            "prompt_tokens": state["prompt_eval_count"],
            "eval_seconds": state["eval_duration"] / 1e9,
            "load_seconds": state["load_duration"] / 1e9,
            "done_reason": state["done_reason"],
        }


class OpenAIClient:
    def __init__(self, cfg):
        self.cfg = cfg

    def chat(self, messages: list[dict], deadline: float = 0.0) -> dict:
        cfg = self.cfg
        base = cfg.api.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        payload = {
            "model": cfg.model,
            "messages": to_openai_messages(messages),
            "tools": TOOL_SCHEMAS,
            "tool_choice": "auto",
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_predict,
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if cfg.key:
            headers["Authorization"] = f"Bearer {cfg.key}"
        request = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=MODEL_TIMEOUT) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            if exc.code == 401:
                raise RuntimeError(f"API rejected the key (401): {body}") from exc
            if exc.code == 404:
                raise RuntimeError(f"model or endpoint not found (404): {body}") from exc
            raise RuntimeError(f"API HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"cannot reach API at {base} ({exc.reason})") from exc

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("API returned no choices")
        message = choices[0].get("message") or {}
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            raw_args = function.get("arguments")
            try:
                arguments = parse_arguments(raw_args)
            except ValueError:
                arguments = raw_args
            calls.append({"id": call.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                          "name": function.get("name") or "", "arguments": arguments})
        usage = data.get("usage") or {}
        return {
            "content": message.get("content") or "",
            "thinking": message.get("reasoning_content") or "",
            "tool_calls": calls,
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "eval_seconds": 0.0,
            "load_seconds": 0.0,
            "done_reason": choices[0].get("finish_reason") or "",
        }


def make_client(cfg):
    return OllamaClient(cfg) if cfg.backend == "ollama" else OpenAIClient(cfg)

# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------
def drop_incomplete_tail(messages: list[dict]) -> list[dict]:
    """A resumed log may end mid tool-call; drop that partial group."""
    while messages and messages[-1].get("role") == "tool":
        messages.pop()
    if messages and messages[-1].get("role") == "assistant" and messages[-1].get("tool_calls"):
        messages.pop()
    return messages


class Harness:
    def __init__(self, cfg, ui: UI):
        self.cfg = cfg
        self.ui = ui
        self.client = make_client(cfg)
        self.messages: list[dict] = []
        self.todo: list[dict] = []
        self.repeat_key: str | None = None
        self.repeat_count = 0
        self.session_path: Path | None = None
        self.log_fh = None
        self._ps_shown = False
        self.system_prompt = build_system_prompt(cfg)
        # ~2.8 chars/token measured on code and JSON; keep room for the answer too.
        reserve = min(cfg.max_predict, 1024)
        self.history_budget = max(1500, int((cfg.ctx - reserve) * 2.8)
                                  - len(json.dumps(TOOL_SCHEMAS)))
        self.tool_chars = min(MAX_TOOL_CHARS, max(1500, int(cfg.ctx * 0.75)))
        self._open_log(cfg.resume)
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = {"role": "system", "content": self.system_prompt}
        else:
            self.messages.insert(0, {"role": "system", "content": self.system_prompt})

    def _open_log(self, resume) -> None:
        directory = Path(self.cfg.session_dir)
        directory.mkdir(parents=True, exist_ok=True)
        if resume:
            path = None
            loaded: list[dict] = []
            if resume == "latest":
                # Newest log can be an empty session from /new; skip to the newest with messages.
                files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
                for candidate in files:
                    loaded = drop_incomplete_tail(self._read_messages(candidate))
                    if loaded:
                        path = candidate
                        break
                if files and not path:
                    self.ui.warn("no session with messages to resume; starting a new session")
            else:
                path = Path(str(resume)).expanduser()
                if path.is_file():
                    loaded = drop_incomplete_tail(self._read_messages(path))
                    if not loaded:
                        self.ui.warn(f"no usable messages in {path}; starting a new session")
                else:
                    self.ui.warn(f"resume target not found: {path or resume}; starting a new session")
            if path and loaded:
                self.session_path = path
                self.log_fh = open(path, "a", encoding="utf-8")
                self.messages = loaded
                self.log("resumed", messages=len(loaded))
                self.ui.meta(f"resumed {len(loaded)} messages from {shorten_left(str(path), 60)}")
                return

        stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        self.session_path = directory / (stamp + ".jsonl")
        serial = 1
        while self.session_path.exists():
            serial += 1
            self.session_path = directory / f"{stamp}-{serial}.jsonl"
        self.log_fh = open(self.session_path, "a", encoding="utf-8")
        self.log("start", model=self.cfg.model, api=self.cfg.api, backend=self.cfg.backend,
                 workspace=str(self.cfg.workspace), threads=self.cfg.threads, ctx=self.cfg.ctx)

    @staticmethod
    def _read_messages(path: Path) -> list[dict]:
        messages: list[dict] = []
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return messages
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("kind") == "message" and isinstance(record.get("message"), dict):
                messages.append(record["message"])
        return messages

    def log(self, kind: str, **data) -> None:
        if not self.log_fh:
            return
        record = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind, **data}
        try:
            self.log_fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self.log_fh.flush()
        except OSError:
            pass

    def append(self, message: dict) -> None:
        self.messages.append(message)
        self.log("message", message=message)

    def close(self) -> None:
        self.log("end")
        if self.log_fh:
            self.log_fh.close()
            self.log_fh = None
        if self.cfg.backend == "ollama":
            try:
                request = urllib.request.Request(
                    native_base(self.cfg.api) + "/api/generate",
                    data=json.dumps({"model": self.cfg.model, "keep_alive": 0}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(request, timeout=3).read()
            except Exception:
                pass

    def new_session(self) -> None:
        if self.log_fh:
            self.log_fh.close()
            self.log_fh = None
        self.messages = []
        self.todo = []
        self.repeat_key = None
        self.repeat_count = 0
        self._open_log(None)
        self.messages.insert(0, {"role": "system", "content": self.system_prompt})
        self.ui.meta(f"new session · {self.session_path.name}")

    def refresh_ps(self) -> None:
        if self.cfg.backend != "ollama" or self._ps_shown:
            return
        self._ps_shown = True
        try:
            with urllib.request.urlopen(native_base(self.cfg.api) + "/api/ps", timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception:
            return
        model = None
        for item in data.get("models") or []:
            name = item.get("name") or item.get("model") or ""
            if name == self.cfg.model or name.startswith(self.cfg.model + ":"):
                model = item
                break
        if not model:
            return
        size = int(model.get("size") or 0)
        vram = int(model.get("size_vram") or 0)
        threads = f"{self.cfg.threads} threads" if self.cfg.threads else "auto threads"
        if size:
            pct = round(100 * vram / size)
            self.ui.meta(f"GPU {pct}% · CPU {100 - pct}% · {threads} · ctx {self.cfg.ctx}")
            if pct < 100:
                self.ui.warn("CPU fallback is active; expect heat. Lower --threads or use a smaller model.")
        else:
            self.ui.meta(f"{threads} · ctx {self.cfg.ctx}")

    def run_turn(self, task: str) -> None:
        user_message = {"role": "user", "content": task}
        self.append(user_message)
        trim_history(self.messages, self.history_budget)
        self.todo = []
        self.repeat_key = None
        self.repeat_count = 0
        started = time.perf_counter()
        deadline = started + self.cfg.max_minutes * 60 if self.cfg.max_minutes else 0.0
        self.log("task", task=task)
        steps = 0
        tokens = 0
        prompt_tokens = 0
        eval_seconds = 0.0
        load_seconds = 0.0
        empties = 0

        try:
            for step in range(1, self.cfg.max_steps + 1):
                if deadline and time.perf_counter() > deadline:
                    self.ui.stop(f"time limit ({self.cfg.max_minutes} min) reached")
                    self.log("timeout")
                    return
                self.ui.out()
                try:
                    with Spinner(self.ui, self.ui.step_prefix(step)) as spin:
                        result = self.client.chat(self.messages, deadline)
                except TaskTimeout:
                    self.ui.stop(f"time limit ({self.cfg.max_minutes} min) reached")
                    self.log("timeout")
                    return
                except RuntimeError as exc:
                    self.ui.error(str(exc))
                    self.log("error", error=str(exc))
                    return
                except KeyboardInterrupt:
                    self.ui.aborted()
                    self.log("aborted")
                    return

                steps = step
                content = result.get("content") or ""
                thinking = result.get("thinking") or ""
                calls = result.get("tool_calls") or []
                generated = int(result.get("completion_tokens") or 0)
                tokens += generated
                prompt_tokens += int(result.get("prompt_tokens") or 0)
                step_eval = float(result.get("eval_seconds") or 0)
                eval_seconds += step_eval
                if result.get("load_seconds") and not load_seconds:
                    load_seconds = float(result["load_seconds"])
                rate = generated / step_eval if step_eval > 0.05 and generated >= 2 else 0.0
                if self.cfg.backend == "ollama":
                    self.refresh_ps()

                if not calls:
                    self.ui.step(step, spin.elapsed, generated, rate)
                    if not content.strip():
                        used = int(result.get("prompt_tokens") or 0)
                        reason = result.get("done_reason") or "?"
                        if empties == 0:
                            self.ui.warn(f"no answer (prompt {used}/{self.cfg.ctx} tokens, "
                                         f"done {reason}); trimming history and retrying")
                            self.log("empty_reply", step=step, prompt_tokens=used,
                                     done_reason=reason)
                            empties = 1
                            trim_history(self.messages, max(1500, self.history_budget // 2))
                            continue
                        self.ui.error("model returned an empty reply twice; stopping. "
                                      "Raise --ctx (e.g. 8192) or lower --max-predict.")
                        self.log("empty_reply_failed", step=step, prompt_tokens=used,
                                 done_reason=reason)
                        return
                    self.append({"role": "assistant", "content": content})
                    if thinking:
                        self.ui.thought(thinking)
                    avg_rate = tokens / eval_seconds if eval_seconds > 0.05 and tokens >= 2 else 0.0
                    self.ui.done(content, time.perf_counter() - started,
                                 step, tokens, avg_rate, load_seconds)
                    self.log("done", content=content)
                    return

                self.ui.step(step, spin.elapsed, generated, rate)
                if thinking:
                    self.ui.thought(thinking)
                if content:
                    self.ui.say(content)
                self.append({"role": "assistant", "content": content, "tool_calls": calls})
                self.log("assistant", step=step, content=content, thinking=thinking, tool_calls=calls)

                for index, call in enumerate(calls):
                    name = call.get("name") or ""
                    try:
                        args = parse_arguments(call.get("arguments"))
                    except ValueError as exc:
                        observation = f"ERROR: bad tool arguments: {exc}"
                        self.ui.tool(name or "unknown", "(bad arguments)")
                        self.ui.result(observation, error=True)
                        self.append({"role": "tool", "tool_call_id": call.get("id"),
                                     "name": name or "unknown", "content": observation})
                        self.log("tool", name=name or "unknown", args={}, observation=observation)
                        continue

                    normalize_path_args(self, name, args)

                    key = name + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
                    if key == self.repeat_key:
                        self.repeat_count += 1
                    else:
                        self.repeat_key, self.repeat_count = key, 1

                    summary = args_summary(name, args)
                    seconds = 0.0
                    if self.repeat_count >= 3:
                        observation = (f"ERROR: {name} was already called with these exact arguments "
                                       f"{self.repeat_count} times. Use a different approach.")
                    else:
                        try:
                            with Spinner(self.ui, self.ui.tool_prefix(name, summary), "RUNNING") as tool_spin:
                                observation = execute_tool(self, name, args)
                            seconds = tool_spin.elapsed
                        except KeyboardInterrupt:
                            for rest in calls[index:]:
                                self.append({"role": "tool", "tool_call_id": rest.get("id"),
                                             "name": rest.get("name") or "unknown",
                                             "content": "ERROR: aborted by user"})
                            self.ui.aborted()
                            self.log("aborted")
                            return

                    self.ui.tool(name, summary, seconds)
                    self.log("tool", name=name, args=args, observation=observation,
                             seconds=round(seconds, 2))
                    self.append({"role": "tool", "tool_call_id": call.get("id"),
                                 "name": name, "content": observation})

                    if observation.startswith("ERROR:"):
                        self.ui.result(observation, error=True)
                    elif name == "update_todo":
                        self.ui.todo(self.todo)
                    elif name == "edit_file":
                        self.ui.diff(args)
                    else:
                        self.ui.result(observation)

                trim_history(self.messages, self.history_budget)

            self.ui.stop(f"max steps ({self.cfg.max_steps}) reached")
            self.log("max_steps")
        except KeyboardInterrupt:
            self.ui.aborted()
            self.log("aborted")

# --------------------------------------------------------------------------
# Self-check (no model required)
# --------------------------------------------------------------------------
def self_check() -> None:
    root = Path.home().resolve()
    clipped = clip("a" * 5000 + "b" * 5000, 1000)
    assert clipped.startswith("a") and clipped.endswith("b") and len(clipped) <= 1200
    assert resolve_path(root, "Documents") == (root / "Documents").resolve()
    assert resolve_path(root, "C:/Windows") == Path("C:/Windows").resolve()
    desktop = desktop_path()
    stale_desktop = Path.home() / "Desktop"
    if desktop and desktop != stale_desktop:
        assert resolve_path(root, stale_desktop / "x.txt") == (desktop / "x.txt").resolve()
        assert remap_desktop_text(f'dir "{stale_desktop}"') == f'dir "{desktop}"'
    try:
        resolve_path(root, "")
    except ToolError:
        pass
    else:
        raise AssertionError("empty path accepted")

    assert parse_arguments({"x": 1}) == {"x": 1}
    assert parse_arguments('{"x": 1}') == {"x": 1}
    try:
        parse_arguments("[1]")
    except ValueError:
        pass
    else:
        raise AssertionError("non-object arguments accepted")

    messages = [{"role": "system", "content": "s"},
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "name": "shell", "arguments": {}}]},
                {"role": "tool", "tool_call_id": "c", "name": "shell", "content": "ok"},
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": "b"},
                {"role": "assistant", "content": "done2"}]
    trim_history(messages, 1)
    assert messages[0]["role"] == "system"
    assert messages[-1]["role"] == "assistant"

    schema_names = {tool["function"]["name"] for tool in TOOL_SCHEMAS}
    assert schema_names == set(TOOL_FUNCS), f"schema/function mismatch: {schema_names ^ set(TOOL_FUNCS)}"

    fake = argparse.Namespace(todo=[])
    tool_update_todo(fake, {"items": [{"task": "x", "status": "doing"}]})
    assert fake.todo[0]["status"] == "doing"
    try:
        tool_update_todo(fake, {"items": [{"task": "x", "status": "wat"}]})
    except ToolError:
        pass
    else:
        raise AssertionError("invalid todo status accepted")

    internal = [{"role": "assistant", "content": "", "tool_calls": [
                    {"id": "c1", "name": "read_file", "arguments": {"path": "a.py"}}]},
                {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "ok"}]
    openai_messages = to_openai_messages(internal)
    assert isinstance(openai_messages[0]["tool_calls"][0]["function"]["arguments"], str)
    assert openai_messages[1]["tool_call_id"] == "c1"
    ollama_messages = to_ollama_messages(internal)
    assert ollama_messages[0]["tool_calls"][0]["function"]["arguments"] == {"path": "a.py"}
    assert ollama_messages[1]["tool_name"] == "read_file"

    state = {"content": "", "thinking": "", "tool_calls": [], "seen": set(), "done": False,
             "done_reason": "", "eval_count": 0, "prompt_eval_count": 0,
             "eval_duration": 0.0, "load_duration": 0.0}
    merge_ollama_event(state, {"message": {"content": "hi"}})
    merge_ollama_event(state, {"message": {"tool_calls": [
        {"function": {"name": "shell", "arguments": {"command": "dir"}}}]}})
    merge_ollama_event(state, {"done": True, "done_reason": "stop", "eval_count": 5,
                               "prompt_eval_count": 7, "eval_duration": 1e9,
                               "load_duration": 2e9})
    assert state["content"] == "hi"
    assert len(state["tool_calls"]) == 1 and state["tool_calls"][0]["name"] == "shell"
    assert state["eval_count"] == 5 and state["prompt_eval_count"] == 7
    assert state["eval_duration"] == 1e9 and state["load_duration"] == 2e9
    assert state["done_reason"] == "stop"

    assert [m["role"] for m in drop_incomplete_tail(
        [{"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]}])] == []

    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        (workspace / "AGENTS.md").write_text("Use pytest.", encoding="utf-8")
        cfg = argparse.Namespace(workspace=workspace, model="test-model")
        prompt = build_system_prompt(cfg)
        assert "Use pytest." in prompt and str(workspace) in prompt
        desktop = desktop_path()
        if desktop:
            assert desktop.is_absolute() and f"Desktop: {desktop}" in prompt

    assert strip_ansi("\x1b[33mhi\x1b[0m") == "hi"
    assert visible_len("\x1b[33mhi\x1b[0m") == 2
    assert ellipsize("abcdef", 4) == "abc…"
    assert fmt_time(0.416) == "0.42s"
    assert fmt_time(8.44) == "8.4s"
    assert fmt_time(12.4) == "12s"
    assert fmt_time(75) == "1m 15s"
    assert fmt_tool("edit_file") == "EDIT"
    assert args_summary("edit_file", {"path": "notes.txt"}) == "notes.txt"

    plain = UI(color="never", stream=io.StringIO())
    assert not plain.color and plain.blue("x") == "x"
    assert plain.spinner_enabled is False
    forced = UI(color="always", stream=io.StringIO())
    assert forced.color and forced.blue("x").startswith("\x1b[")
    assert forced.spinner_enabled is False

    class FakeTTY(io.StringIO):
        def isatty(self) -> bool:
            return True

    tty_ui = UI(color="always", stream=FakeTTY())
    assert tty_ui.spinner_enabled
    with Spinner(tty_ui, tty_ui.step_prefix(1)) as spinner:
        time.sleep(0.15)
    frames = tty_ui.stream.getvalue()
    assert "\r" in frames and "THINKING" in frames
    assert spinner.elapsed >= 0.1
    settled = tty_ui.stream.getvalue()
    time.sleep(0.1)
    assert tty_ui.stream.getvalue() == settled, "spinner wrote after it stopped"

    with tempfile.TemporaryDirectory() as temp:
        sessions = Path(temp) / "sessions"
        sessions.mkdir()
        empty = sessions / "a.jsonl"
        empty.write_text(json.dumps({"kind": "start"}) + "\n", encoding="utf-8")
        full = sessions / "b.jsonl"
        full.write_text(json.dumps({"kind": "message",
                                    "message": {"role": "user", "content": "hi"}}) + "\n",
                        encoding="utf-8")
        os.utime(empty, (time.time() + 10, time.time() + 10))  # empty log is newest
        cfg = argparse.Namespace(session_dir=str(sessions), resume="latest", workspace=Path(temp),
                                 model="m", api="http://127.0.0.1:1", backend="openai",
                                 threads=0, ctx=4096, max_minutes=1, max_steps=1,
                                 max_predict=16, temperature=0.2, key="")
        resumed = Harness(cfg, UI(color="never", stream=io.StringIO()))
        assert resumed.session_path == full, f"resume picked {resumed.session_path}"
        assert [m["role"] for m in resumed.messages] == ["system", "user"]
        resumed.close()

    with tempfile.TemporaryDirectory() as temp:
        sessions = Path(temp) / "sessions"
        sessions.mkdir()
        cfg = argparse.Namespace(session_dir=str(sessions), resume=None, workspace=Path(temp),
                                 model="m", api="http://127.0.0.1:1", backend="openai",
                                 threads=0, ctx=4096, max_minutes=1, max_steps=3,
                                 max_predict=1024, temperature=0.2, key="")
        harness = Harness(cfg, UI(color="never", stream=io.StringIO()))

        class FakeClient:
            def __init__(self):
                self.calls = 0

            def chat(self, messages, deadline=0.0):
                self.calls += 1
                if self.calls == 1:
                    return {"content": "", "thinking": "", "tool_calls": [],
                            "completion_tokens": 1, "prompt_tokens": 4000,
                            "eval_seconds": 0.0, "load_seconds": 0.0,
                            "done_reason": "length"}
                return {"content": "ok", "thinking": "", "tool_calls": [],
                        "completion_tokens": 1, "prompt_tokens": 100,
                        "eval_seconds": 0.0, "load_seconds": 0.0,
                        "done_reason": "stop"}

        harness.client = FakeClient()
        harness.run_turn("create the file")
        assert harness.client.calls == 2, harness.client.calls
        assert harness.messages[-1] == {"role": "assistant", "content": "ok"}
        harness.close()

    print("self-check OK")

# --------------------------------------------------------------------------
# Doctor
# --------------------------------------------------------------------------
def doctor(cfg, ui: UI) -> int:
    if cfg.backend != "ollama":
        ui.out()
        ui.out("  " + ui.spec("BACKEND", cfg.backend))
        ui.out("  " + ui.spec("MODEL", cfg.model))
        ui.out("  " + ui.spec("API", cfg.api))
        ui.out("  " + ui.gray("remote backend: no local inference, nothing to measure"))
        ui.out()
        return 0

    options = {"num_predict": 16, "num_ctx": cfg.ctx, "temperature": 0, "num_batch": NUM_BATCH}
    if cfg.threads:
        options["num_thread"] = cfg.threads
    payload = {"model": cfg.model, "prompt": "Count from 1 to 16, separated by spaces.",
               "stream": False, "keep_alive": "30s", "options": options}
    request = urllib.request.Request(
        native_base(cfg.api) + "/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=MODEL_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        if exc.code == 404:
            ui.error(f"Ollama does not have model {cfg.model!r}. Pull it first: ollama pull {cfg.model}")
        else:
            ui.error(f"Ollama HTTP {exc.code}: {body}")
        return 1
    except urllib.error.URLError as exc:
        ui.error(f"cannot reach Ollama at {native_base(cfg.api)} ({exc.reason})")
        return 1

    load = float(data.get("load_duration") or 0) / 1e9
    eval_count = int(data.get("eval_count") or 0)
    eval_seconds = float(data.get("eval_duration") or 0) / 1e9
    rate = eval_count / eval_seconds if eval_seconds > 0 else 0.0

    ui.out()
    ui.out("  " + ui.spec("BACKEND", "ollama"))
    ui.out("  " + ui.spec("MODEL", cfg.model))
    ui.out("  " + ui.spec("LOAD", fmt_time(load) if load else "already loaded"))
    ui.out("  " + ui.spec("GEN", f"{rate:.1f} tok/s ({eval_count} token{'s' if eval_count != 1 else ''})"))
    ui.out("  " + ui.spec("SETTINGS", f"ctx {cfg.ctx} · {cfg.threads or 'auto'} threads · "
                                      f"max predict {cfg.max_predict}"))

    try:
        with urllib.request.urlopen(native_base(cfg.api) + "/api/ps", timeout=5) as response:
            ps = json.loads(response.read().decode("utf-8"))
        item = None
        for model in ps.get("models") or []:
            name = model.get("name") or model.get("model") or ""
            if name == cfg.model or name.startswith(cfg.model + ":"):
                item = model
                break
        if item:
            size = int(item.get("size") or 0)
            vram = int(item.get("size_vram") or 0)
            if size:
                pct = round(100 * vram / size)
                ui.out("  " + ui.spec("MEMORY", f"{size / 1e9:.1f} GB · GPU {pct}% · CPU {100 - pct}%"))
                if pct < 100:
                    ui.warn("CPU fallback is active; expect heat. "
                            "Lower --threads or use a smaller model.")
    except Exception:
        pass

    ui.out()
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Autonomous coding agent for Windows.")
    parser.add_argument("--model", default=os.environ.get("DEEPCODE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--api", default=os.environ.get("DEEPCODE_API", DEFAULT_API))
    parser.add_argument("--key", default=None, help="API key for OpenAI-compatible backends.")
    parser.add_argument("--backend", choices=("auto", "ollama", "openai"), default="auto")
    parser.add_argument("--workspace", default=os.getcwd(),
                        help="Project folder; relative paths resolve here (default: current directory).")
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS,
                        help="Maximum model turns per request (default 60).")
    parser.add_argument("--max-minutes", type=int, default=MAX_MINUTES,
                        help="Wall-clock cap per request in minutes, 0 disables (default 20).")
    parser.add_argument("--threads", type=int, default=DEFAULT_THREADS,
                        help="Ollama CPU threads, 0 lets Ollama decide (default 8).")
    parser.add_argument("--ctx", type=int, default=DEFAULT_CTX,
                        help="Ollama context window in tokens (default 4096).")
    parser.add_argument("--max-predict", type=int, default=DEFAULT_MAX_PREDICT,
                        help="Maximum tokens generated per model call (default 1024).")
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                        help="Color: auto (real terminal only), always, never. NO_COLOR is respected.")
    parser.add_argument("--session-dir", default=str(Path(__file__).resolve().parent / "sessions"))
    parser.add_argument("--resume", nargs="?", const="latest", default=None,
                        help="Resume the latest session, or a path to a .jsonl log.")
    parser.add_argument("--doctor", action="store_true", help="Measure local model speed and GPU split, then exit.")
    parser.add_argument("--self-check", action="store_true", help="Run local sanity checks and exit.")
    args = parser.parse_args(argv)

    if args.self_check:
        self_check()
        return 0

    if args.max_steps < 1 or args.max_minutes < 0 or not (0 <= args.threads <= 64):
        print("invalid limits: check --max-steps, --max-minutes, --threads", file=sys.stderr)
        return 2
    if args.ctx < 512 or args.ctx > 131072 or args.max_predict < 1:
        print("invalid model limits: check --ctx and --max-predict", file=sys.stderr)
        return 2
    if not (0 <= args.temperature <= 2):
        print("--temperature must be between 0 and 2", file=sys.stderr)
        return 2

    ui = UI(args.color)
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        ui.error(f"workspace is not a directory: {workspace}")
        return 2
    args.workspace = workspace
    if args.backend == "auto":
        args.backend = "openai" if args.api.rstrip("/").endswith("/v1") else "ollama"
    args.key = (args.key or os.environ.get("DEEPCODE_API_KEY")
                or os.environ.get("DEEPSEEK_API_KEY")
                or os.environ.get("OPENAI_API_KEY") or "")

    if args.doctor:
        return doctor(args, ui)

    harness = Harness(args, ui)
    ui.banner(args, harness.session_path)

    try:
        while True:
            try:
                task = input(ui.prompt()).strip()
            except (EOFError, KeyboardInterrupt):
                ui.out()
                break
            if not task:
                continue
            command = task.lower()
            if command in {"/exit", "/quit"}:
                break
            if command == "/new":
                harness.new_session()
                continue
            if command == "/help":
                ui.help()
                continue
            try:
                harness.run_turn(task)
            except KeyboardInterrupt:
                ui.aborted()
                harness.log("aborted")
            except Exception as exc:  # keep the REPL alive
                ui.error(f"{type(exc).__name__}: {exc}")
                harness.log("error", error=f"{type(exc).__name__}: {exc}")
    finally:
        harness.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())









