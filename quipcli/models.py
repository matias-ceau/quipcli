import http.client
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from . import constants
from .http_client import _open_connection, _request_target

# Minimum gap between background refresh attempts, so a failing fetch (offline,
# bad key) doesn't respawn a Python process on every single `qp` call.
_REFRESH_RETRY = 3600


def _refresh_stamp() -> Path:
    return constants._MODELS_CACHE.with_name("models.attempt")


def _models_url() -> str:
    parsed = urlparse(constants._API_URL)
    base = parsed.path.removesuffix("/chat/completions")
    return f"{parsed.scheme}://{parsed.netloc}{base}/models"


def _load_models() -> list[str]:
    """Return cached model IDs (stale cache is fine — background refresh handles freshness)."""
    if not constants._MODELS_CACHE.exists():
        return []
    try:
        data = json.loads(constants._MODELS_CACHE.read_text())
        return sorted(m["id"] for m in data.get("data", []))
    except json.JSONDecodeError, KeyError, TypeError:
        return []


def _load_models_full() -> list[dict]:
    if not constants._MODELS_CACHE.exists():
        return []
    try:
        return json.loads(constants._MODELS_CACHE.read_text()).get("data", [])
    except json.JSONDecodeError, OSError:
        return []


def _maybe_update_models_bg() -> None:
    """Spawn a detached background process to refresh the model cache if older than 12h."""
    if os.environ.get("_LLM_CMD_BG_UPDATE"):
        return  # we ARE the background process, don't recurse
    try:
        if time.time() - constants._MODELS_CACHE.stat().st_mtime < constants._CACHE_TTL:
            return  # cache is fresh enough
    except FileNotFoundError:
        pass
    stamp = _refresh_stamp()
    try:
        if time.time() - stamp.stat().st_mtime < _REFRESH_RETRY:
            return  # a refresh was attempted recently (and evidently failed)
    except FileNotFoundError:
        pass
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    except OSError:
        pass
    pkg_parent = str(Path(__file__).parent.parent)
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                f"import sys; sys.path.insert(0, {pkg_parent!r});"
                f"from quipcli.models import _fetch_models; _fetch_models()"
            ),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "_LLM_CMD_BG_UPDATE": "1"},
    )


def _fetch_models() -> list[str]:
    """Fetch model list from provider, save to cache, return sorted IDs."""
    url = _models_url()
    parsed = urlparse(url)
    headers = (
        {"Authorization": f"Bearer {constants._API_KEY}"} if constants._API_KEY else {}
    )
    try:
        conn = _open_connection(parsed)
        conn.request("GET", _request_target(parsed), headers=headers)
        resp = conn.getresponse()
    except OSError as e:
        print(f"Error: connection failed: {e}", file=sys.stderr)
        return []
    if resp.status != 200:
        print(f"Error: failed to fetch models: HTTP {resp.status}", file=sys.stderr)
        return []
    try:
        data = json.loads(resp.read().decode())
    except json.JSONDecodeError:
        print("Error: invalid JSON in models response.", file=sys.stderr)
        return []
    constants._CACHE_DIR.mkdir(parents=True, exist_ok=True)
    constants._atomic_write_text(constants._MODELS_CACHE, json.dumps(data))
    return sorted(m["id"] for m in data.get("data", []))


_RANKINGS_HOST = "openrouter.ai"
_RANKINGS_PATH = "/api/v1/datasets/rankings-daily"


def _fetch_rankings() -> list[dict]:
    """Fetch OpenRouter's top-50-models-by-daily-tokens dataset and cache the
    most recent day's ranking. This is a usage-volume signal (how much
    traffic a model gets on OpenRouter), not a quality/intelligence score —
    OpenRouter doesn't publish one. Only meaningful against OpenRouter
    itself, and requires an API key (unlike the public /models endpoint)."""
    if constants._API_URL != constants._DEFAULT_API_URL:
        print(
            "Error: --update-rankings only supports OpenRouter (the default provider).",
            file=sys.stderr,
        )
        return []
    if not constants._API_KEY:
        print(
            "Error: --update-rankings requires an OpenRouter API key.", file=sys.stderr
        )
        return []
    conn = http.client.HTTPSConnection(
        _RANKINGS_HOST, context=constants._SSL_CTX, timeout=30
    )
    try:
        conn.request(
            "GET",
            _RANKINGS_PATH,
            headers={"Authorization": f"Bearer {constants._API_KEY}"},
        )
        resp = conn.getresponse()
    except OSError as e:
        print(f"Error: connection failed: {e}", file=sys.stderr)
        return []
    if resp.status != 200:
        print(f"Error: failed to fetch rankings: HTTP {resp.status}", file=sys.stderr)
        return []
    try:
        payload = json.loads(resp.read().decode())
    except json.JSONDecodeError:
        print("Error: invalid JSON in rankings response.", file=sys.stderr)
        return []

    rows = payload.get("data", [])
    latest_date = max((r["date"] for r in rows if "date" in r), default=None)
    latest = [
        r
        for r in rows
        if r.get("date") == latest_date and r.get("model_permaslug") != "other"
    ]
    latest.sort(key=lambda r: int(r.get("total_tokens", 0) or 0), reverse=True)
    ranked = [
        {
            "rank": i + 1,
            "model_permaslug": r["model_permaslug"],
            "total_tokens": int(r.get("total_tokens", 0) or 0),
        }
        for i, r in enumerate(latest)
    ]
    constants._CACHE_DIR.mkdir(parents=True, exist_ok=True)
    constants._atomic_write_text(
        constants._RANKINGS_CACHE, json.dumps({"date": latest_date, "data": ranked})
    )
    return ranked


