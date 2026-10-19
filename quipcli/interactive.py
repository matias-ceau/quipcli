"""Interactive chat session (`qp -t`) on prompt_toolkit: a prompt loop over
the same run_turn() one-shot `qp` uses, plus a few /commands."""

import shlex
import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from prompt_toolkit.completion import (
    CompleteEvent,
    Completer,
    Completion,
    PathCompleter,
)
from prompt_toolkit.document import Document

from . import constants
from .config import _load_config
from .db import _resolve_session, _Session
from .http_client import _MarkdownAnsiRenderer, _use_markdown_rendering
from .models import (
    _check_modality_support,
    _check_reasoning_support,
    _load_models,
    _resolve_model_name,
)
from .multimodal import _build_user_content
from .turn import TurnOptions, _history_limit, _prompt_text, run_turn

_COMMANDS = {
    "/help": "show this help",
    "/model": "[name] switch model (fzf picker without a name)",
    "/reasoning": "[effort|default] set reasoning effort for this session",
    "/mode": "[chat|code|exec|agent] switch mode",
    "/attach": "PATH… attach files to the next message",
    "/new": "[name] start a new session (or resume/create one by name)",
    "/sessions": "switch to another session (fzf)",
    "/session": "show the current session",
    "/config": "edit the config (fzf)",
    "/exit": "quit (also Ctrl-D)",
}
_MODE_ALIASES = {
    "chat": "chat",
    "code": "code",
    "c": "code",
    "exec": "execute",
    "execute": "execute",
    "e": "execute",
    "agent": "agent",
    "a": "agent",
}
_PROMPTS = {"chat": "›", "code": "code›", "execute": "$›", "agent": "agent›"}
_DIM, _BOLD_CYAN, _RESET = "\033[2m", "\033[1;36m", "\033[0m"


@dataclass
class _State:
    opts: TurnOptions
    session: _Session
    attachments: list[str] = field(default_factory=list)
    effort: str | None = None  # effective reasoning effort, for the toolbar
    sent: bool = False  # a prompt went out → this session is now `qp -f`'s

    def toolbar(self) -> str:
        parts = [
            self.opts.model,
            f"reasoning: {self.effort or 'default'}",
            f"mode: {self.opts.mode}",
            f"session: {self.session.name or 'new'}",
        ]
        if self.attachments:
            parts.append(f"{len(self.attachments)} attachment(s)")
        return " " + " │ ".join(parts)


class _Completer(Completer):
    """Completes /commands, then their argument (models, efforts, modes, paths)."""

    def __init__(self) -> None:
        self._models: list[str] | None = None
        self._paths = PathCompleter(expanduser=True)

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        if " " not in text:
            for cmd, help_text in _COMMANDS.items():
                if cmd.startswith(text):
                    yield Completion(cmd, -len(text), display_meta=help_text)
            return
        cmd, _, arg = text.partition(" ")
        if cmd == "/attach":
            word = arg.rsplit(" ", 1)[-1]
            yield from self._paths.get_completions(Document(word), complete_event)
            return
        if cmd == "/model":
            if self._models is None:
                self._models = _load_models()
            options = self._models
        elif cmd == "/reasoning":
            options = [*constants.REASONING_EFFORTS, "default"]
        elif cmd == "/mode":
            options = ["chat", "code", "exec", "agent"]
        else:
            return
        for option in options:
            if arg.lower() in option.lower():
                yield Completion(option, -len(arg))


def _print_message(role: str, content: str | list) -> None:
    text = _prompt_text(content) if isinstance(content, list) else content
    if role == "user":
        print(f"{_BOLD_CYAN}› {text}{_RESET}")
        return
    if _use_markdown_rendering():
        renderer = _MarkdownAnsiRenderer()
        print(renderer.render(text) + renderer.finish())
    else:
        print(text)


def _print_banner(state: _State) -> None:
    session = state.session
    where = session.label if not session.is_new or session.name else "new session"
    print(
        f"{_DIM}qp · {state.opts.model} · {where} — /help for commands, "
        f"Alt-Enter for a newline, Ctrl-D to quit{_RESET}"
    )
    recent = session.messages[-6:]
    if recent:
        hidden = len(session.messages) - len(recent)
        if hidden:
            print(f"{_DIM}… {hidden} earlier message(s){_RESET}")
        for message in recent:
            _print_message(message["role"], message["content"])
        print()


def _switch_session(state: _State, ref: str | None) -> None:
    state.session = _resolve_session(
        ref, False, quiet=True, history_limit=_history_limit(_load_config())
    )
    print(f"{_DIM}session: {state.session.label if ref else 'new'}{_RESET}")
    if state.session.messages:
        _print_banner(state)


