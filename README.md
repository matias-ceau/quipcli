# quipcli

Minimal CLI for LLMs. Ask questions or run AI-generated shell commands — **no quotes needed**.

```bash
qp what is the meaning of anagnorisis
qp -e update all my cargo binaries
qp -c write a python function that flattens a nested list
qp -a summarize what changed in this repo today and note it in CHANGES.md
qp                      # interactive chat session (same as qp -t)
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
qp [-e|-c|-a|-C] [-m MODEL] [-r EFFORT] [-P SYSTEM] [-s SESSION|-f] [-i FILE] [-q] [words ...]
```

| Flag | Description |
|------|-------------|
| *(none)* | Ask a question, stream the answer |
| `-e` | Generate a shell command, confirm `[Y/n/e/r]`, run it (only the command is shown, in the prompt; stray ``` fences / `$ ` are stripped). `e` edits it in `$EDITOR`, `r` asks what should change and gets a new command. Mutually exclusive with `-c`/`-a` |
| `-c` | Generate code, print to stdout. Mutually exclusive with `-e`/`-a` |
| `-a` | Agent mode: multi-turn tool-calling loop — see below. Mutually exclusive with `-e`/`-c` |
| `-C` | Chat mode — the default; only needed with `-f`, which otherwise keeps the mode of the turn it follows |
| `--max-steps N` | With `-a`: max tool-calling loop iterations (default: 4) |
| `--no-web` | With `-a`: disable the hosted web search/fetch tools for this call |
| `-m MODEL` | Override model (default: `openrouter/pareto-code`, OpenRouter's Pareto coding router). MODEL may be a substring matching a single cached model, e.g. `-m haiku` |
| `-r EFFORT` | Reasoning effort for this call (`max`/`xhigh`/`high`/`medium`/`low`/`minimal`/`none`) — see below |
| `-P PROMPT` | Override system prompt |
| `-f` | Follow up: resends this terminal's last exchange only, in the same mode — see [Sessions](#sessions) |
| `-s REF` | Continue a session by name or id prefix, or start one named `REF` |
| `-S` | List sessions (● = current) |
| `-i FILE` | Attach a file (repeatable): images/PDF/audio/video (`.mp3 .wav .ogg .flac .m4a .aac .aiff`, `.mp4 .webm .mov .mpeg`) as multimodal content, any other text file inlined into the prompt |
| `-q` | Quiet: suppress usage stats, informational stderr lines, and the reasoning display |
| `--models` [`--in`/`--out`] [`-t`] | List cached models with usage rank, price per 1M tokens and context (marked default; ids only when piped), filterable by modality; `-t` opens the fzf picker |
| `--model-get` / `--model-set [MODEL]` | Print / set the default model |
| `--config-edit` | Open `config.json` in `$EDITOR` |
| `--status` | Show current configuration |
| `--cost [PERIOD]` | Usage cost summary (`1d`/`7d`/`30d`/`all`) |
| `-t` | Interactive: alone (or with `-f`/`-s`/`-e`/`-c`/`-a`) a chat session, words name it; with `-S` the session picker; with `--config-edit` the config picker — see below. A bare `qp` on a terminal = `qp -t` |
| `--update-models` | Force-refresh model cache |
| `--update-rankings` | Cache OpenRouter's top-50 daily usage ranking (needs an OpenRouter key; usage volume, not a quality score — shown in the model picker's preview once cached) |
| `--shell-init bash` | Print the raw-line shell integration — see [No quotes, really](#no-quotes-really) |
| `--version` | Print version |

Chat responses render lightweight ANSI markdown styling on TTYs (headings, code spans/blocks, bold, list items, blockquotes) while still streaming token-by-token. Disable colors with `NO_COLOR=1`. A provider error that arrives mid-stream is printed as `Error: …`, and an empty reply prints a warning with its `finish_reason` rather than exiting silently.

Stdin is also supported, alone or combined with a prompt (piped content is appended after the words):

```bash
cat error.log | qp            # stdin as the whole prompt
git diff | qp summarize this  # words + piped context
git diff | qp -e write a conventional commit command
```

When the prompt comes from a pipe, `-e`'s and `-a`'s confirmations are read from the terminal itself (`/dev/tty`), and the command you confirm gets the terminal as its stdin.

## No quotes, really

The shell splits a command line before `qp` ever sees it, so `qp what's up` opens a quote and a `;`, `&`, `(`, `$` or `*` in a question is the shell's to interpret. `qp` cannot prevent that from the inside; a readline hook can:

```bash
# ~/.bashrc
eval "$(qp --shell-init bash)"
```

On Enter, a line that is a `qp` call (`qp …` or `… | qp …`) whose prompt contains such characters is rewritten to `qp --raw '…'` with the prompt single-quoted, then run. Lines without special characters, and every other command, are left exactly as typed.

```bash
qp what's up; any (real) idea?    # runs: qp --raw 'what'\''s up; any (real) idea?'
qp isn't this long | less         # the pipe still works
```

- Everything from the first ` | `, ` && `, ` > ` or ` >> ` on stays normal shell. `QP_RAW_PIPES=0` makes those literal too (for questions *about* pipes).
- In a raw line, flags come first: from the first plain word on, everything is prompt text — so `qp what does ls -la do` no longer trips over `-la`.
- Shell history keeps the quoted form (it is valid with or without the hook).
- bash only for now (emacs and vi-insert keymaps).

## Sessions

Every prompt belongs to a session. A plain `qp …` starts a new one, with a 6-hex-digit id and a short name made from the prompt's first significant words (shown in the stats line). `-f` continues the *current* session — the last one you sent a prompt to **from this terminal** (tty + shell session; a terminal that hasn't asked anything yet falls back to the last session used anywhere); `qp -S`, `qp --status` or `qp -h` never change it.

