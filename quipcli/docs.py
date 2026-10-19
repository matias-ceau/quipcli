_TLDR = """\
qp — quick reference

  qp what is anagnorisis               free-text question (no quotes needed)
  git diff | qp summarize this         piped stdin is appended to the prompt
  qp describe this photo.jpg           multimodal: auto-detect files in words
  qp -i photo.jpg what is this         multimodal: explicit file input
  qp -i notes.md summarize             text files are inlined into the prompt
  qp -e update all cargo bins          generate + confirm + run a shell command
  qp -c write a merge sort             generate code to stdout
  qp -a find and fix the failing test  agent mode: multi-turn tool-calling loop
  qp -a --no-web ...                   agent mode without hosted web search/fetch
  qp -m anthropic/claude-3 ...         use a specific model
  qp -m openrouter/auto-beta ...       Auto Router: picks a model per-prompt
                                        (stats show "router → real model")
  qp -m openrouter/pareto-code -c ...  Pareto Router: best coder for the price
  qp -r low ...                        reasoning effort for this call
                                        (max/xhigh/high/medium/low/minimal/none;
                                        persistent: config "reasoning_effort")
  qp -q ...                            suppress usage stats (and reasoning display)
  NO_COLOR=1 qp ...                    disable ANSI markdown styling
  qp -f what did I just ask            continue the current (last) session
  qp -s myconv ask something           continue session "myconv" (name or id
                                        prefix), or start one with that name
  qp -S                                list sessions (● = current)
  qp -P "be terse" ...                 override the system prompt

  qp --models                          list cached models (* = default)
  qp --models --in image               models accepting image input
  qp --models --out audio              models that can generate audio
  qp --model-get                       print current default model
  qp --model-set openai/gpt-4o         set default model (saved to config)
  qp --model-set haiku                 set by unique substring match
  qp --model-set                       pick a model (fzf if available, else a numbered list)
  qp --config-edit                     open ~/.config/quipcli/config.json in $EDITOR
                                        (chat/execute/code/agent_system_prompt are
                                        seeded with editable defaults; add
                                        "system_prompt" for standing instructions)
  qp   (or qp -t)                      interactive chat session (prompt_toolkit)
  qp -t myproject                      interactive session "myproject" (resume/create)
  qp -f -t / qp -e -t / qp -a -t       continue / execute / agent, interactively
  qp -S -t                             search sessions (fzf): enter = make current,
                                        alt-enter = open interactively
  qp --config-edit -t                  fzf config picker (qp --model-set: model picker)
  qp --status                          show current configuration + machine context
  qp --cost [1d|7d|30d|all]            show cost summary (default 7d)

  qp --update-models                   refresh model cache from provider
  qp --update-rankings                 cache OpenRouter's top-50 usage ranking
                                        (needs an OpenRouter API key; usage
                                        volume, not a quality score — shown in
                                        the model picker's preview when cached)
  qp --version                         show version
  qp --tldr                            this cheatsheet
  qp --docs                            full documentation

  Offline? If the provider is unreachable (or no API key is set), qp
  falls back to a local Ollama (config "ollama_model" or first available).\
"""

