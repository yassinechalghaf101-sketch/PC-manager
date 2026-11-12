#!/usr/bin/env python3
"""pc-manager: a tiny autonomous Windows PC manager powered by a local Ollama model.

ReAct loop: the model reasons, calls exactly one tool, receives the observation,
and repeats until it calls finish(). No third-party dependencies.

    python pc_manager.py
    python pc_manager.py --self-check
"""

from __future__ import annotations

import argparse
import ctypes
import fnmatch
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path

# --------------------------------------------------------------------------
# Tunables. Everything the model may or may not do is described here and in
# the three prompt blocks below, so features stay easy to add or remove.
# --------------------------------------------------------------------------
DEFAULT_MODEL = "qwen3.5:4b"
DEFAULT_API = "http://127.0.0.1:11434"
MAX_STEPS = 40
KEEP_TURNS = 8                 # assistant turns kept in the conversation
MAX_TOOL_CHARS = 6000          # tool results are clipped before they hit the model
MAX_TODO_ITEMS = 12
SHELL_TIMEOUT = 120
SHELL_TIMEOUT_MAX = 600
SEARCH_MAX_FILES = 5000
SEARCH_MAX_MATCHES = 100
SEARCH_MAX_BYTES = 1_000_000
LIST_MAX_ENTRIES = 500
READ_MAX_LINES = 400
MODEL_TIMEOUT = 600            # one model call; first load can be slow
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache", "AppData", "site-packages"}

# Windows shell folder GUIDs (SHGetKnownFolderPath), so OneDrive redirection is respected.
KNOWN_FOLDER_IDS = {
    "Desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "Documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "Downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "Pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
}

CAPABILITIES = """\
- shell: run any Windows PowerShell 5.1 command as the current Windows user, including launching installed apps (Start-Process), opening files or folders (Invoke-Item/explorer), and opening URLs in the default browser.
- list_dir, read_file, write_file, edit_file, search_files: inspect and modify files under the allowed root.
- update_todo: keep a checklist for multi-step work.
- finish: end the task with a short summary."""

LIMITATIONS = """\
- You can launch apps but cannot click, type, or read inside their windows; the harness is text only. No screenshots or images.
- No dedicated web or browser tool. shell can use network tools if they are installed (curl, git, ...).
- File tools refuse paths outside the allowed root. shell is not clamped to the root, but still runs with only your normal user rights.
- No UAC/elevation. Admin-only commands fail unless the harness itself was launched as admin.
- No undo and no recycle bin: deletes and overwrites are permanent.
- Shell calls are separate processes: cd, variables, and environment changes do not persist between calls.
- Each shell command is killed after its timeout (default {timeout}s, max {timeout_max}s).
- Interactive commands that wait for input or confirmation will hang until that timeout kills them; use non-interactive flags.
- Tool output is clipped to {clip} characters; read large files in chunks with offset/limit.
- Each task starts with no memory of earlier tasks; only the current task's conversation is available.
- You cannot ask the user a question mid-task. If blocked, report the blocker in finish()."""

RULES = """\
1. One tool call per reply.
2. Inspect before you modify: list, read, or search first.
3. Never guess file contents or invent command output.
4. If a command fails, read the error and change approach. Do not repeat the exact same call.
5. Quote paths that contain spaces. Prefer absolute paths.
6. For multi-step work, update the todo list and mark items done as you go.
7. Keep your reasoning short; the harness already trims tool output.
8. Call finish() when done, with what changed, where, and anything that failed.
9. Shell is Windows PowerShell 5.1, not bash: no &&, head, tail, grep, or ls. Use ; to chain, Select-Object -First/Last, Select-String, and Get-ChildItem.
10. For Desktop, Documents, Downloads, or Pictures, use the exact path from KNOWN USER FOLDERS. Never assume <root>\\Desktop.
11. Before finish(), verify the result: list the folder or read the file you changed, and include the exact paths in the summary.
12. To open an app, launch it; never create files for that. Find it with `Get-StartApps`, then run `Start-Process <AppID>` when the AppID is a path, or `explorer.exe shell:AppsFolder\\<AppID>` for a Store app ID. Launch once; do not inspect the app folder or read its .exe.
13. For "open <file/folder>" use `Invoke-Item <path>`; for a URL use `Start-Process <url>`. Do not modify anything for open-only tasks."""

