"""One prompt → one reply, in any mode — shared by one-shot `qp …` and the
interactive session (`-t`)."""

import os
from dataclasses import dataclass

from . import constants
from .agent import run_agent_loop
from .cli import _print_stats
from .config import _load_config
from .context import _machine_context
from .db import _record_message, _record_usage, _save_session, _Session, _trim_history
from .execute import confirm_and_run, run_confirmed
from .http_client import call_llm_streaming

MODES = ("chat", "code", "execute", "agent")

# Fallbacks used when a mode's config key is missing/blank — see _mode_prompt.
_MODE_PROMPT_DEFAULTS = {
    "chat": constants.DEFAULT_CHAT_SYSTEM_PROMPT,
    "execute": constants.DEFAULT_EXECUTE_SYSTEM_PROMPT,
    "code": constants.CODE_SYSTEM_PROMPT,
    "agent": constants.DEFAULT_AGENT_SYSTEM_PROMPT,
}


def _mode_prompt(cfg: dict, mode: str) -> str:
    template = cfg.get(f"{mode}_system_prompt") or _MODE_PROMPT_DEFAULTS[mode]
    if "{shell}" in template:
        shell = os.path.basename(os.environ.get("SHELL", "/bin/bash"))
        template = template.replace("{shell}", shell)
    return template


def _reasoning_extra(cli_effort: str | None, cfg: dict) -> dict | None:
    effort = cli_effort or cfg.get("reasoning_effort")
    return {"reasoning": {"effort": effort}} if effort else None


def _history_limit(cfg: dict) -> int | None:
    value = cfg.get("session_history", constants.SESSION_HISTORY)
    return value if isinstance(value, int) else constants.SESSION_HISTORY


@dataclass
class TurnOptions:
    model: str
    mode: str = "chat"
    reasoning: str | None = None  # -r; None → config, then model default
    system: str | None = None  # -P: replaces the whole default system prompt
    max_steps: int = 12
    web: bool = True
    quiet: bool = False
    show_stats: bool = True


def _default_system(cfg: dict, mode: str) -> str | None:
    parts = [
        p
        for p in (_mode_prompt(cfg, mode), _machine_context(), cfg.get("system_prompt"))
        if p
    ]
    return "\n\n".join(parts) if parts else None


def run_turn(
    opts: TurnOptions,
    session: _Session,
    user_content: str | list[dict],
    prompt_text: str,
    interactive: bool = False,
) -> str:
    """Send one prompt with the session's history, record both messages and
    append them to `session.messages`. In execute mode, then confirm + run the
    command (one-shot: exit with its status; interactive: just return)."""
    cfg = _load_config()
    extra = _reasoning_extra(opts.reasoning, cfg)
    system = opts.system or _default_system(cfg, opts.mode)
    msgs: list[dict] = [{"role": "system", "content": system}] if system else []
    msgs += _trim_history(session.messages, _history_limit(cfg))
    msgs.append({"role": "user", "content": user_content})

    if opts.mode == "agent":
        text, stats = run_agent_loop(
            msgs,
            opts.model,
            max_steps=opts.max_steps,
            server_tools=opts.web,
            extra=extra,
            show_progress=not opts.quiet,
        )
    else:
        # -e is streamed too (not captured) so a reasoning model shows its
        # thinking; the command itself only appears in the confirm prompt.
        text, stats = call_llm_streaming(
            msgs,
            opts.model,
            collect_usage=True,
            render_markdown=opts.mode == "chat",
            show_reasoning=not opts.quiet,
            extra=extra,
            echo=opts.mode != "execute",
        )
        if opts.mode == "execute":
            text = text.strip()

    if stats:
        _record_usage(stats, opts.mode)
    _save_session(session, prompt_text)
    if opts.show_stats:
        _print_stats(stats, session.name)
    served = stats.model if stats else opts.model
    _record_message(session.id, "user", user_content, None, None, opts.mode)
    _record_message(session.id, "assistant", text, served, stats, opts.mode)
    session.messages += [
        {"role": "user", "content": user_content},
        {"role": "assistant", "content": text},
    ]

    if opts.mode == "execute":
        if interactive:
            run_confirmed(text, prompt_text)
        else:
            confirm_and_run(text, prompt_text)
    return text


def _prompt_text(user_content: str | list[dict]) -> str:
    if isinstance(user_content, str):
        return user_content
    return " ".join(
        p["text"]
        for p in user_content
        if isinstance(p, dict) and p.get("type") == "text"
    )


def _mode_from_flags(execute: bool, code: bool, agent: bool) -> str:
    return "execute" if execute else "code" if code else "agent" if agent else "chat"
