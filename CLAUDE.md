# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`quipcli` is a minimal Python CLI for LLMs, installed as the single binary `qp`
(renamed from `llm-cmd`/`llm-cmd-model`/`llm-cmd-status`/`llm-cmd-cost` — the old
name collided with an unrelated PyPI package, and the four separate executables
are now flags on one binary). Core use cases:
- `qp what is the meaning of anagnorisis` — free-text prompt, no quotes
- `qp -e update all my cargo binaries` — generate + confirm + run a shell command
- `qp -a find and summarize the largest files under ~/Downloads` — agent mode: multi-turn tool-calling loop (shell/file/web tools)
- `qp --model-set openai/gpt-4o` — set persistent default model
- `qp -r low ...` — per-call reasoning effort (persistent: config `reasoning_effort`)
- `qp --status` — show current configuration
- `qp --cost 30d` — usage cost summary
- `qp --tui` — interactive fzf/bat picker for models and config

Default provider: OpenRouter (`OPENROUTER_API_KEY`). Any OpenAI-compatible API works via `LLM_CMD_API_URL` / `LLM_CMD_API_KEY` / `LLM_CMD_MODEL`.

## Install (Arch Linux / uv)

```bash
uv tool install -e .

# Activate tab-completion (add to .bashrc / .zshrc)
eval "$(register-python-argcomplete qp)"

# Populate model cache (auto-refreshes every 12h in background)
qp --update-models
```

## Development commands

```bash
uv run python -m quipcli <prompt>  # run without installing
qp --models                        # inspect model cache
qp --update-models                 # force model cache refresh
```

## Architecture

Package: `quipcli/` (16 modules)

| Module | Responsibility |
|---|---|
| `constants.py` | Provider env vars, XDG paths, MIME types, SSL context, legacy-data migration, default per-mode system prompt text |
| `config.py` | Load/save/resolve config file; `DEFAULT_MODEL`; `_seed_defaults` |
| `context.py` | `_machine_context` — OS/distro/shell/arch, computed fresh per call |
| `db.py` | SQLite history + sessions + cost summary; `_UsageStats` dataclass |
| `models.py` | Model cache (load, fetch, background refresh, modality filtering); usage-ranking cache (fetch, load, lookup) |
| `multimodal.py` | File encoding, image URL detection, user content builder |
| `http_client.py` | `_make_request`, `call_llm_streaming`, `call_llm_capture`, Ollama fallback |
| `tools.py` | `-a`'s local tool schemas + implementations (`run_shell`, `read_file`, `write_file`), `default_tools`, `execute_tool_call` |
| `agent.py` | `run_agent_loop` — the `-a` multi-turn tool-calling loop |
| `execute.py` | `_strip_fences`, `_edit_in_editor`, `_edit_text_value`, `confirm_and_run` |
| `cli.py` | `build_parser`, `get_content`, `_print_stats`, `_execute_prompt` |
| `tui.py` | `run_tui` — fzf/bat-based interactive picker (Models / Config views) |
| `docs.py` | `_TLDR` and `_DOCS` strings |
| `entry.py` | `main` + flag handlers (`_do_status`, `_do_cost`, `_do_models`, `_do_model_get`, `_do_model_set`, `_do_config_edit`) + `_mode_prompt` (per-mode system prompt resolution) |
| `__init__.py` | Façade: re-exports public API for backward compat with tests |
| `__main__.py` | `python -m quipcli` support |