PROMPT = """You are PC Manager, an autonomous agent controlling a Windows PC through tools.

Machine: {machine}
OS: {os_name}
Date: {date}
Allowed file root: {root}
Shell working directory: {root}

KNOWN USER FOLDERS (use these exact paths)
{folders}

You work in a loop: pick exactly one tool, wait for its result, then decide the
next step. When the task is done, call finish(summary). Never end a task without
calling finish.

WHAT YOU CAN DO
{capabilities}

WHAT YOU CANNOT DO
{limitations}

RULES
{rules}

TASK
{task}

TODO
{todo}

STEP {step}/{max_steps}"""


class ToolError(Exception):
    """A tool rejected its input; the message is fed back to the model."""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def clip(text: str, limit: int = MAX_TOOL_CHARS) -> str:
    s = str(text)
    if len(s) <= limit:
        return s
    head = limit * 2 // 3
    tail = limit - head
    return s[:head] + f"\n...[truncated {len(s) - limit} chars]...\n" + s[-tail:]


def parse_arguments(raw) -> dict:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError("arguments must be a JSON object")
    return raw


def safe_path(root: Path, value) -> Path:
    """Resolve a model-supplied path and keep it inside root."""
    text = str(value).strip().strip('"')
    if not text:
        raise ToolError("path is required")
    text = re.sub(r"\$\{?env:(\w+)\}?", lambda m: os.environ.get(m.group(1), m.group(0)), text)
    text = os.path.expandvars(os.path.expanduser(text))
    path = Path(text)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if path != root and root not in path.parents:
        raise ToolError(f"refusing path outside the allowed root {root}: {path}")
    return path


def render_todo(items: list[dict]) -> str:
    if not items:
        return "(empty)"
    marks = {"pending": "[ ]", "doing": "[>]", "done": "[x]"}
    return "\n".join(f"{marks[item['status']]} {item['task']}" for item in items)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


@lru_cache(maxsize=1)
def known_folders() -> dict[str, str]:
    """Real shell folder paths from Windows, so OneDrive redirection is respected."""
    found = {}
    try:
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        for name, folder_id in KNOWN_FOLDER_IDS.items():
            guid = GUID.from_buffer_copy(uuid.UUID(folder_id).bytes_le)
            path_ptr = ctypes.c_wchar_p()
            hr = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path_ptr))
            if hr == 0:
                try:
                    found[name] = str(Path(path_ptr.value))
                finally:
                    ole32.CoTaskMemFree(path_ptr)
    except (AttributeError, OSError):
        return {}
    return found


def folder_lines() -> str:
    folders = known_folders()
    return "\n".join(f"- {name}: {path}" for name, path in folders.items()) or "- (not available)"


def trim_history(messages: list[dict], keep_turns: int = KEEP_TURNS) -> None:
    """Drop the oldest assistant+tool groups, but keep system and task pinned."""
    cap = 2 + keep_turns * 2
    while len(messages) > cap:
        next_assistant = 3
        while next_assistant < len(messages) and messages[next_assistant].get("role") != "assistant":
            next_assistant += 1
        if next_assistant >= len(messages):
            break
        del messages[2:next_assistant]


def build_system_prompt(cfg, task: str, todo: list[dict], step: int) -> str:
    limitations = LIMITATIONS.format(
        timeout=SHELL_TIMEOUT, timeout_max=SHELL_TIMEOUT_MAX, clip=MAX_TOOL_CHARS
    )
    return PROMPT.format(
        machine=platform.node() or "this PC",
        os_name=f"{platform.system()} {platform.release()}",
        date=datetime.now().strftime("%Y-%m-%d %H:%M"),
        root=cfg.root,
        folders=folder_lines(),
        capabilities=CAPABILITIES,
        limitations=limitations,
        rules=RULES,
        task=task,
        todo=render_todo(todo),
        step=step,
        max_steps=cfg.max_steps,
    )


