"""Tab completion for the interactive session's /commands — kept apart from
interactive.py so that importing quipcli doesn't import prompt_toolkit."""

from collections.abc import Iterable

from prompt_toolkit.completion import (
    CompleteEvent,
    Completer,
    Completion,
    PathCompleter,
)
from prompt_toolkit.document import Document

from . import constants
from .interactive import _COMMANDS
from .models import _load_models


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
