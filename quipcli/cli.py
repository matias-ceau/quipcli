import argparse
import importlib.util
import os
import sys

from . import constants
from .config import DEFAULT_MODEL
from .constants import DEFAULT_EXECUTE_SYSTEM_PROMPT
from .db import _UsageStats
from .models import _load_models
from .multimodal import _build_user_content


def _execute_prompt() -> str:
    shell = os.path.basename(os.environ.get("SHELL", "/bin/bash"))
    return DEFAULT_EXECUTE_SYSTEM_PROMPT.replace("{shell}", shell)


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def _package_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("quipcli")
    except PackageNotFoundError:
        return "unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qp",
        description="Ask questions or run AI-generated shell commands — no quotes needed.",
        usage="%(prog)s [-e|-c|-a] [-m MODEL] [-r EFFORT] [-S SYSTEM] [-s SESSION|-f] [-i FILE] [-q] [words ...]",
    )
    parser.add_argument(
        "words",
        nargs="*",
        help="Prompt words (no quoting needed). Files auto-detected by extension.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "-e",
        "--execute",
        action="store_true",
        help="Execute mode: generate and run a shell command.",
    )
    mode_group.add_argument(
        "-c",
        "--code",
        action="store_true",
        help="Code mode: generate code and print to stdout.",
    )
    mode_group.add_argument(
        "-a",
        "--agent",
        action="store_true",
        help="Agent mode: multi-turn tool-calling loop (shell, file read/write, web search/fetch).",
    )
    parser.add_argument(
        "--max-steps",
        type=_positive_int,
        default=12,
        metavar="N",
        help="With -a/--agent: max tool-calling loop iterations (default: 12).",
    )
    parser.add_argument(
        "--no-web",
        action="store_true",
        help="With -a/--agent: disable the hosted web search/fetch tools for this call.",
    )
    model_arg = parser.add_argument(
        "-m",
        "--model",
        default=DEFAULT_MODEL,
        metavar="MODEL",
        help=f"Model to use (default: {DEFAULT_MODEL} or $LLM_CMD_MODEL).",
    )
    parser.add_argument(
        "-r",
        "--reasoning",
        default=None,
        choices=constants.REASONING_EFFORTS,
        metavar="EFFORT",
        help="Reasoning effort for this call: "
        + ", ".join(constants.REASONING_EFFORTS)
        + ' (default: config "reasoning_effort", else the model\'s own default).',
    )
    parser.add_argument(
        "-S",
        "--system",
        default=None,
        metavar="PROMPT",
        help="Override the system prompt.",
    )
    parser.add_argument(
        "-s",
        "--session",
        default=None,
        metavar="SESSION",
        help="Session name or 'auto'. Displayed on stderr for reuse.",
    )
    parser.add_argument(
        "-f",
        "--follow-up",
        action="store_true",
        help="Continue the last session in history.",
    )
    parser.add_argument(
        "-i",
        "--input",
        action="append",
        metavar="FILE",
        help="Explicit file input (image/pdf/audio/video). Repeatable.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress post-response usage stats.",
    )
    parser.add_argument(
        "--update-models",
        action="store_true",
        help="Fetch and cache the model list from the provider, then exit.",
    )
    parser.add_argument(
        "--update-rankings",
        action="store_true",
        help="Fetch OpenRouter's top-50 daily usage ranking (needs an OpenRouter "
        "API key), then exit. Usage volume, not a quality score.",
    )
    parser.add_argument(
        "--models",
        action="store_true",
        help="List cached models (* marks the current default), then exit.",
    )
    parser.add_argument(
        "--in",
        dest="in_filter",
        default=None,
        metavar="MODALITIES",
        help="With --models: comma-separated required input modalities (e.g. image,text).",
    )
    parser.add_argument(
        "--out",
        dest="out_filter",
        default=None,
        metavar="MODALITIES",
        help="With --models: comma-separated required output modalities (e.g. audio).",
    )
    parser.add_argument(
        "--model-get",
        action="store_true",
        help="Print the current default model, then exit.",
    )
    parser.add_argument(
        "--model-set",
        nargs="?",
        const="",
        default=None,
        metavar="MODEL",
        help="Set the default model (omit MODEL to pick interactively), then exit.",
    )
    parser.add_argument(
        "--config-edit",
        action="store_true",
        help="Open the config file in $EDITOR, then exit.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current configuration and machine context, then exit.",
    )
    parser.add_argument(
        "--cost",
        nargs="?",
        const="7d",
        default=None,
        metavar="PERIOD",
        help="Show usage cost summary (1d, 7d default, 30d, or all), then exit.",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Interactive fzf-based picker for models and config (requires fzf).",
    )
    parser.add_argument(
        "--_tui-model-info",
        dest="tui_model_info",
        default=None,
        metavar="MODEL_ID",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_tui-list-models",
        dest="tui_list_models",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_tui-config-lines",
        dest="tui_config_lines",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_package_version()}",
    )
    parser.add_argument(
        "--tldr",
        action="store_true",
        help="Show quick-reference cheatsheet and exit.",
    )
    parser.add_argument(
        "--docs",
        action="store_true",
        help="Show full documentation and exit.",
    )

    # Attach completer — substring match so "haiku" finds "anthropic/claude-3-5-haiku"
    if importlib.util.find_spec("argcomplete") is not None:

        def _model_completer(prefix, **_):
            return [m for m in _load_models() if not prefix or prefix in m]

        model_arg.completer = _model_completer  # type: ignore[attr-defined]

    return parser


def get_content(
    args: argparse.Namespace,
) -> tuple[str | list[dict], set[str]]:
    """
    Returns (user_content, detected_input_modalities).
    Piped stdin combines with words/-i files (appended after the prompt);
    alone, it becomes the whole prompt.
    """
    stdin_text = ""
    if not sys.stdin.isatty():
        try:
            stdin_text = sys.stdin.read().strip()
        except OSError:
            pass
    if args.words or args.input:
        return _build_user_content(args.words, args.input or [], stdin_text or None)
    if stdin_text:
        return stdin_text, set()
    print("Usage: qp [words ...]\n       echo 'question' | qp", file=sys.stderr)
    sys.exit(1)


def _print_stats(stats: _UsageStats | None) -> None:
    if stats is None:
        return
    total_tok = stats.prompt_tokens + stats.completion_tokens
    parts = [stats.model]
    if total_tok:
        parts.append(f"{total_tok} tok")
    if stats.cost_usd is not None:
        parts.append(f"${stats.cost_usd:.4f}")
    print(f"\033[2m  [{' | '.join(parts)}]\033[0m", file=sys.stderr)