def _command(state: _State, line: str) -> bool:
    """Run a /command. Returns False to leave the session."""
    cmd, _, arg = line.partition(" ")
    arg = arg.strip()
    opts = state.opts
    if cmd in ("/exit", "/quit", "/q"):
        return False
    if cmd == "/help":
        width = max(len(c) for c in _COMMANDS)
        for name, help_text in _COMMANDS.items():
            print(f"  {name:{width}}  {_DIM}{help_text}{_RESET}")
    elif cmd == "/model":
        if arg:
            opts.model = _resolve_model_name(arg)
        elif shutil.which("fzf"):
            from .tui import pick_model_interactive

            opts.model = pick_model_interactive() or opts.model
        else:
            print("Usage: /model NAME  (fzf not installed for the picker)")
        print(f"{_DIM}model: {opts.model}{_RESET}")
        if state.effort:
            _check_reasoning_support(opts.model, state.effort)
    elif cmd == "/reasoning":
        if not arg:
            print(f"{_DIM}reasoning: {state.effort or 'default'}{_RESET}")
        elif arg == "default":
            opts.reasoning = None
            state.effort = _load_config().get("reasoning_effort")
        elif arg in constants.REASONING_EFFORTS:
            opts.reasoning = state.effort = arg
            _check_reasoning_support(opts.model, arg)
        else:
            print(f"Unknown effort {arg!r}: {', '.join(constants.REASONING_EFFORTS)}")
    elif cmd == "/mode":
        mode = _MODE_ALIASES.get(arg)
        if mode is None:
            print(f"{_DIM}mode: {opts.mode} (choose: chat, code, exec, agent){_RESET}")
        else:
            opts.mode = mode
    elif cmd == "/attach":
        for raw in shlex.split(arg):
            path = Path(raw).expanduser()
            if path.is_file():
                state.attachments.append(str(path))
            else:
                print(f"Not a file: {raw}")
    elif cmd == "/new":
        _switch_session(state, arg or None)
    elif cmd == "/session":
        count = len(state.session.messages)
        name = state.session.label if state.session.name else "new (unsaved)"
        print(f"{_DIM}session: {name} · {count} message(s) loaded{_RESET}")
    elif cmd == "/sessions":
        from .tui import sessions_view

        picked = sessions_view()
        if picked:
            _switch_session(state, picked[1])
    elif cmd == "/config":
        from .tui import _config_view, _require_fzf

        _require_fzf()
        _config_view()
    else:
        print(f"Unknown command {cmd} — /help lists them.")
    return True


def _send(state: _State, text: str) -> None:
    if state.attachments:
        content, mods = _build_user_content([text], state.attachments, None)
        state.attachments = []
        _check_modality_support(state.opts.model, mods)
    else:
        content = text
    run_turn(
        state.opts, state.session, content, _prompt_text(content), interactive=True
    )
    state.sent = True


def run_interactive(opts: TurnOptions, session: _Session) -> None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("Error: the interactive session needs a terminal.", file=sys.stderr)
        sys.exit(1)
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings

    opts.show_stats = not opts.quiet
    state = _State(
        opts, session, effort=opts.reasoning or _load_config().get("reasoning_effort")
    )
    bindings = KeyBindings()

    @bindings.add("escape", "enter")  # Alt-Enter: newline instead of send
    def _newline(event) -> None:
        event.current_buffer.insert_text("\n")

    history = None
    try:
        constants._DATA_DIR.mkdir(parents=True, exist_ok=True)
        history = FileHistory(str(constants._DATA_DIR / "prompt_history"))
    except OSError:
        pass
    prompt = PromptSession(
        history=history,
        key_bindings=bindings,
        completer=_Completer(),
        complete_while_typing=True,
        bottom_toolbar=state.toolbar,
    )
    _print_banner(state)
    while True:
        try:
            line = prompt.prompt(
                ANSI(f"{_BOLD_CYAN}{_PROMPTS[state.opts.mode]}{_RESET} ")
            ).strip()
        except KeyboardInterrupt:
            continue  # Ctrl-C clears the line, Ctrl-D quits
        except EOFError:
            break
        if not line:
            continue
        try:
            if line.startswith("/"):
                if not _command(state, line):
                    break
            else:
                _send(state, line)
        except KeyboardInterrupt:
            print(f"{_RESET}\nInterrupted.", file=sys.stderr)
        except SystemExit:
            pass  # an error was already printed (API error, ambiguous model…)
    if state.sent:
        print(f"{_DIM}session: {state.session.label} — resume with qp -f{_RESET}")
