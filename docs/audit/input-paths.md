# Input Path Audit — quipcli / `qp`

Scope: every way input/configuration reaches `qp` — CLI flags (`quipcli/cli.py`
`build_parser`), stdin/positional merging (`get_content`), env vars and config
file (`quipcli/constants.py`, `quipcli/config.py`), model resolution
(`quipcli/models.py`), and dispatch/precedence logic (`quipcli/entry.py`
`main()`). Verified against actual code (including live `argparse` probes),
not just docs. Read-only audit, no source changes made.

## Input sources & precedence (as implemented)

| Setting | Precedence (highest → lowest) | Notes |
|---|---|---|
| Model | `-m/--model` flag (if passed) → else `$LLM_CMD_MODEL` → `config.json:default_model` → hardcoded `openrouter/pareto-code` | Whatever value wins is *then* run through `_resolve_model_name` substring match against the model cache (`entry.py:245`). Argparse's `-m` default is itself `DEFAULT_MODEL`, pre-resolved once at import time in `config.py:55`. |
| API key | `$LLM_CMD_API_KEY` → `$OPENROUTER_API_KEY` → `""` | Env-only; no config.json key exists for this. |
| API URL | `$LLM_CMD_API_URL` → hardcoded OpenRouter endpoint | Env-only; no config.json key. |
| Ollama URL | `$LLM_CMD_OLLAMA_URL` → hardcoded `http://localhost:11434` | Env-only; no config.json key (distinct from `ollama_model`, which is config-only). |
| Ollama model | `config.json:ollama_model` → first `/api/tags` entry | Config-only; no env var. |
| System prompt (per mode) | `-S/--system` (full override, skips everything else) → else `config.json:{mode}_system_prompt` → `constants.py` hardcoded default for that mode → + machine context → + `config.json:system_prompt` | No env var pathway at all. |
| Session | `-f/--follow-up` and `-s/--session` are mutually exclusive, enforced at **runtime** in `db.py:_resolve_session` (not by argparse) | `--help` does not show this constraint. |
| Config file itself | N/A | `~/.config/quipcli/config.json`, auto-created empty on first run; malformed JSON silently treated as `{}` (see High-2). |

**Takeaway on "is precedence consistent":** No single rule applies everywhere — it varies by *category* deliberately (secrets/endpoints are env-only, textual preferences are config-only, model is the only setting with a full env→config→hardcoded chain). That split is reasonable, but README.md's blanket claim "Environment variables always take priority over the config file" overstates it — see Medium-1.

## Findings

### High

- **H1 — `--model-set`/`--cost` silently swallow following prompt words (argparse `nargs="?"` greediness).**
  `quipcli/cli.py:126-145` — both `--model-set` (`nargs="?", const=""`) and `--cost` (`nargs="?", const="7d"`) will greedily consume the very next token as their own value if present, even when the user meant it as the start of a prompt.
  Verified: `qp --model-set list all my files` → `args.model_set == "list"`, `args.words == ["all","my","files"]`. Since `_do_model_set` returns before ever touching `args.words`, this **silently sets the persistent default model to the literal string `"list"`** (via `_resolve_model_name`, which passes unmatched names through unchanged — `models.py:159`) and silently discards the rest of the prompt. Same issue for `--cost`: `qp --cost what is going on` → tries to treat `"what"` as the cost period, prints `Error: invalid --cost period 'what'` and exits, instead of running the prompt.
  Why it matters: a plausible real command (forgetting these flags take no bare positional in normal chat usage) corrupts persistent config with no confirmation, or produces a confusing unrelated error.
  Suggested fix: don't rely on `nargs="?"` sharing the same token stream as `words`; either require `=` syntax for a value (`--model-set=foo`), or validate/reject when the "value" doesn't look like a period/model token and the rest of `args.words` is non-empty.