```bash
qp how do I update the debtap database   # new session "update-debtap-database"
qp -f and what if it stalls ?            # same session
qp -S                                    # list sessions, most recent first
qp -s 3f9a explain again                 # resume by id prefix…
qp -s myproject explain the architecture # …or by name (created if it doesn't exist)
```

### What `-f` carries

A follow-up is for refining a question or criticising a command, not for replaying a conversation — especially with small models. So a one-shot `qp -f`:

- **resends only the last exchange**: the previous prompt and its reply (config `follow_history` = number of exchanges, default `1`; `0` = no narrowing);
- **keeps that turn's mode**: after `qp -e …`, `qp -f use rsync instead` returns a new command. A mode flag overrides — `-C` for plain chat (`qp -f -C why that flag?`);
- **knows what happened to the command**: after `-e`, the session notes whether it ran and its exit status, so `qp -f -C why did it fail` has something to go on. (Only the status — the command's output goes straight to your terminal and is not captured.)

```bash
qp -e copy the photos dir to the nas     # [Y/n/e/r] — r: "use rsync" → new proposal
qp -f keep the timestamps                # still execute mode, sees the last command
qp -f -C what does -a include            # same context, plain answer
```

`-s` and the interactive session resend up to the last 40 messages (config `session_history`, `0` = all), always starting on a user turn. Attachments (`-i`, auto-detected media) are sent once: history keeps a marker, not the file, so they are never re-sent — or re-billed — on later turns; attach again to ask again. `qp --status` shows the current session. Sessions recorded by older versions (`-s name`) are migrated automatically and keep their name.

## Agent mode (`-a`)

```bash
qp -a find the largest log file under /var/log and tell me what's in it
qp -a --max-steps 5 --no-web refactor the imports in quipcli/cli.py
```

Runs a short multi-turn tool-calling loop instead of a single one-shot reply — a couple of autonomous steps for small jobs, not a coding agent — so it can go beyond oneliners: read files, run shell commands, write files, and (unless `--no-web`) search/fetch the web via OpenRouter's hosted `openrouter:web_search`/`openrouter:web_fetch` server tools — those two run entirely on OpenRouter's side, so there's nothing extra to configure or pay for beyond your normal API usage.

`run_shell` and `write_file` always prompt `[Y/n]` before doing anything — same confirm-first posture as `-e`. Declining just tells the model to try another approach; it doesn't abort the run. `read_file` runs without confirmation. A dim `thinking…` line (and the model's reasoning, if any) shows on stderr between steps. If `--max-steps` (default 4) runs out, `qp` makes one last call with tools disabled so the model summarizes what it found. Steps are not streamed, so each may take up to 5 minutes before timing out; a request that was sent but got no answer is never retried (it could be billed twice). Long command output is cut in the middle — the model sees the start and, mostly, the end, where the errors are. The hosted web tools are only sent to OpenRouter — other providers (and the Ollama fallback) would reject them.

The prompt for this mode lives in `agent_system_prompt` in `config.json`, seeded like the other per-mode prompts — edit it with `qp --config-edit` (add `-t` for the picker).

## Model routing (no code needed)

`-m/--model` passes whatever string you give it straight to the API, so OpenRouter's own routing features and model-variant suffixes already work without any `qp`-specific flag:

```bash
qp -m openrouter/auto-beta what's the fastest way to sort a linked list  # Auto Router: picks a model per-prompt
qp -m openrouter/pareto-code -c write a merge sort in rust               # Pareto Router: best coder for the price
qp -m "anthropic/claude-3-5-haiku:thinking" solve this step by step      # :thinking / :nitro / :extended / :free variants
```

