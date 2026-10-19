"""Multi-turn tool-calling loop for --agent mode.

Mirrors OpenRouter's own "Simple Agentic Loop" pattern (see
docs/guides/features/tool-calling): each step is a plain, non-streaming
chat-completions call with `tools` attached; if the model asks for a
function tool, we run it locally and feed the result back; otherwise the
response is the final answer. Hosted server tools (web_search/web_fetch)
never appear as a tool_call here — OpenRouter resolves those itself before
replying, so there's nothing for this loop to execute for them."""

import json
import sys

from . import constants
from .db import _UsageStats
from .http_client import _make_request, _MarkdownAnsiRenderer, _use_markdown_rendering
from .tools import default_tools, execute_tool_call

_DEFAULT_MAX_STEPS = 12


def _is_openrouter() -> bool:
    # Hosted openrouter:* server tools only exist on OpenRouter; any other
    # OpenAI-compatible endpoint rejects the whole request if they're listed.
    return constants._API_URL == constants._DEFAULT_API_URL


class _Progress:
    """A single dim, self-erasing "thinking…" line on stderr while a
    non-streaming step is in flight, plus the step's reasoning (if the
    provider returned any) once it lands. Silent when stderr isn't a TTY."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled and sys.stderr.isatty()

    def start(self, step: int) -> None:
        if self.enabled:
            label = "thinking…" if step == 0 else f"thinking… (step {step + 1})"
            print(f"\033[2m{label}\033[0m", end="", file=sys.stderr, flush=True)

    def done(self, message: dict) -> None:
        if not self.enabled:
            return
        print("\r\033[K", end="", file=sys.stderr)
        reasoning = (message.get("reasoning") or "").strip()
        if reasoning:
            print(f"\033[2m{reasoning}\033[0m", file=sys.stderr, flush=True)


def _step(messages: list[dict], model: str, extra: dict) -> tuple[dict, dict, str]:
    """One non-streaming chat-completions call → (message, usage, model used)."""
    resp, used_model = _make_request(messages, model, stream=False, extra=extra)
    raw = resp.read().decode(errors="replace")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"Error: unexpected API response: {raw[:200]}", file=sys.stderr)
        sys.exit(1)
    if "error" in data or not data.get("choices"):
        err = data.get("error")
        msg = err.get("message") if isinstance(err, dict) else err
        print(
            f"Error: API returned no completion{': ' + str(msg) if msg else '.'}",
            file=sys.stderr,
        )
        sys.exit(1)
    return data["choices"][0]["message"], data.get("usage") or {}, used_model


def run_agent_loop(
    messages: list[dict],
    model: str,
    max_steps: int = _DEFAULT_MAX_STEPS,
    server_tools: bool = True,
    render_markdown: bool = True,
    extra: dict | None = None,
    show_progress: bool = False,
) -> tuple[str, _UsageStats | None]:
    tools = default_tools(include_server_tools=server_tools and _is_openrouter())
    progress = _Progress(show_progress)
    total_prompt = 0
    total_completion = 0
    cost = 0.0
    have_cost = False
    used_model = model

    def _account(usage: dict) -> None:
        nonlocal total_prompt, total_completion, cost, have_cost
        total_prompt += usage.get("prompt_tokens", 0)
        total_completion += usage.get("completion_tokens", 0)
        if usage.get("cost") is not None:
            cost += usage["cost"]
            have_cost = True

    def _finish(text: str) -> tuple[str, _UsageStats]:
        _print_final(text, render_markdown)
        return text, _UsageStats(
            model=used_model,
            prompt_tokens=total_prompt,
            completion_tokens=total_completion,
            cost_usd=cost if have_cost else None,
        )

    for step in range(max_steps):
        progress.start(step)
        message, usage, used_model = _step(
            messages, model, {"tools": tools, **(extra or {})}
        )
        progress.done(message)
        _account(usage)
        messages.append(message)
        tool_calls = [
            c
            for c in (message.get("tool_calls") or [])
            if c.get("type", "function") == "function"
        ]

        if not tool_calls:
            return _finish((message.get("content") or "").strip())

        for call in tool_calls:
            fn = call["function"]
            result = execute_tool_call(fn["name"], fn.get("arguments", ""))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                }
            )

    # Out of steps: the last message is a tool result, not an answer. Ask
    # once more with tool use disabled so the user gets a real final reply.
    print(
        f"\033[2m⚠ agent: max steps ({max_steps}) reached — asking for a final answer\033[0m",
        file=sys.stderr,
    )
    progress.start(max_steps)
    message, usage, used_model = _step(
        messages, model, {"tools": tools, "tool_choice": "none", **(extra or {})}
    )
    progress.done(message)
    _account(usage)
    messages.append(message)
    return _finish((message.get("content") or "").strip())


def _print_final(text: str, render_markdown: bool) -> None:
    if not text:
        return
    if render_markdown and _use_markdown_rendering():
        renderer = _MarkdownAnsiRenderer()
        print(renderer.render(text) + renderer.finish(), end="")
    else:
        print(text, end="")
    print()