- **H2 — Mode flags `-e`/`-c`/`-a` are not mutually exclusive; conflicts are silently resolved by an undocumented precedence order.**
  `quipcli/cli.py:36-50` defines `-e`, `-c`, `-a` as independent `store_true` flags with no `add_mutually_exclusive_group`. `quipcli/entry.py:287-318` dispatches with `if args.execute: … elif args.agent: … elif args.code: … else: chat`, i.e. **execute > agent > code > chat**.
  Verified: `qp -e -c do a thing` parses with both `execute=True, code=True`; execute mode silently wins, `-c` is silently ignored — no warning, nothing in `_TLDR`/`_DOCS`/README documents this precedence or that combining these flags is even meaningful.
  Why it matters: a user who fat-fingers or scripts `-e -c` (e.g. muscle memory from another tool) gets a different mode than intended with zero feedback.
  Suggested fix: put `-e/-c/-a` in an `add_mutually_exclusive_group()` so argparse rejects combinations outright, or explicitly warn/exit in `main()` when more than one is set.

- **H3 — `_do_model_get`/`_do_status` mislabel the model's source when both env var and config are set (duplicated bug).**
  `quipcli/entry.py:66-68` and `quipcli/entry.py:121-123` (identical logic in two places):
  ```python
  source = "config" if cfg.get("default_model") else (
      "env" if os.environ.get("LLM_CMD_MODEL") else "default"
  )
  ```
  This checks `config.json` *before* the env var. But the actual value shown (`_resolve_default_model()`, `config.py:47-52`) checks env *before* config. So when both `LLM_CMD_MODEL` and `config.json:default_model` are set to different values, `qp --model-get`/`qp --status` print the **env var's value** labeled as **`(config)`** — actively wrong, not just imprecise.
  Why it matters: this is the one diagnostic surface a user checks to understand why their model choice isn't taking effect; it lies about the cause.
  Suggested fix: reuse the same precedence check in one helper (e.g. `_model_source() -> str`) called by both `_do_model_get` and `_do_status`, checking env first.

### Medium

- **M1 — README's "Environment variables always take priority over the config file" is misleading.**
  `README.md` (Configuration section) states this as a blanket rule directly above a table that only lists 4 env vars (`OPENROUTER_API_KEY`, `LLM_CMD_MODEL`, `LLM_CMD_API_KEY`, `LLM_CMD_API_URL`, `LLM_CMD_OLLAMA_URL`). In reality most `config.json` keys (`ollama_model`, `chat_system_prompt`, `execute_system_prompt`, `code_system_prompt`, `agent_system_prompt`, `system_prompt`) have **no env var counterpart at all**, so "always take priority" only actually applies to `default_model`.
  Why it matters: sets a false expectation that, e.g., an env var could override `system_prompt` or `ollama_model`.
  Suggested fix: reword to "Where both exist (currently just the model), the environment variable wins" or scope the sentence to the table it precedes.

- **M2 — Malformed `config.json` fails silently to `{}`.**
  `quipcli/config.py:9-15` (`_load_config`) catches `json.JSONDecodeError`/`OSError` and returns `{}` with no message anywhere in the call chain. Every setting (default model, all four per-mode prompts, `system_prompt`, `ollama_model`) then silently reverts to hardcoded defaults with no indication the user's file is broken. `qp --status`/`--model-get` would show `(default)`/`(default)` source with no hint *why*.
  Why it matters: a hand-edit typo (this is explicitly a "hand-editable" file per `CLAUDE.md`/docs) silently discards all customization instead of erroring loudly.
  Suggested fix: on parse failure, print a one-line warning to stderr (e.g. `Warning: config.json is invalid JSON, ignoring: <path>`) instead of failing silently.

- **M3 — `--in`/`--out` modality filters are accepted but silently no-ops without `--models`.**
  `quipcli/cli.py:113-120` defines `--in`/`--out` as always-available top-level flags, but `quipcli/entry.py:216-218` only reads them inside `_do_models`, gated on `args.models`. `qp --in image what is this` parses fine (`in_filter="image"`), consumes no error, and simply ignores the flag while running a normal chat prompt.
  Why it matters: no validation ties `--in`/`--out` to requiring `--models`; a user combining them elsewhere gets silent no-op instead of an error explaining they're `--models`-only.
  Suggested fix: argparse-level note is already there in help text ("With --models: …"), but add a runtime check: if `in_filter`/`out_filter` set and `not args.models`, print a usage error.