def size_str(path: Path) -> str:
    try:
        return f"{path.stat().st_size:,} bytes"
    except OSError:
        return "unknown size"


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
def tool_shell(h, args: dict) -> str:
    command = str(args.get("command", "")).strip()
    if not command:
        raise ToolError("command is required")
    try:
        timeout = int(args.get("timeout", SHELL_TIMEOUT))
    except (TypeError, ValueError):
        timeout = SHELL_TIMEOUT
    timeout = max(1, min(timeout, SHELL_TIMEOUT_MAX))

    # Force UTF-8 on the child's redirected output, then decode it as UTF-8.
    wrapped = "$OutputEncoding=[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + command
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", wrapped],
            cwd=str(h.cfg.root),
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
    path = safe_path(h.cfg.root, args.get("path", "."))
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
    path = safe_path(h.cfg.root, args.get("path", ""))
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
    limit = max(1, min(limit, 2000))
    if total and offset > total:
        raise ToolError(f"offset {offset} is past the end of the file ({total} lines)")

    chunk = lines[offset - 1: offset - 1 + limit]
    end = offset + len(chunk) - 1 if chunk else offset - 1
    header = f"{path} (lines {offset}-{end} of {total}):"
    return header + "\n" + ("\n".join(chunk) if chunk else "(empty file)")


def tool_write_file(h, args: dict) -> str:
    path = safe_path(h.cfg.root, args.get("path", ""))
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
    path = safe_path(h.cfg.root, args.get("path", ""))
    if not path.is_file():
        raise ToolError(f"not a file: {path}")
    old = args.get("old")
    new = args.get("new")
    if not isinstance(old, str) or not old:
        raise ToolError("old is required and must be non-empty (use write_file to create a file)")
    if not isinstance(new, str):
        raise ToolError("new is required (use an empty string to delete)")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ToolError(str(exc)) from exc
    count = text.count(old)
    if count != 1:
        raise ToolError(f"old text occurs {count} times in {path}; it must occur exactly once")
    try:
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
    except OSError as exc:
        raise ToolError(str(exc)) from exc
    return f"edited {path} (1 replacement)"


def tool_search_files(h, args: dict) -> str:
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ToolError("pattern is required")
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ToolError(f"invalid regex: {exc}") from exc

    base = safe_path(h.cfg.root, args.get("path", "."))
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
            if b"\x00" in raw[:4096]:       # binary file
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


def tool_finish(h, args: dict) -> str:
    summary = str(args.get("summary") or "").strip()
    return summary or "(no summary given)"


TOOL_FUNCS = {
    "shell": tool_shell,
    "list_dir": tool_list_dir,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "search_files": tool_search_files,
    "update_todo": tool_update_todo,
    "finish": tool_finish,
}

# Add/remove a tool here and in TOOL_FUNCS, then update CAPABILITIES above.
TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "shell",
        "description": "Run a PowerShell command and return exit code, stdout, and stderr. Each call is a new process starting in the allowed root, so cd does not persist (use absolute paths or 'Set-Location X; ...'). Runs as the current user, no elevation. Killed after timeout seconds.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "PowerShell command to run."},
            "timeout": {"type": "integer", "description": f"Seconds before the command is killed (default {SHELL_TIMEOUT}, max {SHELL_TIMEOUT_MAX})."},
        }, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List a directory inside the allowed root. Directories end with a path separator and files show their size.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory path, absolute or relative to the root. Defaults to the root."},
            "recursive": {"type": "boolean", "description": "Walk subdirectories (capped at 500 entries)."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a UTF-8 text file inside the allowed root. Use offset/limit for large files.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path, absolute or relative to the root."},
            "offset": {"type": "integer", "description": "1-based line to start at (default 1)."},
            "limit": {"type": "integer", "description": f"Maximum lines to return (default {READ_MAX_LINES})."},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite a text file inside the allowed root. Parent directories are created automatically. Overwrites are permanent.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path, absolute or relative to the root."},
            "content": {"type": "string", "description": "Full file content."},
        }, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace one exact string in a text file inside the allowed root. Fails if old occurs zero times or more than once.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path, absolute or relative to the root."},
            "old": {"type": "string", "description": "Exact text to replace (must occur exactly once)."},
            "new": {"type": "string", "description": "Replacement text; empty string deletes it."},
        }, "required": ["path", "old", "new"]},
    }},
    {"type": "function", "function": {
        "name": "search_files",
        "description": "Search file contents with a case-insensitive regular expression inside the allowed root. Text files up to 1 MB; skips .git, node_modules, AppData, venvs, and caches.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Regular expression to search for."},
            "path": {"type": "string", "description": "Directory to search, absolute or relative to the root. Defaults to the root."},
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
    {"type": "function", "function": {
        "name": "finish",
        "description": "Finish the task. Call this last with a short factual summary of what changed, where, and anything that failed.",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string", "description": "What was done (or the exact blocker)."},
        }, "required": ["summary"]},
    }},
]


