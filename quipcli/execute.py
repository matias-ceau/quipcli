import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from .tools import _ask


def _strip_fences(command: str) -> str:
    command = command.strip()
    if command.startswith("```"):
        # Drop the whole opening fence line, whatever its info string
        # (bash, sh, zsh, shell, console, fish…).
        _, _, command = command.partition("\n")
    command = command.strip().removesuffix("```").strip()
    # A single "$ cmd" prompt-style line is still just cmd.
    if "\n" not in command and command.startswith("$ "):
        command = command[2:]
    return command


def _edit_in_editor(command: str, prompt: str) -> str:
    editor = os.environ.get("EDITOR", "vi")
    sep = "─" * 48
    prompt_lines = "\n".join(f"# {l}" for l in prompt.splitlines()) or "# (empty)"
    header = (
        f"# Prompt:\n{prompt_lines}\n"
        f"# {sep}\n"
        f"# Edit the command below. Lines starting with # are ignored.\n\n"
    )
    with tempfile.NamedTemporaryFile(suffix=".sh", mode="w", delete=False) as f:
        f.write(header + command + "\n")
        tmpfile = f.name
    try:
        subprocess.run([*shlex.split(editor), tmpfile], check=False)
        lines = Path(tmpfile).read_text().splitlines()
        return "\n".join(l for l in lines if not l.startswith("#")).strip()
    finally:
        os.unlink(tmpfile)


def _edit_text_value(current: str) -> str:
    """Open $EDITOR on a plain text value (no comment header/stripping —
    unlike _edit_in_editor, which is specific to shell-command editing)."""
    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
        f.write(current)
        tmpfile = f.name
    try:
        subprocess.run([*shlex.split(editor), tmpfile], check=False)
        return Path(tmpfile).read_text().strip()
    finally:
        os.unlink(tmpfile)


def confirm_and_run(command: str, prompt: str) -> None:
    command = _strip_fences(command)
    while True:
        if not command:
            print("Error: no command to run.", file=sys.stderr)
            sys.exit(1)
        print(f"\n\033[1;32m$ {command}\033[0m", file=sys.stderr)
        try:
            choice = _ask("Run this command? [Y/n/e] ").strip().lower()
        except EOFError, KeyboardInterrupt:
            print("\nAborted.", file=sys.stderr)
            sys.exit(0)
        if choice in ("y", "yes", ""):
            sys.exit(subprocess.run(command, shell=True, check=False).returncode)
        elif choice == "e":
            command = _edit_in_editor(command, prompt)
        else:
            print("Aborted.", file=sys.stderr)
            sys.exit(0)