- **M4 — `--max-steps` has no lower-bound validation; `0`/negative values produce a broken agent response.**
  `quipcli/cli.py:51-57` (`type=int`, no `choices`/validation). `quipcli/agent.py:35` uses `for step in range(max_steps)`; with `max_steps <= 0` the loop body never executes, so the code falls through to the "max steps reached" branch (`agent.py:82-92`) with `messages[-1]` still being the **original user message**, not any assistant reply. It then prints that user message back as if it were the agent's "final response" (and if the prompt was multimodal — `content` is a `list`, not `str` — `.strip()` on `agent.py:84` would raise `AttributeError` and crash uncaught).
  Why it matters: confusing/incorrect output for `--max-steps 0`, and an actual crash for the multimodal+`--max-steps 0` combination.
  Suggested fix: enforce `max-steps >= 1` in argparse (`type=` validator) or at the top of `run_agent_loop`.

### Low

- **L1 — `-m/--model` help text is redundant/confusing about its own default.**
  `quipcli/cli.py:63-68`: `help=f"Model to use (default: {DEFAULT_MODEL} or $LLM_CMD_MODEL)."` — `DEFAULT_MODEL` (`config.py:55`) is *already* resolved through the env-var check at import time, so if `$LLM_CMD_MODEL` is set, the printed default already **is** that env value, making "…or $LLM_CMD_MODEL" read as a second, separate option rather than the reason the shown default is what it is.
  Suggested fix: word it as "(current default: X — see \`qp --status\`)" instead of re-listing the env var as an alternative.

- **L2 — `-s/-f` mutual exclusivity isn't visible in `--help`.**
  Enforced correctly at runtime (`db.py:133-135`, clear error message), but since it's not an `argparse` mutually-exclusive group, `qp --help`/`_DOCS`'s SYNOPSIS line (`[-s SESSION|-f]`) is the only place the constraint is visible before actually hitting the runtime error. Cosmetic — behavior is already correct and documented in `_DOCS`.
  Suggested fix: optional — could still switch to `add_mutually_exclusive_group()` for self-documenting `--help` output, but low value since the manual check already gives a clear message.

- **L3 — Env var naming inherited from pre-rename package (`OPENROUTER_API_KEY` vs `LLM_CMD_*`).**
  `constants.py:37-43`. Known/intentional per `CLAUDE.md` history (package renamed `llm-cmd` → `quipcli`, binary `llm-cmd` → `qp`, but env vars were kept as `LLM_CMD_*` for backward compat plus the third-party-style `OPENROUTER_API_KEY`). Flagging per audit instructions, but this looks like a deliberate, documented tradeoff (avoids breaking existing shell configs) rather than an oversight — no action needed unless a `QP_*`/`QUIPCLI_*` rename is desired for consistency, which would be a breaking change.

- **L4 — Hidden `--_tui-*` flags run full config/model-cache bootstrapping on every fzf keystroke.**
  `quipcli/entry.py:173-175` runs `_maybe_update_models_bg()`, `_ensure_config()`, `_seed_defaults()` unconditionally before dispatching to *any* flag, including the hidden `--_tui-model-info`/`--_tui-list-models`/`--_tui-config-lines` preview flags that fzf invokes per keystroke (`tui.py:182-183`, `230`). Each call re-reads `config.json` from disk (`_ensure_config` → `_load_config`) and stats the model cache file. Functionally harmless (all cheap/local after first run) and consistent with `CLAUDE.md`'s stated design ("must stay fast — cache-only, no network"), but it's doing more file I/O per keystroke than strictly necessary.
  Suggested fix: skip the three bootstrap calls when any `tui_*` hidden flag is set, since they're irrelevant to a preview render.

## Not flagged (verified correct / already well-documented)

- `-S/--system` correctly skips machine-context + `system_prompt` injection but still includes session history (`entry.py:265-276`) — matches docs, which only claims to override the system prompt, not history.
- API key precedence (`LLM_CMD_API_KEY` > `OPENROUTER_API_KEY`), Ollama fallback triggers (unreachable/no key, not on 401), and stdin+words merging in `get_content` (`cli.py:193-212`) all behave exactly as `_TLDR`/`_DOCS`/README describe.
- `_resolve_model_name` substring-match behavior (unique match resolves, ambiguous exits with candidates listed, no match passes through unchanged) matches its own docstring and `_DOCS`'s `-m`/`--model-set` descriptions.
