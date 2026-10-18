import http.client
import json
import os
import re
import sys
import time
from urllib.parse import urlparse

from . import constants
from .db import _UsageStats

_RETRIABLE_STATUSES = {429, 500, 502, 503, 529}
_RETRY_WAITS = (1, 2, 4)  # seconds between attempts (4 attempts total)


class _APIStatusError(Exception):
    """Non-transient HTTP error from the API — retries exhausted or pointless."""


def _open_connection(parsed) -> http.client.HTTPConnection:
    if parsed.scheme == "http":
        return http.client.HTTPConnection(parsed.netloc, timeout=30)
    return http.client.HTTPSConnection(parsed.netloc, context=constants._SSL_CTX, timeout=30)


def _post_json(url: str, body: str, api_key: str) -> http.client.HTTPResponse:
    """POST with retry/backoff on connection errors and transient HTTP statuses.

    Raises ConnectionError when the endpoint is unreachable (fallback-worthy),
    _APIStatusError when the API answered with a non-200 status.
    """
    parsed = urlparse(url)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    last_error = ""
    status_error = False
    for wait in (*_RETRY_WAITS, None):
        try:
            conn = _open_connection(parsed)
            conn.request("POST", parsed.path, body, headers)
            resp = conn.getresponse()
        except OSError as e:
            last_error = f"Connection error: {e}"
            status_error = False
            resp = None
        if resp is not None:
            if resp.status == 200:
                return resp
            detail = resp.read().decode(errors="replace")
            last_error = f"API error {resp.status}: {detail}"
            status_error = True
            if resp.status not in _RETRIABLE_STATUSES:
                break
        if wait is None:
            break
        print(f"\033[2m  retrying in {wait}s… ({last_error.splitlines()[0]})\033[0m", file=sys.stderr)
        time.sleep(wait)
    raise _APIStatusError(last_error) if status_error else ConnectionError(last_error)


def _ollama_models() -> list[str] | None:
    """Names of locally available Ollama models, or None if Ollama is unreachable."""
    parsed = urlparse(constants._OLLAMA_URL)
    try:
        conn = http.client.HTTPConnection(parsed.netloc, timeout=2)
        conn.request("GET", "/api/tags")
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        data = json.loads(resp.read().decode())
        models = [m["name"] for m in data.get("models", [])]
        return models or None
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None


def _pick_ollama_model(models: list[str], cfg: dict) -> str:
    # TODO(user): fallback-model policy — config "ollama_model" wins, otherwise
    # first available. Other options: most recently pulled, or a preference list.
    return cfg.get("ollama_model") or models[0]


def _ollama_fallback(
    body_dict: dict,
    reason: str,
) -> tuple[http.client.HTTPResponse, str] | None:
    models = _ollama_models()
    if not models:
        return None
    from .config import _load_config
    model = _pick_ollama_model(models, _load_config())
    print(f"\033[2m⚠ {reason} — falling back to Ollama ({model})\033[0m", file=sys.stderr)
    body = json.dumps({**body_dict, "model": model})
    try:
        resp = _post_json(f"{constants._OLLAMA_URL}/v1/chat/completions", body, "")
        return resp, model
    except (ConnectionError, _APIStatusError) as e:
        print(f"Error: Ollama fallback failed: {e}", file=sys.stderr)
        return None


def _make_request(
    messages: list[dict],
    model: str,
    stream: bool,
    include_usage: bool = False,
    extra: dict | None = None,
) -> tuple[http.client.HTTPResponse, str]:
    """Returns (response, model actually used — may differ on Ollama fallback).

    `extra` merges additional top-level request fields (e.g. {"tools": [...],
    "max_tool_calls": 30}) into the body — used by agent.py's tool-calling
    loop without adding a long, ever-growing list of named parameters here."""
    body_dict: dict = {"model": model, "messages": messages, "stream": stream}
    if stream and include_usage:
        body_dict["stream_options"] = {"include_usage": True}
    if extra:
        body_dict.update(extra)
    body = json.dumps(body_dict)
    is_local = urlparse(constants._API_URL).scheme == "http"

    if not constants._API_KEY and not is_local:
        result = _ollama_fallback(body_dict, "no API key configured")
        if result:
            return result
        print("Error: no API key. Set LLM_CMD_API_KEY or OPENROUTER_API_KEY, "
              "or run a local Ollama.", file=sys.stderr)
        sys.exit(1)

    try:
        return _post_json(constants._API_URL, body, constants._API_KEY), model
    except _APIStatusError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ConnectionError as e:
        result = _ollama_fallback(body_dict, "provider unreachable")
        if result:
            return result
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


