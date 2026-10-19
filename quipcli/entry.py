import os
import shutil
import sys

from . import constants
from .cli import build_parser, get_content
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
    _age,
    _current_session_id,
    _list_sessions,
    _resolve_session,
    _session_info,
    _set_current_session,
)
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
from .turn import (
    _MODE_PROMPT_DEFAULTS,
    TurnOptions,
    _history_limit,
    _mode_from_flags,
    _prompt_text,
    _reasoning_extra,
    run_turn,
)


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

    if args.tui_session_lines is not None:
        from .tui import _session_lines

        print("\n".join(_session_lines(args.tui_session_lines)))
        return

    if args.tui_session_preview is not None:
        from .tui import _print_session_preview

        _print_session_preview(*args.tui_session_preview)
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
        if args.tui:
            from .tui import _config_view, _require_fzf

            _require_fzf()
            _config_view()
        else:
            _do_config_edit()
        return

    if args.status:
        _do_status()
        return

    if args.sessions and args.tui:
        from .tui import sessions_view

        picked = sessions_view()
        if picked is None:
            return
        key, sid = picked
        if key != "alt-enter":
            _set_current_session(sid)
            info = _session_info(sid)
            name = info[0] if info else sid
            print(f"Current session: {name} ({sid}) — continue with: qp -f …")
            return
        args.session, args.words = sid, []  # alt-enter: open it interactively
    elif args.sessions:
        _do_sessions()
        return

    if args.cost is not None:
        _do_cost(args.cost)
        return

    resolved_model = _resolve_model_name(args.model)
    if resolved_model != args.model and not args.quiet:
        print(f"\033[2mModel: {resolved_model}\033[0m", file=sys.stderr)
    args.model = resolved_model

    cfg = _load_config()
    extra = _reasoning_extra(args.reasoning, cfg)
    if extra:
        _check_reasoning_support(args.model, extra["reasoning"]["effort"])
    opts = TurnOptions(
        model=args.model,
        mode=_mode_from_flags(args.execute, args.code, args.agent),
        reasoning=args.reasoning,
        system=args.system,
        max_steps=args.max_steps,
        web=not args.no_web,
        quiet=args.quiet,
        show_stats=not args.quiet and sys.stdout.isatty(),
    )

    # `qp -t [name]`, or a bare `qp` / `qp -f` / `qp -e` on a terminal with
    # nothing to send: interactive session. With -t, words name the session.
    no_prompt = not args.words and not args.input and sys.stdin.isatty()
    if args.tui or no_prompt:
        session_ref = args.session or ("-".join(args.words) if args.words else None)
        session = _resolve_session(
            session_ref, args.follow_up, args.quiet, history_limit=_history_limit(cfg)
        )
        from .interactive import run_interactive

        run_interactive(opts, session)
        return

    session = _resolve_session(
        args.session, args.follow_up, args.quiet, history_limit=_history_limit(cfg)
    )
    user_content, detected_mods = get_content(args)
    _check_modality_support(args.model, detected_mods)
    run_turn(opts, session, user_content, _prompt_text(user_content))