See OpenRouter's [Auto Router](https://openrouter.ai/docs/guides/routing/routers/auto-router) and [Pareto Router](https://openrouter.ai/docs/guides/routing/routers/pareto-router) docs for details. `qp --model-set openrouter/auto-beta` persists one of these as your default the same way as any other model name. The stats line always shows the model that actually answered (`[openrouter/auto → deepseek/deepseek-v4.1-flash | 38 tok | $0.0000]`), and that real model is what's recorded in history.

### Offline / Ollama fallback

If the provider is unreachable (after automatic retries) or no API key is set, `qp` transparently falls back to a local [Ollama](https://ollama.com) instance when one is running. The local model comes from the `"ollama_model"` config key, or the first available model otherwise. Override the Ollama URL with `LLM_CMD_OLLAMA_URL` (default `http://localhost:11434`). Retries honour the provider's `Retry-After` header (capped at 30 s).

## Interactive mode (`-t`)

```bash
qp                     # new interactive session (or: qp -t)
qp -t myproject        # resume "myproject", or start a session with that name
qp -f -t               # continue the current session interactively
qp -e -t               # interactive execute mode: each reply is a command to confirm/run
qp -S -t               # search sessions with fzf
```

A prompt loop built on [prompt_toolkit](https://python-prompt-toolkit.readthedocs.io): `Enter` sends, `Alt-Enter` inserts a newline, `Ctrl-C` clears the line or interrupts a reply, `Ctrl-D` quits; input history persists and the bottom bar shows model · reasoning · mode · session. Every exchange is recorded like a one-shot `qp`, so `qp -f` continues it later. Resuming prints the last messages.

| Command | |
|---|---|
| `/model [name]` | switch model (fzf picker without a name) |
| `/reasoning [effort\|default]` | reasoning effort for the rest of the session |
| `/mode [chat\|code\|exec\|agent]` | switch mode |
| `/attach PATH…` | attach files to the next message |
| `/new [name]` · `/sessions` · `/session` | new / switch (fzf) / show session |
| `/config` · `/help` · `/exit` | config picker, help, quit |

Commands and their arguments (models, efforts, paths) are Tab-completed.

### fzf pickers

Shells out to the real `fzf` (and `bat`, for preview syntax highlighting) — no reimplemented fuzzy-finder, and it inherits your existing `FZF_DEFAULT_OPTS` if you have one.

- **Sessions** (`qp -S -t`, `/sessions`) — the query searches session names *and message contents* (every term must match, case-insensitive), re-queried from SQLite as you type; the preview shows each hit highlighted in context, then the end of the conversation. `Enter` makes the session current (continue with `qp -f`), `Alt-Enter` opens it interactively.
- **Models** (`qp --models -t`, `qp --model-set -t`, `/model`) — fuzzy list of cached models with usage rank, price per 1M tokens (in/out) and context columns, current default marked `*`. The preview pane shows name, context length, price per 1M tokens (prompt/completion), input/output modalities, usage rank (once `qp --update-rankings` has been run), and the model's description from the provider. `Enter` sets the highlighted model as the new default; `ctrl-r` refreshes the cache from the provider without leaving the picker.
- **Config** (`qp --config-edit -t`, `/config`) — fuzzy list of editable keys (`default_model`, `reasoning_effort`, `chat_system_prompt`, `execute_system_prompt`, `code_system_prompt`, `agent_system_prompt`, `system_prompt`, `ollama_model`), with the live `config.json` shown via `bat` on the right. `Enter` on `default_model` drills into the Models list; on `reasoning_effort`, into a list of effort levels (or `(model default)` to remove the key); on `ollama_model`, into a list of locally available Ollama models (falls back to free-text entry if Ollama is unreachable); on any `*_system_prompt` key, opens `$EDITOR` on just that value. `ctrl-e` opens the whole config file in `$EDITOR` at any time.

`Esc` steps back one level. Requires `fzf` — `qp --model-set` (no value) falls back to a plain numbered prompt without it.

## Configuration

`qp` keeps a persistent config file at `~/.config/quipcli/config.json`, created automatically on first run. Edit it in place with any text editor, or interactively:

```bash
qp --model-set            # pick a default model interactively (fzf, or a numbered list)
qp --model-set haiku      # set by substring match
qp --config-edit          # open config.json in $EDITOR
qp --config-edit -t       # or browse/edit everything with fzf
```

Most of these have no config.json equivalent at all (API key, API URL, Ollama URL are env-only). The one exception is the model: where both `LLM_CMD_MODEL` and `config.json`'s `default_model` are set, the environment variable wins.

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENROUTER_API_KEY` | — | API key (required unless `LLM_CMD_API_KEY` set, or the provider is a local `http://` endpoint) |
| `LLM_CMD_MODEL` | config file, or `openrouter/pareto-code` | Default model |
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

Every request (unless `-P` fully overrides the system prompt) automatically gets:

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