class _MarkdownAnsiRenderer:
    _RESET = "\033[0m"
    _HEADING = "\033[1;36m"
    _CODE = "\033[38;5;150m"
    _INLINE_CODE = "\033[38;5;214m"
    _BOLD = "\033[1m"
    _LIST_MARKER = "\033[38;5;215m"
    _BLOCKQUOTE = "\033[2;3m"

    _LIST_RE = re.compile(r"\d+[.)](?= )")

    def __init__(self) -> None:
        self._carry = ""
        self._line_start = True
        self._line_leading_spaces = 0
        self._in_heading = False
        self._in_fenced_code = False
        self._fence_ticks = 0
        self._fence_colored = False
        self._in_inline_code = False
        self._in_bold = False
        self._in_blockquote = False

    def _render_text(self, text: str) -> str:
        out: list[str] = []
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "\n":
                if self._in_heading:
                    out.append(self._RESET)
                    self._in_heading = False
                if self._in_blockquote:
                    out.append(self._RESET)
                    self._in_blockquote = False
                out.append("\n")
                self._line_start = True
                self._line_leading_spaces = 0
                i += 1
                continue

            if self._line_start and ch == " " and self._line_leading_spaces < 3:
                out.append(ch)
                self._line_leading_spaces += 1
                i += 1
                continue

            if ch == "`" and (self._line_start or self._line_leading_spaces <= 3):
                j = i
                while j < len(text) and text[j] == "`":
                    j += 1
                tick_count = j - i
                if tick_count >= 3:
                    if self._in_heading:
                        out.append(self._RESET)
                        self._in_heading = False
                    if self._in_inline_code:
                        out.append(self._RESET)
                        self._in_inline_code = False
                    if self._in_bold:
                        out.append("\033[22m")
                        self._in_bold = False
                    out.append("`" * tick_count)
                    if not self._in_fenced_code:
                        line_end = text.find("\n", j)
                        if line_end == -1:
                            line_end = len(text)
                        info = text[j:line_end].strip().lower()
                        self._fence_colored = info not in {"md", "markdown"}
                        self._in_fenced_code = True
                        self._fence_ticks = tick_count
                        if self._fence_colored:
                            out.append(self._CODE)
                    elif tick_count >= self._fence_ticks:
                        self._in_fenced_code = False
                        self._fence_ticks = 0
                        if self._fence_colored:
                            out.append(self._RESET)
                            self._fence_colored = False
                    self._line_start = False
                    self._line_leading_spaces = 4
                    i = j
                    continue

            if self._in_fenced_code:
                out.append(ch)
                self._line_start = False
                self._line_leading_spaces = 4
                i += 1
                continue

            if self._line_start and ch in "-*+" and text[i + 1:i + 2] == " ":
                out.append(self._LIST_MARKER)
                out.append(ch)
                out.append(self._RESET)
                self._line_start = False
                self._line_leading_spaces = 4
                i += 1
                continue

            if self._line_start and ch.isdigit():
                m = self._LIST_RE.match(text, i)
                if m:
                    out.append(self._LIST_MARKER)
                    out.append(m.group(0))
                    out.append(self._RESET)
                    self._line_start = False
                    self._line_leading_spaces = 4
                    i = m.end()
                    continue

            if self._line_start and ch == ">":
                out.append(self._BLOCKQUOTE)
                out.append(ch)
                self._in_blockquote = True
                self._line_start = False
                self._line_leading_spaces = 4
                i += 1
                continue

            if self._line_start and ch == "#":
                j = i
                while j < len(text) and text[j] == "#" and (j - i) < 6:
                    j += 1
                if j < len(text) and text[j] == " ":
                    out.append(self._HEADING)
                    out.append(text[i:j + 1])
                    self._in_heading = True
                    self._line_start = False
                    self._line_leading_spaces = 4
                    i = j + 1
                    continue

            if ch == "`":
                if self._in_inline_code:
                    out.append("`")
                    out.append(self._RESET)
                    if self._in_bold:
                        out.append(self._BOLD)
                    self._in_inline_code = False
                else:
                    out.append(self._INLINE_CODE)
                    out.append("`")
                    self._in_inline_code = True
                self._line_start = False
                self._line_leading_spaces = 4
                i += 1
                continue

            if text.startswith("**", i):
                if self._in_bold:
                    out.append("**\033[22m")
                    self._in_bold = False
                else:
                    out.append("**" + self._BOLD)
                    self._in_bold = True
                self._line_start = False
                self._line_leading_spaces = 4
                i += 2
                continue

            out.append(ch)
            self._line_start = False
            self._line_leading_spaces = 4
            i += 1
        return "".join(out)

    def render(self, chunk: str) -> str:
        if not chunk:
            return ""
        text = self._carry + chunk
        last_newline = text.rfind("\n")
        if last_newline == -1:
            self._carry = text
            return ""
        safe = text[:last_newline + 1]
        self._carry = text[last_newline + 1:]
        return self._render_text(safe)

    def finish(self) -> str:
        tail = self._render_text(self._carry)
        self._carry = ""
        if (
            self._in_heading or self._in_fenced_code or self._in_inline_code
            or self._in_bold or self._in_blockquote
        ):
            self._in_heading = False
            self._in_fenced_code = False
            self._fence_ticks = 0
            self._fence_colored = False
            self._in_inline_code = False
            self._in_bold = False
            self._in_blockquote = False
            return tail + self._RESET
        return tail