Key design rules:
- **Single binary, no subcommand executables**: everything is a flag on `qp` (`--status`, `--cost`, `--models`, `--model-get`, `--model-set`, `--config-edit`, `--tui`, …), dispatched near the top of `main()` (`entry.py`) before the normal prompt flow — same pattern as the pre-existing `--tldr`/`--docs`/`--update-models` early returns. `pyproject.toml` declares one `[project.scripts]` entry: `qp = "quipcli:main"`.
- **Patchable globals** (`_API_KEY`, `_API_URL`, `_MODELS_CACHE`, `_RANKINGS_CACHE`, `_CONFIG_FILE`, `_CONFIG_DIR`, `_HISTORY_DB`, `_DATA_DIR`, `_CACHE_TTL`): live in `constants.py`. All functions that use them reference them via `from . import constants` + `constants._X` (module-qualified lookup), never `from .constants import _X`. This preserves test patchability at `quipcli.constants._X`.
- **Legacy-data migration** (`_migrate_legacy_data` in `constants.py`, run once at import time): if the new XDG dirs (`~/.config/quipcli` etc.) don't have a file yet but the pre-rename `~/.config/llm-cmd` (etc.) does, copies `config.json`/`models.json`/`history.db` over — one-time, non-destructive, no deletion of the old files.
- **HTTP layer** (`_make_request`): direct `http.client` calls, zero third-party deps except `argcomplete`; retries with backoff (1s/2s/4s) on connection errors and transient statuses (429/500/502/503/529); supports `http://` endpoints (no API key required for those); returns `(response, model_used)`; an optional `extra: dict` param merges arbitrary top-level request fields (e.g. `{"tools": [...]}`) into the body without growing the named-parameter list — used by `agent.py`'s tool-calling loop
- **Stdin + words** (`get_content` in `cli.py`): piped stdin is appended after the word prompt (blank-line separated) — `git diff | qp summarize this` sends both; stdin alone is the whole prompt
- **Ollama fallback** (`_ollama_fallback` in `http_client.py`): when the provider is unreachable after retries, or no API key is set, falls back to local Ollama (`LLM_CMD_OLLAMA_URL`, default `http://localhost:11434`); model from config `ollama_model` or first of `/api/tags` (`_pick_ollama_model`); HTTP status errors (401…) do NOT trigger it
- **Streaming** (`call_llm_streaming`): SSE parsed line-by-line, tokens printed as received; returns `_UsageStats | None`. With `show_reasoning=True` (chat/`-c`, unless `-q`), `delta.reasoning` tokens are printed dimmed to stderr (TTY only) before the content — reasoning models otherwise look stalled for minutes
- **Reasoning effort** (`-r/--reasoning`, config `reasoning_effort`): `_reasoning_extra` (`entry.py`) builds `{"reasoning": {"effort": X}}` (CLI > config > absent = model default) and passes it as `extra` to every mode (`call_llm_streaming`, `call_llm_capture`, `run_agent_loop` — which merges it alongside `tools`). Valid values in `constants.REASONING_EFFORTS`; `_check_reasoning_support` (`models.py`) warns — never exits, the cache may be stale — when the cached model's `reasoning.supported_efforts`/`mandatory` says the effort won't be accepted (silent on cache miss)
- **Ctrl-C**: `main()` wraps `_main()` and turns `KeyboardInterrupt` into ANSI reset + `Interrupted.` + exit 130 (no traceback)
- **Markdown rendering**: chat streaming applies lightweight ANSI markdown styling on TTY (headings, inline/fenced code, bold, list items, blockquotes) without buffering full responses; disabled by `NO_COLOR` or non-TTY output
- **Execute mode** (`confirm_and_run`): captures full response, strips markdown fences, prompts `[Y/n/e]` (Y is default)
- **Agent mode** (`-a`, `run_agent_loop` in `agent.py`): non-streaming, multi-turn tool-calling loop over the same `/chat/completions` endpoint — each step attaches `tools` (`tools.py`'s `default_tools()`: local `function` tools `run_shell`/`read_file`/`write_file` + OpenRouter's hosted `openrouter:web_search`/`openrouter:web_fetch` server tools unless `--no-web`), and if the model's response has `tool_calls` of `type == "function"`, `execute_tool_call` runs it locally and the result is appended as a `role: "tool"` message before looping again; hosted server tools never surface a `tool_call` here — OpenRouter resolves those itself server-side before responding, so there's no client code for them beyond listing them in `tools`. `run_shell`/`write_file` prompt `[Y/n]` first (same confirm-first posture as `-e`); declining feeds `"User declined..."` back to the model instead of erroring. Stops after `--max-steps` (default 12); token/cost usage accumulates across all steps into one `_UsageStats`.
- **Edit mode**: `e` in confirm_and_run opens `$EDITOR` with the original prompt and proposed command as context (comment lines stripped on save); `_edit_text_value` (`execute.py`) is the same tempfile+`$EDITOR` pattern without the comment header, used by `--tui`'s Config view for plain-text fields
- **Model cache** (`~/.cache/quipcli/models.json`): loaded for tab-completion, refreshed every 12h via detached subprocess (`_maybe_update_models_bg`)
- **Usage ranking** (`~/.cache/quipcli/rankings.json`, `qp --update-rankings`): `_fetch_rankings` (`models.py`) hits OpenRouter's `/api/v1/datasets/rankings-daily` (needs an OpenRouter API key — unlike the public `/models` endpoint — and only runs when `_API_URL` is the default OpenRouter endpoint, not a custom provider), keeps the most recent day's top-50-by-tokens, sorts descending, and caches `{rank, model_permaslug, total_tokens}` rows. `_ranking_for(permaslug)` looks up a row by the model's `canonical_slug` (not its `id` — OpenRouter's ranking dataset keys models by the dated canonical slug, e.g. `openai/gpt-4o-2024-05-13`, not the alias `id`). This is a usage-volume signal, not a quality/intelligence score — OpenRouter doesn't publish one; `_print_model_info` labels it as such wherever it's shown. Absent cache → no ranking line, no error.
- **Model name resolution** (`_resolve_model_name` in `models.py`): `-m/--model` and `--model-set` accept a substring that uniquely matches a cached model id (e.g. `-m haiku`); ambiguous matches list candidates and exit, no match passes the name through unchanged. `--model-set` with no argument launches the `--tui` model picker if `fzf` is installed, else falls back to a numbered picker read from stdin.
- **Interactive TUI** (`tui.py`, `--tui`): shells out to real `fzf`/`bat` binaries (same external-tool pattern as `$EDITOR`, no reimplemented fuzzy-finder); `_run_fzf` wraps `subprocess.run(["fzf", ...])` with shared layout/color chrome (`_FZF_LAYOUT`, `_FZF_COLORS` — bordered box, `--header-first`, per-view `--border-label`). Two views: `_models_view(picker_mode)` (standalone sets `default_model` on Enter; `picker_mode=True` — used from the Config view and from `--model-set`'s fallback — just returns the picked id; current default is highlighted green via ANSI, `--ansi` enabled) and `_config_view()` (drills into `_models_view`/local Ollama list/`REASONING_EFFORTS` list for enum-like keys, `_edit_text_value` for any key in `_PROMPT_KEYS`, `ctrl-e` opens the full file in `$EDITOR`). `_CONFIG_KEYS` lists all editable keys, in order; `_config_lines` column-aligns them. Preview panes call back into `qp` itself via hidden flags (`--_tui-model-info`, `--_tui-list-models`, `--_tui-config-lines`, all cache-only/no network since fzf invokes them per keystroke); `_print_model_info` additionally shows the usage rank (via `_ranking_for`, if `--update-rankings` has been run) and the model's `description` from the cache, wrapped to `$FZF_PREVIEW_COLUMNS`.
- **Config** (`~/.config/quipcli/config.json`): persistent default model; priority: env var > config file > hardcoded fallback. Auto-created with current defaults on first run (`_ensure_config` in `config.py`, called from `main`) so the file always exists and can be hand-edited in place; `qp --config-edit` opens it in `$EDITOR`. Keys: `default_model`, `reasoning_effort`, `chat_system_prompt`, `execute_system_prompt`, `code_system_prompt`, `agent_system_prompt`, `system_prompt`, `ollama_model`. The four `*_system_prompt` keys are seeded with their `constants.py` defaults via `_seed_defaults` (`config.py`) on every `main()` call — unlike `default_model`, it's fine to always-seed these since there's no env var to mask; `_seed_defaults` never overwrites a key that already exists (including a deliberately blank one), so this only fills gaps on first run or upgrade.
- **File writes**: config and model cache written via `_atomic_write_text` (temp + `os.replace`) in `constants.py`; SQLite opened with `timeout=5` + WAL; `$EDITOR` invoked via `subprocess.run` + `shlex.split` (never `os.system`)
- **System prompt injection** (`_default_system` in `entry.py`): unless `-S` fully overrides it, every call's system prompt is built from `_mode_prompt(cfg, mode)` (chat/execute/code/agent — reads `{mode}_system_prompt` from config, falling back to the matching `constants.py` default: `DEFAULT_CHAT_SYSTEM_PROMPT`, `DEFAULT_EXECUTE_SYSTEM_PROMPT`, `CODE_SYSTEM_PROMPT`, `DEFAULT_AGENT_SYSTEM_PROMPT`; a literal `"{shell}"` in either the config value or the default is substituted with the current `$SHELL` basename at call time — never persisted as a fixed shell name) + `_machine_context()` (recomputed every call — never cached/stored, so one config.json stays correct across different machines) + the optional `system_prompt` key from config.json (free-text standing instructions/preferences layered on top of the per-mode prompt, regardless of mode). Prompt wording is advisory; hard constraints it describes are separately enforced in code where it matters (e.g. `confirm_and_run` strips ``` fences from `-e` output unconditionally — `-c` output is streamed live and has no equivalent code-level strip, so it currently relies on `code_system_prompt` alone).
- **History** (`~/.local/share/quipcli/history.db`): SQLite, one row per LLM call (timestamp, model, tokens, cost, mode)
- **Usage stats**: printed to stderr after each response unless `-q/--quiet` or stdout not a TTY; `-q` also silences the informational `Model:`/`Session:` stderr lines
- **Provider config**: resolved at module level from env vars — changing provider requires no code changes
- **Model routing**: `-m/--model` passes its argument straight through to the API, so OpenRouter's model-string conventions (`openrouter/auto-beta` Auto Router, `openrouter/pareto-code` Pareto Router, `:nitro`/`:thinking`/`:extended`/`:free` variant suffixes) already work with zero `qp`-specific code — documented in README/`_DOCS`, not implemented as flags.
- **Entry point**: `qp` only (`pyproject.toml` → `quipcli:main`)

Branch strategy: `main` = stable tagged releases, `dev/*` = feature branches, merge to main when tests pass.

## Extending OpenRouter integration

When adding or updating OpenRouter-specific features (new server tools, routers, API fields, etc.), consult `https://openrouter.ai/docs/llms.txt` first — it's a manageable (~40KB) index of links to individual doc pages in Markdown (append `.md` to any docs URL for the raw source). Avoid `https://openrouter.ai/docs/llms-full.txt` (~3.5MB, the entire doc set) unless you specifically need to grep across everything; fetch only the specific page(s) `llms.txt` points to for the feature at hand.

## Documentation rule

**Every feature addition or behaviour change must update:**
1. `_TLDR` in `quipcli/docs.py` (quick reference)
2. `_DOCS` in `quipcli/docs.py` (man-page style)
3. `README.md` (user-facing)
4. `CLAUDE.md` Architecture section (this file)

## Git rules

- Every `git commit` is immediately followed by `git push origin <branch>` — local-only commits do not exist.
- Install with `uv tool install -e .` (never `pip install`).
