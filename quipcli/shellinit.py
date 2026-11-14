"""Raw-line shell integration: `eval "$(qp --shell-init bash)"`.

The shell tokenizes a command line before `qp` ever sees it, so an apostrophe
(`qp what's this`), a `;`, `&`, `(`, `$` or a glob in a question is the
shell's to interpret. The only fix is on the shell's side: a readline hook on
Enter that, when the line is a `qp` call whose prompt contains such
characters, single-quotes the prompt and hands it over as `qp --raw '…'`.
`_split_raw` is the receiving end."""

import argparse

# Enter (\C-m) becomes a macro of two private key sequences: the rewrite hook,
# then accept-line. (Not \C-j for the latter: people rebind it in ~/.inputrc.)
_BASH = r"""# quipcli raw-line integration — eval "$(qp --shell-init bash)"
# Typing `qp what's up; any idea?` + Enter runs  qp --raw 'what'\''s up; any idea?'
# Everything after ` | `, ` && `, ` > ` or ` >> ` stays ordinary shell
# (QP_RAW_PIPES=0 turns that off: the whole rest of the line is the prompt).
_qp_raw_rewrite() {
  local line=$READLINE_LINE lead='' rest body tail='' sep cut
  if [[ $line =~ ^[[:space:]]*qp[[:space:]]+(.*)$ ]]; then
    rest=${BASH_REMATCH[1]}
  elif [[ $line == *'| qp '* ]]; then
    lead="${line%%| qp *}| "
    rest=${line#*| qp }
  else
    return 0
  fi
  [[ $rest == --raw\ * ]] && return 0
  body=$rest
  if [[ ${QP_RAW_PIPES:-1} != 0 ]]; then
    for sep in ' | ' ' && ' ' > ' ' >> '; do
      cut=${rest%%"$sep"*}
      (( ${#cut} < ${#body} )) && body=$cut
    done
    tail=${rest:${#body}}
  fi
  # Nothing the shell would interpret: leave the line (and history) as typed.
  [[ $body == *[!A-Za-z0-9\ _.,:/@%+=-]* ]] || return 0
  READLINE_LINE="${lead}qp --raw '${body//\'/\'\\\'\'}'${tail}"
  READLINE_POINT=${#READLINE_LINE}
}
if [[ $- == *i* ]]; then
  for _qp_map in emacs vi-insert; do
    bind -m "$_qp_map" -x '"\C-x\C-q\C-r": _qp_raw_rewrite'
    bind -m "$_qp_map" '"\C-x\C-q\C-a": accept-line'
    bind -m "$_qp_map" '"\C-m": "\C-x\C-q\C-r\C-x\C-q\C-a"'
  done
  unset _qp_map
fi
"""

_SCRIPTS = {"bash": _BASH}
SHELLS = tuple(_SCRIPTS)


def shell_init(shell: str) -> str:
    return _SCRIPTS[shell]


def _value_flags(parser: argparse.ArgumentParser) -> tuple[set[str], set[str]]:
    """(flags that take a value, flags whose value is optional)."""
    required: set[str] = set()
    optional: set[str] = set()
    for action in parser._actions:
        if not action.option_strings or action.nargs == 0:
            continue
        target = optional if action.nargs == "?" else required
        target.update(action.option_strings)
    return required, optional


def _read_token(line: str, start: int) -> tuple[str, int]:
    """One whitespace-delimited token from `start` — or, if it opens with a
    quote that is closed later, the quoted text. Returns (token, next index)."""
    if line[start] in "\"'":
        end = line.find(line[start], start + 1)
        if end != -1:
            return line[start + 1 : end], end + 1
    end = start
    while end < len(line) and not line[end].isspace():
        end += 1
    return line[start:end], end


def _split_raw(line: str, parser: argparse.ArgumentParser) -> list[str]:
    """argv for a raw command line: leading flags (and their values) are
    parsed as flags; from the first plain word on, everything is prompt
    text, passed after `--` so a `-word` in a question is never an option."""
    required, optional = _value_flags(parser)
    argv: list[str] = []
    pos = 0
    while True:
        while pos < len(line) and line[pos].isspace():
            pos += 1
        if pos >= len(line):
            return argv
        if line[pos] != "-":
            break
        flag, pos = _read_token(line, pos)
        if flag in ("-", "--"):
            break
        argv.append(flag)
        if flag not in required and flag not in optional:
            continue
        while pos < len(line) and line[pos].isspace():
            pos += 1
        if pos < len(line) and (flag in required or line[pos] != "-"):
            value, pos = _read_token(line, pos)
            argv.append(value)
    return [*argv, "--", *line[pos:].split()]