def _use_markdown_rendering() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()


def _reasoning_style() -> tuple[str, str]:
    if os.environ.get("NO_COLOR") is not None:
        return "", ""
    return "\033[2m", "\033[0m"


def call_llm_streaming(
    messages: list[dict],
    model: str,
    collect_usage: bool = False,
    render_markdown: bool = True,
    show_reasoning: bool = False,
    extra: dict | None = None,
) -> tuple[str, _UsageStats | None]:
    """`show_reasoning` streams the model's `delta.reasoning` tokens dimmed to
    stderr (TTY only) — without it a reasoning model looks stalled until its
    first content token, which can take minutes at high effort."""
    resp, model = _make_request(messages, model, stream=True, include_usage=collect_usage, extra=extra)
    usage_data: dict | None = None
    parts: list[str] = []
    renderer = _MarkdownAnsiRenderer() if render_markdown and _use_markdown_rendering() else None
    show_reasoning = show_reasoning and sys.stderr.isatty()
    dim, reset = _reasoning_style()
    in_reasoning = False
    while line := resp.readline():
        text = line.decode().strip()
        if not text.startswith("data: "):
            continue
        payload = text[6:]
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
            choices = chunk.get("choices", [])
            if choices:
                reasoning = choices[0]["delta"].get("reasoning") or ""
                if reasoning and show_reasoning:
                    if not in_reasoning:
                        print(dim, end="", file=sys.stderr)
                        in_reasoning = True
                    print(reasoning, end="", file=sys.stderr, flush=True)
                delta = choices[0]["delta"].get("content", "")
                if delta and in_reasoning:
                    print(f"{reset}\n", file=sys.stderr, flush=True)
                    in_reasoning = False
                if delta:
                    rendered = renderer.render(delta) if renderer else delta
                    print(rendered, end="", flush=True)
                    parts.append(delta)
            if collect_usage and "usage" in chunk:
                usage_data = chunk["usage"]
        except (json.JSONDecodeError, KeyError, IndexError):
            pass
    if in_reasoning:
        print(f"{reset}\n", file=sys.stderr, flush=True)
    if renderer:
        tail = renderer.finish()
        if tail:
            print(tail, end="", flush=True)
    print()
    stats = None
    if collect_usage and usage_data:
        stats = _UsageStats(
            model=model,
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            cost_usd=usage_data.get("cost"),
        )
    return "".join(parts), stats


def call_llm_capture(
    messages: list[dict],
    model: str,
    extra: dict | None = None,
) -> tuple[str, _UsageStats | None]:
    resp, model = _make_request(messages, model, stream=False, extra=extra)
    raw = resp.read().decode(errors="replace")
    try:
        data = json.loads(raw)
        # OpenRouter can return {"error": ...} with HTTP 200
        if "error" in data or not data.get("choices"):
            err = data.get("error")
            msg = err.get("message") if isinstance(err, dict) else err
            print(f"Error: API returned no completion{': ' + str(msg) if msg else '.'}", file=sys.stderr)
            sys.exit(1)
        text = data["choices"][0]["message"]["content"].strip()
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        print(f"Error: unexpected API response: {raw[:200]}", file=sys.stderr)
        sys.exit(1)
    usage = data.get("usage")
    stats = _UsageStats(
        model=model,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        cost_usd=usage.get("cost"),
    ) if usage else None
    return text, stats
