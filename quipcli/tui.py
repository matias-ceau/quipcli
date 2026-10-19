import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap

from . import constants
from .config import _load_config, _resolve_default_model, _save_config
from .execute import _edit_text_value
from .http_client import _ollama_models
from .models import _load_models, _load_models_full, _ranking_for

_CONFIG_KEYS = [
    "default_model",
    "reasoning_effort",
    "chat_system_prompt",
    "execute_system_prompt",
    "code_system_prompt",
    "agent_system_prompt",
    "system_prompt",
    "ollama_model",
]
_PROMPT_KEYS = {
    "chat_system_prompt",
    "execute_system_prompt",
    "code_system_prompt",
    "agent_system_prompt",
    "system_prompt",
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Structural chrome shared by every fzf screen: bordered box, header pinned
# above the list, compact inline counter, a preview pane on the right.
# These are layout decisions (fzf 0.4x+ features), independent of color.
_FZF_LAYOUT = [
    "--height=90%",
    "--layout=reverse",
    "--border=rounded",
    "--header-first",
    "--info=inline-right",
    "--marker=● ",
    "--pointer=▶",
    "--preview-window=right,55%,border-left",
    "--bind=ctrl-/:toggle-preview",
    "--bind=resize:refresh-preview",
]

# fzf semantic color slots (fg/bg = list, fg+/bg+ = highlighted row, hl/hl+ =
# query matches, pointer = ▶ cursor, border/label = box + title, header = the
# keybind-hint line). Flexoki palette, inlined so it works without dotfiles.
_FZF_COLORS = [
    "--color=fg:#878580,fg+:#fffcf0,bg+:#282726",
    "--color=hl:#205ea6,hl+:#24837b",
    "--color=info:#ad8301,prompt:#205ea6,pointer:#5e409d",
    "--color=marker:#a02f6f,spinner:#ad8301,header:#6f6e69",
    "--color=border:#575653,label:#cecdc3",
]


def _fzf_available() -> bool:
    return shutil.which("fzf") is not None


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _run_fzf(
    items: list[str],
    *,
    header: str | None = None,
    preview_cmd: str | None = None,
    extra_binds: list[str] | None = None,
    prompt: str | None = None,
    border_label: str | None = None,
    ansi: bool = False,
) -> str | None:
    """Shell out to the real fzf binary. Returns the selected line, or None
    on Esc/Ctrl-C/empty selection."""
    args = ["fzf", *_FZF_LAYOUT, *_FZF_COLORS]
    if ansi:
        args.append("--ansi")
    if border_label:
        args += ["--border-label", f" {border_label} "]
    if header:
        args += ["--header", header]
    if prompt:
        args += ["--prompt", prompt]
    if preview_cmd:
        args += ["--preview", preview_cmd]
    for bind in extra_binds or []:
        args += ["--bind", bind]
    try:
        result = subprocess.run(
            args, input="\n".join(items), capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    selected = result.stdout.strip()
    return selected or None


# fzf runs preview/reload commands through a shell: call back into *this*
# interpreter rather than whatever `qp` is first on $PATH (or none, under
# `uv run python -m quipcli`).
_SELF = f"{shlex.quote(sys.executable)} -m quipcli"


def _model_id_from_line(line: str) -> str:
    line = _strip_ansi(line)
    return line[2:].strip() if len(line) > 2 else line.strip()


def _model_lines() -> list[str]:
    current = _resolve_default_model()
    lines = []
    for m in _load_models():
        if m == current:
            lines.append(f"\033[32m● {m}\033[0m")
        else:
            lines.append(f"  {m}")
    return lines


def _print_model_info(line: str) -> None:
    """Handler for the hidden --_tui-model-info flag (fzf preview command)."""
    model_id = _model_id_from_line(line)
    for m in _load_models_full():
        if m.get("id") != model_id:
            continue
        pricing = m.get("pricing") or {}
        arch = m.get("architecture") or {}

        def _per_million(key: str, pricing: dict = pricing) -> str:
            try:
                return f"${float(pricing[key]) * 1_000_000:.2f}"
            except KeyError, TypeError, ValueError:
                return "n/a"

        print(f"id: {model_id}")
        print(f"name: {m.get('name', '')}")
        print(f"context_length: {m.get('context_length', 'n/a')}")
        print(f"price_per_1M_prompt: {_per_million('prompt')}")
        print(f"price_per_1M_completion: {_per_million('completion')}")
        print(f"input_modalities: {', '.join(arch.get('input_modalities', []))}")
        print(f"output_modalities: {', '.join(arch.get('output_modalities', []))}")
        reasoning = m.get("reasoning")
        if reasoning:
            efforts = ", ".join(reasoning.get("supported_efforts") or []) or "n/a"
            extras = [
                f"default {reasoning['default_effort']}"
                if reasoning.get("default_effort")
                else "",
                "mandatory" if reasoning.get("mandatory") else "",
            ]
            suffix = "; ".join(filter(None, extras))
            print(f"reasoning_efforts: {efforts}" + (f" ({suffix})" if suffix else ""))

        ranking = _ranking_for(m.get("canonical_slug", ""))
        if ranking:
            print(
                f"usage_rank: #{ranking['rank']} of top 50 on openrouter.ai/rankings "
                f"({ranking['total_tokens']:,} tokens/day — usage volume, not a quality score)"
            )

        description = (m.get("description") or "").strip()
        if description:
            width = 70
            try:
                width = max(20, int(os.environ.get("FZF_PREVIEW_COLUMNS", width)))
            except ValueError:
                pass
            print()
            print(textwrap.fill(description, width=width))
        return
    print(f"id: {model_id}")
    print("(no cached details — run: qp --update-models)")


def _models_view(picker_mode: bool = False) -> str | None:
    lines = _model_lines()
    if not lines:
        print("No cached models — run: qp --update-models", file=sys.stderr)
        return None
    action = "pick" if picker_mode else "set as default"
    selected = _run_fzf(
        lines,
        header=f"enter  {action}   ctrl-r  refresh from provider   esc  back",
        preview_cmd=f"{_SELF} --_tui-model-info {{}}",
        extra_binds=[
            f"ctrl-r:reload({_SELF} --update-models 1>&2; {_SELF} --_tui-list-models)"
        ],
        prompt="model> ",
        border_label="qp · models",
        ansi=True,
    )
    if selected is None:
        return None
    model_id = _model_id_from_line(selected)
    if picker_mode:
        return model_id
    cfg = _load_config()
    cfg["default_model"] = model_id
    _save_config(cfg)
    print(f"\033[2mDefault model set to: {model_id}\033[0m", file=sys.stderr)
    return model_id


def pick_model_interactive() -> str | None:
    """Used by --model-set with no value, when fzf is available."""
    return _models_view(picker_mode=True)


def _config_lines(cfg: dict) -> list[str]:
    width = max(len(key) for key in _CONFIG_KEYS)
    lines = []
    for key in _CONFIG_KEYS:
        val = cfg.get(key) or "(not set)"
        val = val if len(val) <= 60 else val[:57] + "..."
        lines.append(f"{key.ljust(width)} = {val}")
    return lines


def _key_from_line(line: str) -> str:
    return line.split(" = ", 1)[0].strip()


def _config_view() -> None:
    config_path = shlex.quote(str(constants._CONFIG_FILE))
    preview = (
        f"cat {config_path} | bat -l json --color=always --style=plain --paging=never "
        f"2>/dev/null || cat {config_path}"
    )
    while True:
        selected = _run_fzf(
            _config_lines(_load_config()),
            header="enter  edit   ctrl-e  open full file in $EDITOR   esc  back",
            preview_cmd=preview,
            extra_binds=[
                f"ctrl-e:execute({_SELF} --config-edit)+reload({_SELF} --_tui-config-lines)"
            ],
            prompt="config> ",
            border_label="qp · config",
        )
        if selected is None:
            return
        key = _key_from_line(selected)
        cfg = _load_config()
        if key == "default_model":
            picked = _models_view(picker_mode=True)
            if picked is not None:
                cfg["default_model"] = picked
                _save_config(cfg)
        elif key == "ollama_model":
            models = _ollama_models()
            if models:
                current = cfg.get("ollama_model") or ""
                lines = [("* " if m == current else "  ") + m for m in models]
                picked_line = _run_fzf(
                    lines,
                    header="enter  pick   esc  back",
                    prompt="ollama> ",
                    border_label="qp · ollama models",
                )
                if picked_line is not None:
                    cfg["ollama_model"] = _model_id_from_line(picked_line)
                    _save_config(cfg)
            else:
                print(
                    "\033[2mOllama unreachable — enter a value manually.\033[0m",
                    file=sys.stderr,
                )
                cfg["ollama_model"] = _edit_text_value(cfg.get("ollama_model") or "")
                _save_config(cfg)
        elif key == "reasoning_effort":
            current = cfg.get("reasoning_effort") or ""
            options = ["(model default)", *constants.REASONING_EFFORTS]
            lines = [
                ("* " if o == (current or "(model default)") else "  ") + o
                for o in options
            ]
            picked_line = _run_fzf(
                lines,
                header="enter  pick   esc  back",
                prompt="effort> ",
                border_label="qp · reasoning effort",
            )
            if picked_line is not None:
                picked = _model_id_from_line(picked_line)
                if picked == "(model default)":
                    cfg.pop("reasoning_effort", None)
                else:
                    cfg["reasoning_effort"] = picked
                _save_config(cfg)
        elif key in _PROMPT_KEYS:
            cfg[key] = _edit_text_value(cfg.get(key) or "")
            _save_config(cfg)


def run_tui() -> None:
    if not _fzf_available():
        print(
            "Error: --tui requires fzf (https://github.com/junegunn/fzf).",
            file=sys.stderr,
        )
        sys.exit(1)
    while True:
        choice = _run_fzf(
            ["Models", "Config"],
            header="enter  open   esc  quit",
            prompt="quip> ",
            border_label="qp",
        )
        if choice is None:
            return
        if choice == "Models":
            _models_view(picker_mode=False)
        elif choice == "Config":
            _config_view()
