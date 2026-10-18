import json
import os
import sys

from . import constants

_HARDCODED_DEFAULT_MODEL = "openai/gpt-4o-mini"

_warned_bad_config = False


def _load_config() -> dict:
    global _warned_bad_config
    if not constants._CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(constants._CONFIG_FILE.read_text())
    except json.JSONDecodeError, OSError:
        if not _warned_bad_config:
            print(
                f"Warning: {constants._CONFIG_FILE} is invalid JSON, ignoring it "
                "(falling back to defaults).",
                file=sys.stderr,
            )
            _warned_bad_config = True
        return {}


def _save_config(data: dict) -> None:
    constants._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    constants._atomic_write_text(
        constants._CONFIG_FILE, json.dumps(data, indent=2) + "\n"
    )


def _ensure_config() -> dict:
    """Create the config file on first run (empty, or with whatever keys
    already exist) so it always exists on disk and can be hand-edited in
    place. Does not seed default_model — that would persist a one-off
    LLM_CMD_MODEL override or mask it from later env-aware reads."""
    cfg = _load_config()
    if not constants._CONFIG_FILE.exists():
        _save_config(cfg)
    return cfg


def _seed_defaults(defaults: dict) -> dict:
    """Write any of `defaults` not already present in config.json — used to
    materialize the per-mode system prompt defaults as plain, editable JSON
    instead of leaving them buried in Python source. Never overwrites an
    existing key, so a user's edits (or a deliberately blank value) stick."""
    cfg = _load_config()
    missing = {k: v for k, v in defaults.items() if k not in cfg}
    if missing:
        cfg.update(missing)
        _save_config(cfg)
    return cfg


def _resolve_default_model() -> str:
    return (
        os.environ.get("LLM_CMD_MODEL")
        or _load_config().get("default_model")
        or _HARDCODED_DEFAULT_MODEL
    )


def _model_source() -> str:
    """Where the value _resolve_default_model() would return actually comes
    from — env checked before config, matching _resolve_default_model's own
    precedence exactly (do not reorder one without the other)."""
    if os.environ.get("LLM_CMD_MODEL"):
        return "env"
    if _load_config().get("default_model"):
        return "config"
    return "default"


DEFAULT_MODEL = _resolve_default_model()
