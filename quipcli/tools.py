"""Local tools for --agent's tool-calling loop, plus OpenRouter's hosted
server tools (executed entirely on OpenRouter's side — see server_tools()).

Local tools mirror execute.py's confirm-before-running UX so agent mode
never runs a command or touches a file without an explicit [Y/n]."""

import json
import subprocess
import sys
from pathlib import Path

_MAX_OUTPUT_CHARS = 4000


def _confirm(prompt: str) -> bool:
    try:
        choice = input(prompt).strip().lower()
    except EOFError, KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return False
    return choice in ("y", "yes", "")


def _truncate(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def run_shell(args: dict) -> str:
    command = args.get("command", "")
    print(f"\n\033[1;32m$ {command}\033[0m", file=sys.stderr)
    if not _confirm("Run this command? [Y/n] "):
        return "User declined to run this command."
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 120s."
    output = (result.stdout + result.stderr).strip()
    output = _truncate(output)
    return output or f"(exit code {result.returncode}, no output)"


def read_file(args: dict) -> str:
    path = Path(args.get("path", "")).expanduser()
    print(f"\n\033[2m→ reading: {path}\033[0m", file=sys.stderr)
    try:
        text = path.read_text(errors="replace")
    except OSError as e:
        return f"Error reading file: {e}"
    return _truncate(text, _MAX_OUTPUT_CHARS * 4)


def write_file(args: dict) -> str:
    path = Path(args.get("path", "")).expanduser()
    content = args.get("content", "")
    print(f"\n\033[1;33m→ write:\033[0m {path} ({len(content)} chars)", file=sys.stderr)
    if not _confirm("Write this file? [Y/n] "):
        return "User declined to write this file."
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    except OSError as e:
        return f"Error writing file: {e}"
    return f"Wrote {len(content)} characters to {path}"


_LOCAL_TOOLS = {
    "run_shell": run_shell,
    "read_file": read_file,
    "write_file": write_file,
}

_LOCAL_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "Run a shell command on the user's machine and return its "
                "stdout/stderr. Requires the user's explicit confirmation "
                "before running — the call may be declined."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a local text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file, absolute or relative to the cwd.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write text content to a local file, creating parent "
                "directories as needed. Requires the user's explicit "
                "confirmation before writing — the call may be declined."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to write to."},
                    "content": {
                        "type": "string",
                        "description": "Full text content to write.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
]

# Hosted server tools — executed by OpenRouter itself, never surfaced to us
# as a tool_call we need to run. See docs/guides/features/server-tools.
_SERVER_TOOL_SCHEMAS = [
    {"type": "openrouter:web_search"},
    {"type": "openrouter:web_fetch"},
]


def default_tools(include_server_tools: bool = True) -> list[dict]:
    tools = list(_LOCAL_TOOL_SCHEMAS)
    if include_server_tools:
        tools += _SERVER_TOOL_SCHEMAS
    return tools


def execute_tool_call(name: str, arguments_json: str) -> str:
    fn = _LOCAL_TOOLS.get(name)
    if fn is None:
        return f"Error: unknown tool '{name}'."
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError:
        return "Error: invalid arguments JSON."
    try:
        return fn(args)
    except Exception as e:  # noqa: BLE001 — any tool failure must become a message, not crash the agent loop
        return f"Error executing tool: {e}"
