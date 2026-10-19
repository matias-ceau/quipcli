import os
import shutil
import sys
import time

from . import constants
from .agent import run_agent_loop
from .cli import _print_stats, build_parser, get_content
from .config import (
    _ensure_config,
    _load_config,
    _model_source,
    _resolve_default_model,
    _save_config,
    _seed_defaults,
)
from .context import _machine_context
from .db import (
    _current_session_id,
    _list_sessions,
    _record_message,
    _record_usage,
    _resolve_session,
    _save_session,
    _session_info,
)
from .execute import confirm_and_run
from .http_client import call_llm_streaming
from .models import (
    _check_modality_support,
    _check_reasoning_support,
    _fetch_models,
    _fetch_rankings,
    _list_models_by_modality,
    _load_models,
    _maybe_update_models_bg,
    _resolve_model_name,
)

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


def _color_enabled() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _do_models(in_filter: str | None, out_filter: str | None) -> None:
    in_mods = [x.strip() for x in in_filter.split(",")] if in_filter else None
    out_mods = [x.strip() for x in out_filter.split(",")] if out_filter else None

    if in_mods or out_mods:
        models = _list_models_by_modality(in_mods, out_mods)
        if not models:
            print("Error: no models match the given modality filters.", file=sys.stderr)
            sys.exit(1)
    else:
        models = _load_models()
        if not models:
            print("Error: no model cache — run: qp --update-models", file=sys.stderr)
            sys.exit(1)

    marker = "\033[1;32m*\033[0m" if _color_enabled() else "*"
    current = _resolve_default_model()
    for m in models:
        print(f"{marker} {m}" if m == current else f"  {m}")


def _do_model_get() -> None:
    model = _resolve_default_model()
    name = f"\033[1m{model}\033[0m" if _color_enabled() else model
    print(f"{name}  ({_model_source()})")


def _do_model_set(model: str) -> None:
    if not model:
        import shutil

        if shutil.which("fzf"):
            from .tui import pick_model_interactive

            picked = pick_model_interactive()
            if picked is None:
                print("Aborted.", file=sys.stderr)
                return
            model = picked
        else:
            models = _load_models()
            if not models:
                print(
                    "Error: no model cache — run: qp --update-models", file=sys.stderr
                )
                sys.exit(1)
            marker = "\033[1;32m*\033[0m" if _color_enabled() else "*"
            current = _resolve_default_model()
            for i, m in enumerate(models, 1):
                prefix = marker if m == current else " "
                print(f"{prefix} {i:3d}  {m}")
            try:
                choice = input("Select model number or name: ").strip()
            except EOFError, KeyboardInterrupt:
                print("\nAborted.", file=sys.stderr)
                sys.exit(0)
            if choice.isdigit() and 1 <= int(choice) <= len(models):
                model = models[int(choice) - 1]
            else:
                model = _resolve_model_name(choice)
    else:
        model = _resolve_model_name(model)
    cfg = _load_config()
    cfg["default_model"] = model
    _save_config(cfg)
    print(f"Default model set to: {model}")


def _do_config_edit() -> None:
    import shlex
    import subprocess

    editor = os.environ.get("EDITOR", "vi")
    subprocess.run([*shlex.split(editor), str(constants._CONFIG_FILE)], check=False)


def _do_status() -> None:
    cfg = _load_config()
    model = _resolve_default_model()
    n_models = len(_load_models())
    print(f"model         : {model}  ({_model_source()})")
    print(f"api_url       : {constants._API_URL}")
    key = constants._API_KEY
    if not key:
        key_display = "(not set)"
    elif len(key) > 16:
        key_display = f"{key[:8]}…{key[-4:]}"
    else:
        key_display = "(set)"
    print(f"api_key       : {key_display}")
    print(f"models cached : {n_models} models  ({constants._MODELS_CACHE})")
    print(
        f"config file   : {constants._CONFIG_FILE}  ({'exists' if constants._CONFIG_FILE.exists() else 'not created'})"
    )
    print(f"reasoning     : {cfg.get('reasoning_effort') or '(model default)'}")
    print(f"system_prompt : {cfg.get('system_prompt') or '(none)'}")
    sid = _current_session_id()
    info = _session_info(sid) if sid else None
    session_display = f"{info[0]} ({sid}) · {info[1]} messages" if info else "(none)"
    print(f"session       : {session_display}  (continue with -f)")
    print(f"machine ctx   : {_machine_context()}")
    print(
        f"history db    : {constants._HISTORY_DB}  ({'exists' if constants._HISTORY_DB.exists() else 'not created'})"
    )


