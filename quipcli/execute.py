import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .tools import _ask, _tty_stdin


def _run_attached(argv: list[str] | str, shell: bool = False) -> int:
    """Run a child on the terminal. When our stdin is a spent pipe
    (`git diff | qp -e …`) the child gets the terminal instead, so an
    interactive command or $EDITOR still works."""
    kwargs: dict = {"shell": True} if shell else {}
    tty = _tty_stdin()
    if tty is None:
        return subprocess.run(argv, check=False, **kwargs).returncode
    with tty:
        return subprocess.run(argv, check=False, stdin=tty, **kwargs).returncode


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
        _run_attached([*shlex.split(editor), tmpfile])
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
        _run_attached([*shlex.split(editor), tmpfile])
        return Path(tmpfile).read_text().strip()
    finally:
        os.unlink(tmpfile)


@dataclass
class _Outcome:
    """What became of a proposed command."""

    command: str  # the command as finally shown (after any edit/revision)
    status: int | None  # exit status, None when nothing ran
    edited: bool = False  # the user changed it in $EDITOR before running

    def note(self) -> str:
        """One line for the session history, so a follow-up (`qp -f why did
        it fail`) knows what actually happened."""
        if self.status is None:
            return "(The user did not run the command.)"
        if self.edited:
            return (
                f"(The user edited the command to `{self.command}` and ran it: "
                f"exit status {self.status}.)"
            )
        return f"(The user ran the command: exit status {self.status}.)"


def confirm_command(
    command: str,
    prompt: str,
    revise: Callable[[str], str] | None = None,
) -> _Outcome:
    """Show the command, ask [Y/n/e] (plus `r` when `revise` is given), run
    it on yes. `e` edits it in $EDITOR; `r` asks what to change and calls
    `revise(feedback)` for a new proposal from the model."""
    command = _strip_fences(command)
    choices = "[Y/n/e/r]" if revise else "[Y/n/e]"
    edited = False
    while True:
        if not command:
            print("Error: no command to run.", file=sys.stderr)
            return _Outcome(command, None, edited)
        print(f"\n\033[1;32m$ {command}\033[0m", file=sys.stderr)
        try:
            choice = _ask(f"Run this command? {choices} ").strip().lower()
            if choice == "r" and revise:
                feedback = _ask("What should change? ").strip()
                if feedback:
                    command = _strip_fences(revise(feedback))
                    edited = False
                continue
        except EOFError, KeyboardInterrupt:
            print("\nAborted.", file=sys.stderr)
            return _Outcome(command, None, edited)
        if choice in ("y", "yes", ""):
            return _Outcome(command, _run_attached(command, shell=True), edited)
        elif choice == "e":
            new = _edit_in_editor(command, prompt)
            edited = edited or new != command
            command = new
        else:
            print("Aborted.", file=sys.stderr)
            return _Outcome(command, None, edited)


def run_confirmed(command: str, prompt: str) -> int | None:
    """Show the command, ask [Y/n/e], run it on yes. Returns its exit status,
    or None when nothing ran (empty command, declined, Ctrl-C/Ctrl-D)."""
    return confirm_command(command, prompt).status


def confirm_and_run(command: str, prompt: str) -> None:
    """One-shot -e: like run_confirmed, then exit with the command's status
    (0 when declined, 1 when there was nothing to run)."""
    if not _strip_fences(command):
        print("Error: no command to run.", file=sys.stderr)
        sys.exit(1)
    status = run_confirmed(command, prompt)
    sys.exit(0 if status is None else status)