_DOCS = """\
qp(1)                               User Commands                               qp(1)

NAME
    qp — minimal CLI for LLMs, with an interactive mode (-t) and fzf pickers

SYNOPSIS
    qp [-e|-c|-a] [-m MODEL] [-r EFFORT] [-P SYSTEM] [-s SESSION|-f] [-i FILE] [-q] [words ...]
    qp [-t] [-e|-c|-a] [-f | -s SESSION | NAME]
    qp -S [-t]
    qp --update-models | --update-rankings | --models [--in MOD] [--out MOD] | --model-get | --model-set [MODEL]
    qp --config-edit [-t] | --status | --cost [PERIOD]
    qp --tldr | --docs | --version

DESCRIPTION
    qp sends a prompt to an LLM and streams the response to stdout.
    No quoting is needed — words on the command line are joined into the prompt.
    Files (images, PDFs, audio, video) are auto-detected by extension in the
    word list, or supplied explicitly with -i.
    In chat mode on TTY output, markdown gets lightweight ANSI styling
    (headings, inline/fenced code, bold, list items, blockquotes) while
    preserving streaming behavior. A mid-stream provider error is printed
    as "Error: ..." on stderr; an empty reply prints a warning with the
    finish_reason instead of exiting silently.

    Everything is a single binary controlled by flags — there are no
    separate subcommand executables.

OPTIONS
    words               Prompt words, joined with spaces. Files detected by
                        extension are passed as multimodal content. Piped stdin
                        is appended after the words (blank-line separated), so
                        `git diff | qp summarize this` sends both; with no
                        words at all, stdin becomes the whole prompt.

    -e, --execute       Execute mode: generate a shell command, confirm, then run.
                        Prompts [Y/n/e] — Y is default (Enter to confirm).
                        The reply is streamed silently (reasoning still shown
                        dimmed on stderr) and only the command is displayed,
                        in the prompt; a leading ``` fence or "$ " is stripped.
                        Press e to open the command in $EDITOR with context.
                        Mutually exclusive with -c/-a.

    -c, --code          Code mode: generate raw code to stdout (no prose).
                        Mutually exclusive with -e/-a.

    -a, --agent         Agent mode: multi-turn tool-calling loop instead of a
                        single reply — see AGENT MODE below. Mutually
                        exclusive with -e/-c.

    --max-steps N       With -a: max tool-calling loop iterations (default: 12).

    --no-web            With -a: disable the hosted web search/fetch tools
                        for this call.

    -m, --model MODEL   Model to use for this invocation. MODEL may be a substring
                        that uniquely matches a cached model id (e.g. "haiku");
                        the resolved id is printed to stderr.
                        Default: $LLM_CMD_MODEL, config file, or openai/gpt-4o-mini.

    -r, --reasoning EFFORT
                        Reasoning effort for this call, sent as OpenRouter's
                        reasoning.effort: max, xhigh, high, medium, low,
                        minimal, none. Default: config "reasoning_effort", else
                        the model's own default (which can be "max" — e.g.
                        GLM may think for minutes before answering). Accepted
                        levels vary per model; "none" is rejected by models
                        whose reasoning is mandatory; qp warns on stderr
                        when the model cache says the effort is unsupported
                        (request still sent). In every mode (chat, -c,
                        -e, -a) reasoning tokens are shown dimmed on stderr
                        (TTY only, hidden by -q) so the call never looks
                        stalled. Ctrl-C at any point exits with status 130.

    -P, --system PROMPT Fully override the system prompt (skips machine context
                        and config "system_prompt" injection — use this for a
                        one-off, exact system message).

    -S, --sessions      List sessions, most recently active first, and exit.

    -s, --session REF   Continue the session whose name is REF, else whose id
                        starts with REF (ambiguous prefix → error); if none
                        matches, start a new session named REF.

    -f, --follow-up     Continue the current session — the last one a prompt
                        was sent to. Mutually exclusive with --session. The
                        session is announced on stderr.

    -i, --input FILE    Explicitly pass a file as input. Repeatable. Media
                        files (see MULTIMODAL) are attached as multimodal
                        content; any other UTF-8 text file is inlined into
                        the prompt as a fenced block; other binary files are
                        skipped with a warning. Media files are also
                        auto-detected from words by extension.

    -q, --quiet         Suppress post-response usage stats (model, tokens, cost),
                        the informational Model:/Session: stderr lines, and
                        the dimmed reasoning display.

    --update-models     Fetch and cache the model list from the provider, then exit.

    --update-rankings   Fetch and cache OpenRouter's top-50-models-by-daily-tokens
                        ranking, then exit. Requires an OpenRouter API key (the
                        public /models endpoint doesn't need one; this dataset
                        does) and only works against OpenRouter itself, not
                        other OpenAI-compatible providers. This is a usage-volume
                        signal, not a quality/intelligence score — OpenRouter
                        doesn't publish one. When cached, the model picker's preview
                        shows the model's rank and daily token count.

    --models            List cached models (* marks the current default), then exit.
      --in MOD,MOD       Filter by required input modalities (comma-separated).
      --out MOD,MOD       Filter by required output modalities.
                        Valid modalities: text, image, audio, video, file.

    --model-get         Print the current default model, then exit.

    --model-set [MODEL] Set the default model (written to config file), then exit.
                        MODEL may be a substring that uniquely matches a cached
                        model id. If omitted: launches the fzf model picker
                        (see PICKERS) when fzf is installed, otherwise falls back
                        to a numbered list read from stdin.

    --config-edit       Open the config file directly in $EDITOR, then exit.

    --status            Print current configuration: API URL, key (masked),
                        model, paths to config/cache/history, then exit.

    --cost [PERIOD]     Show usage cost summary, then exit.
                        PERIOD: 1d, 7d (default), 30d, or all.

    -t, --tui           Interactive — see INTERACTIVE SESSION and PICKERS.
                        Alone, or with -f/-s/-e/-c/-a: a chat session (words
                        name it, like -s). With -S: the fzf session picker.
                        With --config-edit: the fzf config picker. A bare
                        `qp` (no words, stdin a terminal) is the same as -t.

    --version           Print version and exit.

    --tldr              Show quick-reference cheatsheet and exit.

    --docs              Show this documentation and exit.

MULTIMODAL
    Supported input formats:
      Images : .jpg .jpeg .png .gif .webp  (also accepts https:// image URLs)
      PDFs   : .pdf
      Audio  : .mp3 .wav .ogg .flac .m4a .aac .aiff
      Video  : .mp4 .webm .mov .mpeg

    If the selected model does not support the required input modality, qp
    prints an error and lists compatible models from the cache.

AGENT MODE (-a)
    Runs a multi-turn tool-calling loop (OpenAI-style function calling over
    the same /chat/completions endpoint used everywhere else) instead of a
    single reply, so it can go beyond oneliners. Available tools:

      run_shell       Run a shell command and return stdout/stderr.
                       Prompts [Y/n] before running — same confirm-first
                       posture as -e. Declining tells the model to try a
                       different approach rather than aborting the run.
      read_file        Read a local text file. No confirmation.
      write_file       Write text to a local file (creates parent dirs).
                       Prompts [Y/n] before writing.
      web search/fetch OpenRouter's hosted openrouter:web_search /
                       openrouter:web_fetch server tools (unless --no-web).
                       These run entirely on OpenRouter's side — no local
                       code, no extra config, billed as normal API usage.
                       Only sent when the provider is OpenRouter (and never
                       to the Ollama fallback) — other providers reject them.

    While the model thinks, a dim "thinking…" line shows on stderr, and its
    reasoning (if any) is printed dimmed after each step (TTY only, hidden
    by -q). If --max-steps iterations (default 12) pass without a final
    answer, qp makes one last call with tools disabled so the model has to
    summarize what it found.
    Token/cost usage accumulates across all steps into one usage-stats line.

    The system prompt for this mode is agent_system_prompt in config.json,
    seeded like chat/execute/code_system_prompt — edit with --config-edit
    (-t for the picker).

MODEL ROUTING
    -m/--model passes its argument straight through to the API, so
    OpenRouter's own routing features and model-variant suffixes already
    work with no qp-specific flag:

      qp -m openrouter/auto-beta ...        Auto Router — picks a model
                                              per-prompt from live usage data
      qp -m openrouter/pareto-code -c ...   Pareto Router — best coding
                                              model for the price
      qp -m "MODEL:thinking" ...            :thinking / :nitro / :extended /
                                              :free model-variant suffixes

    See https://openrouter.ai/docs/guides/routing for details. Persist any
    of these as your default the same way as any other model name:
    qp --model-set openrouter/auto-beta.

    The usage-stats line always names the model that actually answered:
    when it differs from the one requested (a router, or the Ollama
    fallback) it reads "openrouter/auto → deepseek/...", and the real model
    is what goes into history/--cost.

SESSIONS
    Every prompt belongs to a session. Without -s/-f a new one starts,
    with a 6-hex-digit id and a short name taken from the prompt's first
    significant words (e.g. "update-debtap-database"); the name is shown in
    the usage-stats line. -f continues the current session; commands that
    send no prompt (-S, --status, -h…) never change which one is current.

    qp how do I update the debtap database   # new session
    qp -f and what if it stalls ?            # same session
    qp -S                                    # list (● = current)
    qp -s 3f9a explain again                 # resume by id prefix
    qp -s myproject explain the architecture # resume, or create "myproject"

    Only the last 40 messages of a session are resent (config
    "session_history"; 0 = everything), always starting on a user turn,
    to stay within the model's context window. --status shows the current
    session.

INTERACTIVE SESSION (-t)
    A prompt loop (prompt_toolkit) over the same session machinery: every
    reply is recorded and the session becomes current, so `qp -f` picks it
    up later. Enter sends, Alt-Enter inserts a newline, Ctrl-C clears the
    line (or interrupts a reply), Ctrl-D quits. Input history is kept in
    ~/.local/share/quipcli/prompt_history; the bottom bar shows model,
    reasoning effort, mode and session. Resuming shows the last messages.
    Commands (Tab-completed, including their arguments):

      /model [NAME]          switch model (fzf picker without a name)
      /reasoning [EFFORT]    set effort ("default" = config/model default)
      /mode [chat|code|exec|agent]
      /attach PATH…          attach files to the next message
      /new [NAME]            new session (or resume/create NAME)
      /sessions              switch session (fzf picker)
      /session               show the current session
      /config                fzf config picker
      /help, /exit

    In exec mode each reply is a command confirmed with [Y/n/e] and run;
    the session continues afterwards.

PICKERS (fzf)
    Built on the real fzf/bat binaries (not a reimplementation) — your
    FZF_DEFAULT_OPTS still apply.

      Sessions (qp -S -t, /sessions)
               The query searches session names and message contents (every
               term must match, case-insensitive), re-run on each keystroke.
               The preview lists each hit in context, highlighted, then the
               end of the conversation. Enter makes the session current
               (continue with qp -f); Alt-Enter opens it interactively.

      Models (qp --model-set, /model)
               Fuzzy list of cached models, current default marked with *.
               Right-hand preview: name, context length, price per 1M
               tokens (prompt/completion), input/output modalities, usage
               rank (only if --update-rankings has been run), and the
               model's description from the provider. Enter sets the
               highlighted model as the new default. ctrl-r refreshes the
               cache from the provider in place.

      Config (qp --config-edit -t, /config)
               Fuzzy list of editable keys: default_model, reasoning_effort,
               chat_system_prompt,
               execute_system_prompt, code_system_prompt, agent_system_prompt,
               system_prompt, ollama_model. Right-hand preview shows the live config.json
               via bat. Enter on default_model drills into the Models list;
               on reasoning_effort, into a list of effort levels (or
               "(model default)" to remove the key);
               on ollama_model, into a list of locally available Ollama
               models (falls back to free-text entry if Ollama is
               unreachable); on any *_system_prompt key, opens $EDITOR on
               just that value. ctrl-e opens the whole config file in
               $EDITOR at any time. Esc returns to the previous list.

OLLAMA FALLBACK
    When the provider is unreachable (after retries) or no API key is set,
    qp falls back to a local Ollama instance if one is running.
    The model is taken from the "ollama_model" config key, or the first
    locally available model otherwise. http:// endpoints (e.g. a permanent
    LLM_CMD_API_URL pointing at Ollama) never require an API key.
    Transient provider errors (429/5xx) are retried with backoff first
    (honouring a Retry-After header, capped at 30s); hosted openrouter:*
    tools are dropped from requests sent to Ollama;
    authentication errors (401…) do NOT trigger the fallback.

ENVIRONMENT
    LLM_CMD_MODEL       Default model name.
    LLM_CMD_API_KEY     API key (takes priority over OPENROUTER_API_KEY).
    LLM_CMD_API_URL     Full endpoint URL (default: OpenRouter).
    LLM_CMD_OLLAMA_URL  Local Ollama base URL (default: http://localhost:11434).
    OPENROUTER_API_KEY  OpenRouter API key (fallback).
    NO_COLOR            Disable ANSI markdown styling in streamed chat output.
    XDG_CACHE_HOME      Cache directory (default: ~/.cache).
    XDG_CONFIG_HOME     Config directory (default: ~/.config).
    XDG_DATA_HOME       Data directory (default: ~/.local/share).
    EDITOR              Editor for -e edit mode, --config-edit, and the pickers
                        text fields (default: vi).
    SHELL               Shell name used in execute-mode system prompt.

CONTEXT INJECTION
    Unless -P/--system fully overrides it, every request's system prompt is
    built from up to three parts, in order:
      1. the per-mode prompt for the mode in use — chat_system_prompt,
         execute_system_prompt, code_system_prompt, or agent_system_prompt
         in config.json. These four keys are seeded with sensible defaults
         the first time qp runs (or on upgrade, for any missing key), so they're plain,
         editable JSON — not buried in Python source — from the start.
         execute_system_prompt may contain the literal placeholder
         "{shell}", substituted with the invoking machine's actual $SHELL
         at request time (never baked in as a fixed name), so a
         config.json synced across machines with different shells stays
         correct. Editing these only changes wording/tone — the hard
         constraints they describe (e.g. "no markdown fences" for -e) are
         also enforced in code: confirm_and_run() strips ``` fences from
         -e output regardless of what the model or the prompt says.
      2. machine context — OS/distro, $SHELL, architecture — computed fresh
         on every call, so the same config.json works correctly across
         different machines without editing it per host
      3. the "system_prompt" key from config.json, if set — free-text
         instructions/preferences that should apply to every call regardless
         of mode (e.g. "prefer pacman over apt-get", "I use zsh and neovim")

    Set persistent instructions with:
        qp --config-edit          # edit chat/execute/code/agent_system_prompt,
                                   # or add "system_prompt": "..."
        qp --config-edit -t       # or interactively (fzf)

FILES
    ~/.config/quipcli/config.json       Persistent config: default_model,
                                         reasoning_effort,
                                         chat_system_prompt,
                                         execute_system_prompt,
                                         code_system_prompt,
                                         agent_system_prompt, system_prompt,
                                         ollama_model, session_history.
                                         Auto-created on first
                                         run (the four prompt keys are
                                         seeded with defaults); edit it
                                         directly or via `qp --config-edit`.
    ~/.cache/quipcli/models.json        Cached model list (12h TTL; a failed
                                         background refresh is retried at
                                         most once an hour).
    ~/.cache/quipcli/rankings.json      Cached OpenRouter usage ranking
                                         (only present after
                                         `qp --update-rankings`).
    ~/.local/share/quipcli/history.db   Usage history + sessions (SQLite;
                                         pre-existing named sessions are
                                         migrated, keeping their name).

    On first run, if the above don't exist yet but a pre-rename
    ~/.config/llm-cmd (etc.) layout does, its config/cache/history are
    copied over automatically (one-time, non-destructive).

USAGE STATS
    After each response, qp prints to stderr:
        [model | N tok | $0.0012]
    Suppressed with -q/--quiet or when stdout is not a TTY.\
"""