def _age(ts: float) -> str:
    secs = max(0, int(time.time() - ts))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit}"
    return f"{secs}s"


def _do_sessions() -> None:
    sessions = _list_sessions()
    if not sessions:
        print("No sessions yet.")
        return
    current = _current_session_id()
    name_w = min(28, max(len(s["name"]) for s in sessions))
    width = shutil.get_terminal_size((100, 20)).columns
    prompt_w = max(10, width - name_w - 26)
    color = _color_enabled()
    print(f"  {'id':6}  {'name':{name_w}}  {'age':>4}  {'msgs':>4}  first prompt")
    for s in sessions:
        prompt = " ".join(s["first_prompt"].split())
        if len(prompt) > prompt_w:
            prompt = prompt[: prompt_w - 1] + "…"
        mark = "●" if s["id"] == current else " "
        line = (
            f"{mark} {s['id']:6}  {s['name'][:name_w]:{name_w}}  "
            f"{_age(s['updated']):>4}  {s['messages']:>4}  {prompt}"
        )
        if color and s["id"] == current:
            line = f"\033[32m{line}\033[0m"
        print(line)


def _do_cost(period: str) -> None:
    if period not in ("1d", "7d", "30d", "all"):
        print(
            f"Error: invalid --cost period {period!r} (use 1d, 7d, 30d, or all).",
            file=sys.stderr,
        )
        sys.exit(1)

    from .db import _cost_summary

    days = {"1d": 1, "7d": 7, "30d": 30, "all": 0}[period]
    s = _cost_summary(days)

    if not s or s["requests"] == 0:
        print(f"No history for period: {period}")
        return

    total_tok = s["prompt_tokens"] + s["completion_tokens"]
    cost_str = f"${s['cost_usd']:.4f}" if s["cost_usd"] else "n/a"

    print(f"Period      : {period}")
    print(f"Requests    : {s['requests']}")
    print(
        f"Tokens      : {total_tok:,}  (prompt: {s['prompt_tokens']:,} / completion: {s['completion_tokens']:,})"
    )
    print(f"Cost        : {cost_str}")


def _reasoning_extra(cli_effort: str | None, cfg: dict) -> dict | None:
    effort = cli_effort or cfg.get("reasoning_effort")
    return {"reasoning": {"effort": effort}} if effort else None


def main() -> None:
    try:
        _main()
    except KeyboardInterrupt:
        # Close any dim/markdown ANSI style left open mid-stream, then exit
        # with the conventional SIGINT status instead of a traceback.
        if sys.stdout.isatty():
            print("\033[0m", end="", flush=True)
        if sys.stderr.isatty():
            print("\033[0m", end="", file=sys.stderr)
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


