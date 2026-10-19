# quipcli

Minimal CLI for LLMs. Ask questions or run AI-generated shell commands — **no quotes needed**.

```bash
qp what is the meaning of anagnorisis
qp -e update all my cargo binaries
qp -c write a python function that flattens a nested list
qp -a summarize what changed in this repo today and note it in CHANGES.md
```

Everything lives behind a single binary, `qp` — no separate subcommand executables.

## Install

```bash
uv tool install -e .
```

Requires Python ≥ 3.14 and an [OpenRouter](https://openrouter.ai) API key (or any OpenAI-compatible provider, or a local [Ollama](https://ollama.com)).

## Tab completion

```bash
# Add to .bashrc / .zshrc
eval "$(register-python-argcomplete qp)"

# Populate model cache (auto-refreshes every 12h)
qp --update-models
```

Then `qp -m <Tab>` completes model names. Substring match: typing `haiku` finds `anthropic/claude-3-5-haiku-20241022`.

## Usage

```
qp [-e|-c|-a] [-m MODEL] [-r EFFORT] [-S SYSTEM] [-s SESSION|-f] [-i FILE] [-q] [words ...]
```

| Flag | Description |
|------|-------------|
| *(none)* | Ask a question, stream the answer |
| `-e` | Generate a shell command, confirm `[Y/n/e]`, run it (only the command is shown, in the prompt; stray ``` fences / `$ ` are stripped). Mutually exclusive with `-c`/`-a` |
| `-c` | Generate code, print to stdout. Mutually exclusive with `-e`/`-a` |
| `-a` | Agent mode: multi-turn tool-calling loop — see below. Mutually exclusive with `-e`/`-c` |
| `--max-steps N` | With `-a`: max tool-calling loop iterations (default: 12) |
| `--no-web` | With `-a`: disable the hosted web search/fetch tools for this call |
| `-m MODEL` | Override model (default: `openai/gpt-4o-mini`). MODEL may be a substring matching a single cached model, e.g. `-m haiku` |
| `-r EFFORT` | Reasoning effort for this call (`max`/`xhigh`/`high`/`medium`/`low`/`minimal`/`none`) — see below |
| `-S PROMPT` | Override system prompt |
| `-s SESSION` / `-f` | Named session, or follow up on the last one |
| `-i FILE` | Attach a file (repeatable): images/PDF/audio/video (`.mp3 .wav .ogg .flac .m4a .aac .aiff`, `.mp4 .webm .mov .mpeg`) as multimodal content, any other text file inlined into the prompt |
| `-q` | Quiet: suppress usage stats, informational stderr lines, and the reasoning display |
| `--models` [`--in`/`--out`] | List cached models (marked default), filterable by modality |
| `--model-get` / `--model-set [MODEL]` | Print / set the default model |
| `--config-edit` | Open `config.json` in `$EDITOR` |
| `--status` | Show current configuration |
| `--cost [PERIOD]` | Usage cost summary (`1d`/`7d`/`30d`/`all`) |
| `--tui` | Interactive fzf-based picker for models and config — see below |
| `--update-models` | Force-refresh model cache |
| `--update-rankings` | Cache OpenRouter's top-50 daily usage ranking (needs an OpenRouter key; usage volume, not a quality score — shown in `--tui`'s model preview once cached) |
| `--version` | Print version |

Chat responses render lightweight ANSI markdown styling on TTYs (headings, code spans/blocks, bold, list items, blockquotes) while still streaming token-by-token. Disable colors with `NO_COLOR=1`. A provider error that arrives mid-stream is printed as `Error: …`, and an empty reply prints a warning with its `finish_reason` rather than exiting silently.

Stdin is also supported, alone or combined with a prompt (piped content is appended after the words):

```bash
cat error.log | qp            # stdin as the whole prompt
git diff | qp summarize this  # words + piped context
git diff | qp -e write a conventional commit command
```

## Agent mode (`-a`)

```bash
qp -a find the largest log file under /var/log and tell me what's in it
qp -a --max-steps 5 --no-web refactor the imports in quipcli/cli.py
```

Runs a multi-turn tool-calling loop instead of a single one-shot reply, so it can go beyond oneliners: read files, run shell commands, write files, and (unless `--no-web`) search/fetch the web via OpenRouter's hosted `openrouter:web_search`/`openrouter:web_fetch` server tools — those two run entirely on OpenRouter's side, so there's nothing extra to configure or pay for beyond your normal API usage.

`run_shell` and `write_file` always prompt `[Y/n]` before doing anything — same confirm-first posture as `-e`. Declining just tells the model to try another approach; it doesn't abort the run. `read_file` runs without confirmation. A dim `thinking…` line (and the model's reasoning, if any) shows on stderr between steps. If `--max-steps` (default 12) runs out, `qp` makes one last call with tools disabled so the model summarizes what it found. The hosted web tools are only sent to OpenRouter — other providers (and the Ollama fallback) would reject them.

The prompt for this mode lives in `agent_system_prompt` in `config.json`, seeded like the other per-mode prompts — edit it with `qp --config-edit` or `qp --tui`.

## Model routing (no code needed)

`-m/--model` passes whatever string you give it straight to the API, so OpenRouter's own routing features and model-variant suffixes already work without any `qp`-specific flag:

```bash
qp -m openrouter/auto-beta what's the fastest way to sort a linked list  # Auto Router: picks a model per-prompt
qp -m openrouter/pareto-code -c write a merge sort in rust               # Pareto Router: best coder for the price
qp -m "anthropic/claude-3-5-haiku:thinking" solve this step by step      # :thinking / :nitro / :extended / :free variants
```

See OpenRouter's [Auto Router](https://openrouter.ai/docs/guides/routing/routers/auto-router) and [Pareto Router](https://openrouter.ai/docs/guides/routing/routers/pareto-router) docs for details. `qp --model-set openrouter/auto-beta` persists one of these as your default the same way as any other model name.

### Offline / Ollama fallback

If the provider is unreachable (after automatic retries) or no API key is set, `qp` transparently falls back to a local [Ollama](https://ollama.com) instance when one is running. The local model comes from the `"ollama_model"` config key, or the first available model otherwise. Override the Ollama URL with `LLM_CMD_OLLAMA_URL` (default `http://localhost:11434`). Retries honour the provider's `Retry-After` header (capped at 30 s).

## Interactive TUI (`--tui`)

```bash
qp --tui
```

Shells out to the real `fzf` (and `bat`, for preview syntax highlighting) — no reimplemented fuzzy-finder, and it inherits your existing `FZF_DEFAULT_OPTS` if you have one.

- **Models** — fuzzy list of cached models, current default marked `*`. The preview pane shows name, context length, price per 1M tokens (prompt/completion), input/output modalities, usage rank (once `qp --update-rankings` has been run), and the model's description from the provider. `Enter` sets the highlighted model as the new default; `ctrl-r` refreshes the cache from the provider without leaving the picker.
- **Config** — fuzzy list of editable keys (`default_model`, `reasoning_effort`, `chat_system_prompt`, `execute_system_prompt`, `code_system_prompt`, `agent_system_prompt`, `system_prompt`, `ollama_model`), with the live `config.json` shown via `bat` on the right. `Enter` on `default_model` drills into the Models list; on `reasoning_effort`, into a list of effort levels (or `(model default)` to remove the key); on `ollama_model`, into a list of locally available Ollama models (falls back to free-text entry if Ollama is unreachable); on any `*_system_prompt` key, opens `$EDITOR` on just that value. `ctrl-e` opens the whole config file in `$EDITOR` at any time.

`Esc` steps back one level; `Esc` at the top menu exits. Requires `fzf` to be installed — `qp --model-set` (no value) also uses this picker when `fzf` is available, falling back to a plain numbered prompt otherwise.

## Configuration

`qp` keeps a persistent config file at `~/.config/quipcli/config.json`, created automatically on first run. Edit it in place with any text editor, or interactively:

```bash
qp --model-set            # pick a default model interactively (fzf, or a numbered list)
qp --model-set haiku      # set by substring match
qp --config-edit          # open config.json in $EDITOR
qp --tui                  # or browse/edit everything interactively
```

Most of these have no config.json equivalent at all (API key, API URL, Ollama URL are env-only). The one exception is the model: where both `LLM_CMD_MODEL` and `config.json`'s `default_model` are set, the environment variable wins.

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENROUTER_API_KEY` | — | API key (required unless `LLM_CMD_API_KEY` set, or the provider is a local `http://` endpoint) |
| `LLM_CMD_MODEL` | config file, or `openai/gpt-4o-mini` | Default model |
| `LLM_CMD_API_KEY` | `$OPENROUTER_API_KEY` | Override API key |
| `LLM_CMD_API_URL` | OpenRouter endpoint | Any OpenAI-compatible URL (http:// endpoints need no key) |
| `LLM_CMD_OLLAMA_URL` | `http://localhost:11434` | Local Ollama used as offline fallback |

### Reasoning models

Reasoning models (GLM, DeepSeek, Gemini, Claude…) think before answering. `qp` streams those reasoning tokens (OpenRouter's `reasoning`, or `reasoning_content` from DeepSeek/vLLM-style servers) **dimmed on stderr** in every mode — chat, `-c`, `-e`, `-a` — as they arrive (TTY only, hidden by `-q`), then the answer on stdout — so a long think never looks like a hang, and piping stdout stays clean. Ctrl-C exits cleanly (status 130), no traceback.

Some models default to a very high effort (e.g. `z-ai/glm-5.3-flash` defaults to `max` and can think for ~2 minutes). Control it with OpenRouter's `reasoning.effort`:

```bash
qp -r low what does debtap -u do        # one call
qp --config-edit                         # persistent: "reasoning_effort": "low"
```

Valid values: `max`, `xhigh`, `high`, `medium`, `low`, `minimal`, `none`. Accepted levels vary per model, and `none` is rejected by models whose reasoning is mandatory. Unset = the model's own default. The `-r` flag wins over the config key. When the model cache says the chosen effort isn't supported, `qp` prints a warning (but still sends the request — the cache may be stale).

### Persistent instructions + machine context

Every request (unless `-S` fully overrides the system prompt) automatically gets:

1. the **per-mode prompt** for whichever mode is active — `chat_system_prompt`, `execute_system_prompt`, `code_system_prompt`, or `agent_system_prompt` in `config.json`. These are seeded with sensible defaults the first time `qp` runs (or on upgrade, for any key missing from an existing config), so they're plain editable JSON from the start, not buried in the Python source. `execute_system_prompt` may contain the literal placeholder `{shell}`, substituted with the invoking machine's actual `$SHELL` at request time — never baked in as a fixed name, so a `config.json` synced across machines with different shells stays correct. Editing these only changes wording/tone: the hard constraint they describe (no markdown fences in `-e` output) is also enforced in code, independent of what the prompt says.
2. **machine context** — OS/distro, `$SHELL`, architecture — detected fresh on every call, so the same `config.json` is correct whether it's synced to an Arch box or a Mac. No more reminding the model what OS you're on.
3. a free-text `"system_prompt"` from `config.json`, if you set one — standing instructions/preferences applied to every call regardless of mode:

```bash
qp --config-edit
# add to config.json:
# { "default_model": "...", "system_prompt": "Prefer pacman over apt-get. I use zsh and neovim." }
```

### Provider examples

```bash
# Groq (fast)
export LLM_CMD_API_URL=https://api.groq.com/openai/v1/chat/completions
export LLM_CMD_API_KEY=$GROQ_API_KEY
export LLM_CMD_MODEL=llama-3.3-70b-versatile

# Direct OpenAI
export LLM_CMD_API_URL=https://api.openai.com/v1/chat/completions
export LLM_CMD_API_KEY=$OPENAI_API_KEY
export LLM_CMD_MODEL=gpt-4o-mini
```

## Migrating from `llm-cmd`

This project was renamed from `llm-cmd` to `quipcli` (command `qp`) — the old name collided with an unrelated PyPI package. On first run, if `~/.config/quipcli` (etc.) doesn't exist yet but a pre-rename `~/.config/llm-cmd` layout does, `config.json`, `models.json`, and `history.db` are copied over automatically (one-time, non-destructive — the old files are left untouched).

## Development

```bash
uv run pytest tests/ -v
uv run python -m quipcli <prompt>   # without installing
```

## Acknowledgements

Inspired by [aichat](https://github.com/sigoden/aichat) by [@sigoden](https://github.com/sigoden) — particularly its frictionless no-quote prompt UX and `-e` execute mode. `quipcli` is a minimal Python reimplementation focused on fast startup and zero configuration.
