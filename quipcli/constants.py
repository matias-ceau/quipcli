import os
import shutil
import ssl
from pathlib import Path

CODE_SYSTEM_PROMPT = (
    "You are an expert programmer. Output ONLY raw code — no explanation, "
    "no markdown fences, no prose before or after."
)

DEFAULT_CHAT_SYSTEM_PROMPT = (
    "You are a helpful, direct assistant answering from the command line. "
    "Prefer concise, plain-text answers over hedging or padding; use Markdown "
    "only where it actually clarifies (code blocks, short lists)."
)

# {shell} is substituted with the invoking machine's actual $SHELL at request
# time (see entry.py:_mode_prompt) — never baked in as a literal shell name,
# so a config.json synced across machines with different shells stays correct.
DEFAULT_EXECUTE_SYSTEM_PROMPT = (
    "You are a shell command generator for {shell}. "
    "Output ONLY a single executable shell command that accomplishes the user's request. "
    "No explanation. No markdown. No code fences. No newlines. "
    "Chain multiple steps with && or semicolons if needed."
)

DEFAULT_AGENT_SYSTEM_PROMPT = (
    "You are a terminal assistant with tools: run_shell (execute a shell "
    "command), read_file, write_file, and web search/fetch for current "
    "information. The user must explicitly confirm every run_shell and "
    "write_file call before it happens, and may decline — if declined, "
    "adapt and try another approach rather than repeating the same call. "
    "Prefer the fewest tool calls that get the job done; give a concise, "
    "direct final answer once you have what you need."
)

# Provider config — override via env vars:
#   LLM_CMD_MODEL   — model name          (e.g. anthropic/claude-3-5-haiku)
#   LLM_CMD_API_KEY — API key             (falls back to OPENROUTER_API_KEY)
#   LLM_CMD_API_URL — full endpoint URL   (any OpenAI-compatible API)
_DEFAULT_API_URL = "https://openrouter.ai/api/v1/chat/completions"
_API_URL = os.environ.get("LLM_CMD_API_URL", _DEFAULT_API_URL)
_API_KEY = os.environ.get("LLM_CMD_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")

# Local Ollama fallback (used when the provider is unreachable or no key is set)
_OLLAMA_URL = os.environ.get("LLM_CMD_OLLAMA_URL", "http://localhost:11434")

# XDG paths
_CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "quipcli"
_CONFIG_DIR = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "quipcli"
)
_DATA_DIR = (
    Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "quipcli"
)

_MODELS_CACHE = _CACHE_DIR / "models.json"
_RANKINGS_CACHE = _CACHE_DIR / "rankings.json"
_CONFIG_FILE = _CONFIG_DIR / "config.json"
_HISTORY_DB = _DATA_DIR / "history.db"

_CACHE_TTL = 43200
_SSL_CTX = ssl.create_default_context()


def _migrate_legacy_data() -> None:
    """One-time copy from the pre-rename ~/.config/llm-cmd (etc.) layout, if
    present and the quipcli location hasn't been used yet. Never touches or
    deletes the old files — plain copy, safe to run on every startup (it's a
    no-op once the new location exists)."""
    old_cache = (
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "llm-cmd"
    )
    old_config = (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "llm-cmd"
    )
    old_data = (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        / "llm-cmd"
    )
    try:
        for old_file, new_file in (
            (old_config / "config.json", _CONFIG_FILE),
            (old_cache / "models.json", _MODELS_CACHE),
            (old_data / "history.db", _HISTORY_DB),
        ):
            if old_file.exists() and not new_file.exists():
                new_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old_file, new_file)
    except OSError:
        pass


_migrate_legacy_data()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via temp file + rename so concurrent readers never see partial content."""
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


# Multimodal file extension → MIME type
_MEDIA_EXTENSIONS: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".aiff": "audio/aiff",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/mov",
    ".mpeg": "video/mpeg",
}
_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})
_PDF_EXTS = frozenset({".pdf"})
_AUDIO_EXTS = frozenset({".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".aiff"})
_VIDEO_EXTS = frozenset({".mp4", ".webm", ".mov", ".mpeg"})
_MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB warning threshold

# OpenRouter's reasoning.effort values, highest first. Per-model support varies
# (see the model cache's "reasoning.supported_efforts"); "none" is rejected by
# models where reasoning is mandatory.
# Max past messages resent with -f/-s (config "session_history" overrides;
# 0 = unlimited) — keeps long sessions inside the model's context window.
SESSION_HISTORY = 40

REASONING_EFFORTS = ("max", "xhigh", "high", "medium", "low", "minimal", "none")