def execute_tool(h, name: str, args: dict) -> str:
    func = TOOL_FUNCS.get(name)
    if func is None:
        return f"ERROR: unknown tool {name!r}. Available: {', '.join(TOOL_FUNCS)}"
    try:
        return clip(str(func(h, args)))
    except ToolError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:  # a broken tool must never kill the loop
        return f"ERROR: {type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

LOGO = (
    "██████╗  ██████╗",
    "██╔══██╗██╔════╝",
    "██████╔╝██║",
    "██╔═══╝ ██║",
    "██║     ╚██████╗",
    "╚═╝      ╚═════╝",
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
    "finish": "FINISH",
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


def args_summary(name: str, args: dict) -> str:
    def field(key: str) -> str:
        return one_line(args.get(key, ""))

    if name == "shell":
        return field("command") or "(no command)"
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
        return field("path") or "(no path)"
    if name == "search_files":
        text = f"/{field('pattern')}/"
        if args.get("glob"):
            text += f"  in {field('glob')}"
        return text + f"  under {field('path') or '.'}"
    if name == "update_todo":
        items = args.get("items") if isinstance(args.get("items"), list) else []
        return f"{len(items)} item{'s' if len(items) != 1 else ''}"
    if name == "finish":
        return field("summary") or "(no summary)"
    return one_line(json.dumps(args, ensure_ascii=False))


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
        # Wait first so sub-100ms operations never flash a frame.
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

    # -- paint ---------------------------------------------------------
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

    # -- layout --------------------------------------------------------
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

    def banner(self, cfg, session_path: Path) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d · %H:%M")
        info = [
            self.strong("M A N A G E R"),
            self.gray("AUTONOMOUS DESKTOP AGENT"),
            self.blue("─" * 36),
            self.spec("MODEL", cfg.model),
            self.spec("ROOT", str(cfg.root)),
            self.spec("SESSION", stamp),
            self.spec("LOG", shorten_left(str(session_path), 44)),
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
            + self.strong("/exit") + self.gray(" quits.") + "  "
            + self.strong("Ctrl+C") + self.gray(" aborts.")
        )

    def spec(self, label: str, value: str) -> str:
        return self.gray(label.ljust(9)) + "  " + self.strong(value)

    def step_prefix(self, number: int) -> str:
        return "  " + self.blue("STEP") + " " + self.strong(f"{number:02d}")

    def step(self, number: int, seconds: float) -> None:
        self.out(self.row(self.step_prefix(number), self.gray(fmt_time(seconds))))

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
        paint = self.blue if name == "finish" else self.strong
        left = self.gray(fmt_tool(name).ljust(7)) + "  " + paint(ellipsize(summary, max(16, room)))
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

    def done(self, summary: str, seconds: float, steps: int, tokens: int, load: float = 0.0,
             gap: bool = True) -> None:
        bits = [fmt_time(seconds), f"{steps} step{'s' if steps != 1 else ''}"]
        if tokens:
            bits.append(f"{tokens:,} tokens")
        if load >= 0.1:
            bits.append(f"load {fmt_time(load)}")
        if gap:
            self.out()
        self.out("  " + self.gray("─" * max(20, min(self.width - 4, 56))))
        self.out("  " + self.green("✓ DONE") + "    " + self.strong(" · ".join(bits)))
        lines = self._lines(summary)
        if summary.strip():
            for line in lines[:8]:
                self.out("    " + self.gray(line))
            if len(lines) > 8:
                self.out("    " + self.gray("…"))


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------
class Harness:
    def __init__(self, cfg, ui: UI):
        self.cfg = cfg
        self.ui = ui
        self.messages: list[dict] = []
        self.todo: list[dict] = []
        self.repeat_key: str | None = None
        self.repeat_count = 0
        self.nudges = 0
        self.session_path: Path | None = None
        self.log_fh = None
        self._open_log()

    def _open_log(self) -> None:
        directory = Path(self.cfg.session_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.session_path = directory / (datetime.now().strftime("%Y-%m-%dT%H-%M-%S") + ".jsonl")
        self.log_fh = open(self.session_path, "a", encoding="utf-8")
        self.log("start", model=self.cfg.model, api=self.cfg.api,
                 root=str(self.cfg.root), think=bool(self.cfg.think))

    def log(self, kind: str, **data) -> None:
        if not self.log_fh:
            return
        record = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind, **data}
        try:
            self.log_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.log_fh.flush()
        except OSError:
            pass

    def close(self) -> None:
        if self.log_fh:
            self.log_fh.close()
            self.log_fh = None

    def chat(self) -> tuple[dict, dict]:
        cfg = self.cfg
        payload = {
            "model": cfg.model,
            "messages": self.messages,
            "tools": TOOL_SCHEMAS,
            "stream": False,
            "think": bool(cfg.think),
            "options": {"temperature": 0.2, "num_ctx": 32768},
        }
        request = urllib.request.Request(
            cfg.api + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=MODEL_TIMEOUT) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            if exc.code == 404:
                raise RuntimeError(
                    f"Ollama does not have model {cfg.model!r}. Pull it first: ollama pull {cfg.model}"
                ) from exc
            raise RuntimeError(f"Ollama HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"cannot reach Ollama at {cfg.api} ({exc.reason}). Start it with: ollama serve"
            ) from exc
        message = data.get("message") or {}
        meta = {
            "eval_count": int(data.get("eval_count") or 0),
            "load_duration": float(data.get("load_duration") or 0) / 1e9,
        }
        return message, meta

    def run_task(self, task: str) -> None:
        ui = self.ui
        self.messages = [{"role": "system", "content": ""}, {"role": "user", "content": task}]
        self.todo = []
        self.repeat_key = None
        self.repeat_count = 0
        self.nudges = 0
        self.tokens = 0
        self.load_seconds = 0.0
        started = time.perf_counter()
        self.log("task", task=task)

        for step in range(1, self.cfg.max_steps + 1):
            self.messages[0]["content"] = build_system_prompt(self.cfg, task, self.todo, step)
            ui.out()
            try:
                with Spinner(ui, ui.step_prefix(step)) as spin:
                    message, meta = self.chat()
            except RuntimeError as exc:
                ui.error(str(exc))
                self.log("error", error=str(exc))
                return

            self.tokens += meta.get("eval_count", 0)
            if meta.get("load_duration") and not self.load_seconds:
                self.load_seconds = meta["load_duration"]

            thinking = (message.get("thinking") or "").strip()
            content = (message.get("content") or "").strip()
            calls = message.get("tool_calls") or []
            finish_only = (
                len(calls) == 1
                and ((calls[0].get("function") or {}).get("name") == "finish")
                and not thinking
                and not content
            )
            if not finish_only:
                ui.step(step, spin.elapsed)
            if thinking:
                ui.thought(thinking)
            if content and (thinking or calls):
                ui.say(content)
            self.log("assistant", step=step, content=content, thinking=thinking, tool_calls=calls)

            if not calls:
                if content and self.nudges >= 1:
                    ui.done(content, time.perf_counter() - started, step, self.tokens, self.load_seconds)
                    self.log("done", content=content)
                    return
                self.nudges += 1
                if self.nudges > 2:
                    ui.stop("the model keeps replying without calling finish()")
                    self.log("no_tool_calls")
                    return
                ui.warn("no tool call; asking the model to continue")
                self.messages.append({"role": "assistant", "content": content or "(no output)"})
                self.messages.append({
                    "role": "user",
                    "content": "You must call exactly one tool, or call finish(summary) when the task is complete.",
                })
                continue

            self.messages.append({"role": "assistant", "content": content, "tool_calls": calls})
            finished = False
            final_summary = ""

            for call in calls:
                function = call.get("function") or {}
                name = function.get("name") or ""
                try:
                    args = parse_arguments(function.get("arguments"))
                except ValueError as exc:
                    observation = f"ERROR: bad tool arguments: {exc}"
                    self.messages.append({"role": "tool", "tool_name": name or "unknown", "content": observation})
                    ui.tool(name or "unknown", "(bad arguments)")
                    ui.result(observation, error=True)
                    self.log("tool", name=name or "unknown", args={}, observation=observation)
                    continue

                key = name + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False)
                if key == self.repeat_key:
                    self.repeat_count += 1
                else:
                    self.repeat_key, self.repeat_count = key, 1

                seconds = 0.0
                summary = args_summary(name, args)
                if self.repeat_count >= 3:
                    observation = (
                        f"ERROR: {name} was already called with these exact arguments "
                        f"{self.repeat_count} times. Use a different approach."
                    )
                else:
                    with Spinner(ui, ui.tool_prefix(name, summary), "RUNNING") as spin:
                        observation = execute_tool(self, name, args)
                    seconds = spin.elapsed

                if name != "finish":
                    ui.tool(name, summary, seconds)
                self.log("tool", name=name, args=args, observation=observation)
                self.messages.append({"role": "tool", "tool_name": name, "content": observation})
                if name == "finish":
                    finished = True
                    final_summary = observation
                    break

                if observation.startswith("ERROR:"):
                    ui.result(observation, error=True)
                elif name == "update_todo":
                    ui.todo(self.todo)
                elif name == "edit_file":
                    ui.diff(args)
                else:
                    ui.result(observation)

            if finished:
                ui.done(final_summary, time.perf_counter() - started, step, self.tokens,
                        self.load_seconds, gap=not finish_only)
                self.log("done", content=final_summary)
                return
            trim_history(self.messages, KEEP_TURNS)

        ui.stop(f"max steps ({self.cfg.max_steps}) reached before finish()")
        self.log("max_steps")