def _load_rankings() -> dict:
    if not constants._RANKINGS_CACHE.exists():
        return {}
    try:
        return json.loads(constants._RANKINGS_CACHE.read_text())
    except json.JSONDecodeError, OSError:
        return {}


def _ranking_for(permaslug: str) -> dict | None:
    if not permaslug:
        return None
    for row in _load_rankings().get("data", []):
        if row.get("model_permaslug") == permaslug:
            return row
    return None


def _resolve_model_name(name: str) -> str:
    """Resolve a partial/substring model name against the cache.

    Returns the unique cached model id containing `name` (case-insensitive).
    Falls back to `name` unchanged if it's already an exact match, the cache
    is empty, or there's no match (e.g. a custom/uncached model). Exits with
    an error if `name` matches more than one cached model.
    """
    models = _load_models()
    if not name or not models or name in models:
        return name
    matches = [m for m in models if name.lower() in m.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"Error: '{name}' matches multiple cached models:", file=sys.stderr)
        for m in matches[:10]:
            print(f"  {m}", file=sys.stderr)
        if len(matches) > 10:
            print(f"  … and {len(matches) - 10} more.", file=sys.stderr)
        sys.exit(1)
    return name


def _load_model_arch(model_id: str) -> dict:
    for m in _load_models_full():
        if m.get("id") == model_id:
            return m.get("architecture", {})
    return {}


def _check_reasoning_support(model: str, effort: str | None) -> None:
    """Warn (never exit — the cache may be stale) when the cached model
    metadata says `effort` won't be accepted. Silent on a cache miss."""
    if not effort:
        return
    entry = next((m for m in _load_models_full() if m.get("id") == model), None)
    if entry is None:
        return
    info = entry.get("reasoning")
    if not info:
        print(
            f"Warning: {model} exposes no reasoning effort control; "
            f"reasoning effort {effort!r} will likely be ignored.",
            file=sys.stderr,
        )
        return
    supported = info.get("supported_efforts")
    if effort == "none" and info.get("mandatory"):
        print(
            f"Warning: {model} requires reasoning; effort 'none' will be rejected.",
            file=sys.stderr,
        )
    elif supported is not None and effort not in supported:
        print(
            f"Warning: {model} supports reasoning effort {', '.join(supported)} — "
            f"not {effort!r}.",
            file=sys.stderr,
        )


def _list_models_by_modality(
    in_mods: list[str] | None = None,
    out_mods: list[str] | None = None,
) -> list[str]:
    result = []
    for m in _load_models_full():
        arch = m.get("architecture", {})
        in_ok = not in_mods or all(
            x in arch.get("input_modalities", []) for x in in_mods
        )
        out_ok = not out_mods or all(
            x in arch.get("output_modalities", []) for x in out_mods
        )
        if in_ok and out_ok:
            result.append(m["id"])
    return sorted(result)


def _check_modality_support(model: str, needed: set[str]) -> None:
    if not needed:
        return
    arch = _load_model_arch(model)
    if not arch:
        print(
            f"Warning: cannot verify modality support for {model} (cache miss).",
            file=sys.stderr,
        )
        return
    supported = set(arch.get("input_modalities", []))
    missing = needed - supported
    if not missing:
        return
    print(
        f"Error: model {model!r} does not support input modality: {', '.join(sorted(missing))}",
        file=sys.stderr,
    )
    compatible = _list_models_by_modality(in_mods=list(needed))
    if compatible:
        print("Models that support this:", file=sys.stderr)
        for m in compatible[:10]:
            print(f"  {m}", file=sys.stderr)
        if len(compatible) > 10:
            print(
                f"  … and {len(compatible) - 10} more. Use: qp --models --in {','.join(sorted(needed))}",
                file=sys.stderr,
            )
    sys.exit(1)