def _main() -> None:
    parser = build_parser()

    try:
        import argcomplete

        argcomplete.autocomplete(parser)  # exits immediately if completing
    except ImportError:
        pass

    _maybe_update_models_bg()  # fire-and-forget, no impact on startup time
    _ensure_config()  # creates ~/.config/quipcli/config.json on first run
    _seed_defaults(
        {f"{mode}_system_prompt": text for mode, text in _MODE_PROMPT_DEFAULTS.items()}
    )

    args = parser.parse_args()

    # --model-set/--cost use nargs="?", so argparse greedily consumes the next
    # bare word as their value even when it was meant to start a prompt (e.g.
    # `qp --model-set list all my files`). Both flags always exit without
    # touching `words`, so leftover words alongside a consumed value can only
    # mean this ambiguity was hit, not a real intent to combine the two.
    for flag, value in (("--model-set", args.model_set), ("--cost", args.cost)):
        if value is not None and args.words:
            print(
                f"Error: {flag} greedily took {value!r} as its value, leaving "
                f"unrelated words: {' '.join(args.words)!r}. "
                f"Use {flag}={value!r} to be explicit, or run {flag} on its own.",
                file=sys.stderr,
            )
            sys.exit(1)

    # --in/--out only mean anything alongside --models; without it they parse
    # fine but silently do nothing.
    if (args.in_filter or args.out_filter) and not args.models:
        print("Error: --in/--out require --models.", file=sys.stderr)
        sys.exit(1)

    # Hidden flags used internally by --tui's fzf preview/reload — must stay
    # fast (cache-only, no network) since fzf calls them on every keystroke.
    if args.tui_model_info is not None:
        from .tui import _print_model_info

        _print_model_info(args.tui_model_info)
        return

    if args.tui_list_models:
        from .tui import _model_lines

        print("\n".join(_model_lines()))
        return

    if args.tui_config_lines:
        from .tui import _config_lines

        print("\n".join(_config_lines(_load_config())))
        return

    if args.tldr:
        from .docs import _TLDR

        print(_TLDR)
        return

    if args.docs:
        from .docs import _DOCS

        print(_DOCS)
        return

    if args.update_models:
        models = _fetch_models()
        print(
            f"Cached {len(models)} models → {constants._MODELS_CACHE}"
            if models
            else "No models returned."
        )
        return

    if args.update_rankings:
        ranked = _fetch_rankings()
        print(
            f"Cached usage ranking for {len(ranked)} models → {constants._RANKINGS_CACHE}"
            if ranked
            else "No ranking data returned."
        )
        return

    if args.models:
        _do_models(args.in_filter, args.out_filter)
        return

    if args.model_get:
        _do_model_get()
        return

    if args.model_set is not None:
        _do_model_set(args.model_set)
        return

    if args.config_edit:
        _do_config_edit()
        return

    if args.status:
        _do_status()
        return

    if args.sessions:
        _do_sessions()
        return

    if args.cost is not None:
        _do_cost(args.cost)
        return

    if args.tui:
        from .tui import run_tui

        run_tui()
        return

    resolved_model = _resolve_model_name(args.model)
    if resolved_model != args.model and not args.quiet:
        print(f"\033[2mModel: {resolved_model}\033[0m", file=sys.stderr)
    args.model = resolved_model

    history_limit = _load_config().get("session_history", constants.SESSION_HISTORY)
    session = _resolve_session(
        args.session,
        args.follow_up,
        args.quiet,
        history_limit=history_limit if isinstance(history_limit, int) else None,
    )
    ctx_messages = session.messages
    user_content, detected_mods = get_content(args)

    # Prompt text for display / execute mode system prompt
    if isinstance(user_content, str):
        prompt_text = user_content
    else:
        prompt_text = " ".join(
            p["text"]
            for p in user_content
            if isinstance(p, dict) and p.get("type") == "text"
        )

    _check_modality_support(args.model, detected_mods)

    show_stats = not args.quiet and sys.stdout.isatty()
    extra = _reasoning_extra(args.reasoning, _load_config())
    if extra:
        _check_reasoning_support(args.model, extra["reasoning"]["effort"])

    def _default_system(mode: str) -> str | None:
        cfg = _load_config()
        parts = [
            p
            for p in (
                _mode_prompt(cfg, mode),
                _machine_context(),
                cfg.get("system_prompt"),
            )
            if p
        ]
        return "\n\n".join(parts) if parts else None

    def _build_messages(system: str | None) -> list[dict]:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(ctx_messages)
        msgs.append({"role": "user", "content": user_content})
        return msgs

    def _post(response_text: str, stats, mode: str) -> None:
        if stats:
            _record_usage(stats, mode)
        _save_session(session, prompt_text)
        if show_stats:
            _print_stats(stats, session.name)
        _record_message(session.id, "user", user_content, None, None, mode)
        _record_message(
            session.id,
            "assistant",
            response_text,
            stats.model if stats else args.model,
            stats,
            mode,
        )

    if args.execute:
        msgs = _build_messages(args.system or _default_system("execute"))
        # Streamed (not captured) so a reasoning model shows its thinking
        # instead of sitting silent; the command itself is only shown in
        # confirm_and_run's prompt.
        cmd, stats = call_llm_streaming(
            msgs,
            args.model,
            collect_usage=True,
            render_markdown=False,
            show_reasoning=not args.quiet,
            extra=extra,
            echo=False,
        )
        cmd = cmd.strip()
        _post(cmd, stats, "execute")
        confirm_and_run(cmd, prompt_text)
    elif args.agent:
        msgs = _build_messages(args.system or _default_system("agent"))
        text, stats = run_agent_loop(
            msgs,
            args.model,
            max_steps=args.max_steps,
            server_tools=not args.no_web,
            extra=extra,
            show_progress=not args.quiet,
        )
        _post(text, stats, "agent")
    elif args.code:
        msgs = _build_messages(args.system or _default_system("code"))
        text, stats = call_llm_streaming(
            msgs,
            args.model,
            collect_usage=True,
            render_markdown=False,
            show_reasoning=not args.quiet,
            extra=extra,
        )
        _post(text, stats, "code")
    else:
        msgs = _build_messages(args.system or _default_system("chat"))
        text, stats = call_llm_streaming(
            msgs,
            args.model,
            collect_usage=True,
            render_markdown=True,
            show_reasoning=not args.quiet,
            extra=extra,
        )
        _post(text, stats, "chat")
