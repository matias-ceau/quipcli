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

from .db import _UsageStats
from .http_client import _make_request, _MarkdownAnsiRenderer, _use_markdown_rendering
from .tools import default_tools, execute_tool_call

_DEFAULT_MAX_STEPS = 12


def run_agent_loop(
    messages: list[dict],
    model: str,
    max_steps: int = _DEFAULT_MAX_STEPS,
    server_tools: bool = True,
    render_markdown: bool = True,
    extra: dict | None = None,
) -> tuple[str, _UsageStats | None]:
    tools = default_tools(include_server_tools=server_tools)
    total_prompt = 0
    total_completion = 0
    cost = 0.0
    have_cost = False
    used_model = model

    for step in range(max_steps):
        resp, used_model = _make_request(
            messages, model, stream=False, extra={"tools": tools, **(extra or {})},
        )
        raw = resp.read().decode(errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            print(f"Error: unexpected API response: {raw[:200]}", file=sys.stderr)
            sys.exit(1)
        if "error" in data or not data.get("choices"):
            err = data.get("error")
            msg = err.get("message") if isinstance(err, dict) else err
            print(f"Error: API returned no completion{': ' + str(msg) if msg else '.'}", file=sys.stderr)
            sys.exit(1)

        usage = data.get("usage") or {}
        total_prompt += usage.get("prompt_tokens", 0)
        total_completion += usage.get("completion_tokens", 0)
        if usage.get("cost") is not None:
            cost += usage["cost"]
            have_cost = True

        message = data["choices"][0]["message"]
        messages.append(message)
        tool_calls = [c for c in (message.get("tool_calls") or []) if c.get("type", "function") == "function"]

        if not tool_calls:
            text = (message.get("content") or "").strip()
            _print_final(text, render_markdown)
            stats = _UsageStats(
                model=used_model,
                prompt_tokens=total_prompt,
                completion_tokens=total_completion,
                cost_usd=cost if have_cost else None,
            )
            return text, stats

        for call in tool_calls:
            fn = call["function"]
            result = execute_tool_call(fn["name"], fn.get("arguments", ""))
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": result,
            })

    print(f"\033[2m⚠ agent: max steps ({max_steps}) reached — returning last response\033[0m", file=sys.stderr)
    last = messages[-1] if messages else {}
    text = (last.get("content") or "").strip() if isinstance(last, dict) else ""
    _print_final(text, render_markdown)
    stats = _UsageStats(
        model=used_model,
        prompt_tokens=total_prompt,
        completion_tokens=total_completion,
        cost_usd=cost if have_cost else None,
    )
    return text, stats


def _print_final(text: str, render_markdown: bool) -> None:
    if not text:
        return
    if render_markdown and _use_markdown_rendering():
        renderer = _MarkdownAnsiRenderer()
        print(renderer.render(text) + renderer.finish(), end="")
    else:
        print(text, end="")
    print()