# --------------------------------------------------------------------------
# Self-check (no model required)
# --------------------------------------------------------------------------
def self_check() -> None:
    root = Path.home().resolve()

    clipped = clip("a" * 5000 + "b" * 5000, 1000)
    assert clipped.startswith("a") and clipped.endswith("b") and len(clipped) <= 1200

    assert safe_path(root, "Documents") == (root / "Documents").resolve()
    for outside in (root.parent, Path("C:/Windows")):
        try:
            safe_path(root, outside)
        except ToolError:
            pass
        else:
            raise AssertionError(f"outside path allowed: {outside}")

    assert parse_arguments({"x": 1}) == {"x": 1}
    assert parse_arguments('{"x": 1}') == {"x": 1}
    try:
        parse_arguments("[1]")
    except ValueError:
        pass
    else:
        raise AssertionError("non-object arguments accepted")

    messages = [{"role": "system", "content": ""}, {"role": "user", "content": "task"}]
    for _ in range(20):
        messages.append({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "shell"}}]})
        messages.append({"role": "tool", "tool_name": "shell", "content": "ok"})
    trim_history(messages, 3)
    assert len(messages) <= 8
    assert [m["role"] for m in messages[:3]] == ["system", "user", "assistant"]

    schema_names = {tool["function"]["name"] for tool in TOOL_SCHEMAS}
    assert schema_names == set(TOOL_FUNCS), f"schema/function mismatch: {schema_names ^ set(TOOL_FUNCS)}"

    folders = known_folders()
    if os.name == "nt":
        assert "Desktop" in folders, f"Desktop not resolved: {folders}"
    prompt = build_system_prompt(argparse.Namespace(root=root, max_steps=MAX_STEPS), "task", [], 1)
    assert "KNOWN USER FOLDERS" in prompt
    for path in folders.values():
        assert path in prompt, f"folder missing from prompt: {path}"

    fake = argparse.Namespace(todo=[])
    tool_update_todo(fake, {"items": [{"task": "x", "status": "doing"}]})
    assert fake.todo[0]["status"] == "doing"
    try:
        tool_update_todo(fake, {"items": [{"task": "x", "status": "wat"}]})
    except ToolError:
        pass
    else:
        raise AssertionError("invalid todo status accepted")

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

    print("self-check OK")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    # Keep redirected output from crashing on non-cp1252 characters.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Autonomous PC manager driven by a local Ollama model.")
    parser.add_argument("--model", default=os.environ.get("PC_MANAGER_MODEL", DEFAULT_MODEL))
    parser.add_argument("--api", default=os.environ.get("PC_MANAGER_API", DEFAULT_API))
    parser.add_argument("--root", default=os.environ.get("PC_MANAGER_ROOT", str(Path.home())),
                        help="Directory the file tools may touch (default: user home).")
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS)
    parser.add_argument("--think", action="store_true", help="Let the model reason before each tool call (slower).")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                        help="Color: auto (real terminal only), always, never. NO_COLOR is respected.")
    parser.add_argument("--session-dir", default=str(Path(__file__).resolve().parent / "sessions"))
    parser.add_argument("--self-check", action="store_true", help="Run local sanity checks and exit.")
    args = parser.parse_args(argv)

    if args.self_check:
        self_check()
        return 0

    ui = UI(args.color)
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        ui.error(f"root is not a directory: {root}")
        return 2
    if args.max_steps < 1:
        ui.error("--max-steps must be at least 1")
        return 2
    args.root = root

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
            if task.lower() in {"/exit", "/quit"}:
                break
            try:
                harness.run_task(task)
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
