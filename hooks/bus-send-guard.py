#!/usr/bin/env python3
"""PreToolUse guard: stop a claude-bus message body from being executed by the shell.

WHY THIS EXISTS
---------------
On 2026-08-09 10:23:25 PT the orchestrator ran (abridged):

    node ~/.claude/claude-bus/bus.mjs send --to all \
      --subject "... worktree - `git worktree remove --force` followed the link ..." \
      --msg "... everything was committed and `git restore .` brought it all back ..."

The Bash tool runs Git Bash (POSIX sh). Inside DOUBLE quotes a POSIX shell still
expands backticks, $(...), ${...} and $VAR. So the shell ran `git restore .` in
~/.claude BEFORE node ever started -- a repo-wide revert of every modified tracked
file -- and delivered the message with the backticked text silently deleted.

Nothing inside bus.mjs can prevent this: by the time node runs, the shell has
already executed the payload. A PreToolUse hook is the ONLY layer that sees the
command before the shell does. That is what this file is.

POLICY
------
For a `bus.mjs send` command, the value of --msg / --body / --subject must be
inert. Allowed:
  * a single-quoted literal:            --msg 'text'
  * a quoted-heredoc capture:           --msg "$(cat <<'EOF' ... EOF)"
  * a plain file read:                  --msg "$(cat body.txt)"
  * the safe routes:                    --file body.txt | --stdin | --json-stdin
Denied: any backtick, $(...), ${...} or $VAR that the shell would expand inside a
double-quoted or unquoted value.

Fails OPEN on an internal error (exit 0, no opinion): a bug in this guard must
never be able to block Bash across the fleet. It fails CLOSED only on a positive
detection.
"""
import json
import os
import re
import sys
from datetime import datetime

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bus-send-guard.log")

PAYLOAD_FLAGS = ("--msg", "--body", "--subject")
# Commands whose output is literal text, so "$(cat ...)" is an inert capture.
LITERAL_READERS = ("cat", "printf", "type")

REMEDY = (
    "Rewrite the send so the text never reaches the shell. Either:\n"
    "  node ~/.claude/claude-bus/bus.mjs send --to <id> --subject 'plain text' --stdin <<'EOF'\n"
    "  ...body...\n"
    "  EOF\n"
    "or write the body to a file with a quoted heredoc and pass --file <path>.\n"
    "Single quotes around a short subject are also fine. See "
    "~/.claude/claude-bus/SHELL-INJECTION-EVIDENCE-2026-08-09.md"
)


def log(line: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass


def decide(decision: str, reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def split_words(cmd: str):
    """Split a shell command into words, keeping each word's RAW source text.

    Tracks POSIX quoting so we can later ask "would the shell expand this?".
    A $( ) or ` ` region is consumed whole -- whitespace inside it does not split a
    word, which is what keeps `--msg "$(cat <<'EOF' ... EOF)"` a single value.
    """
    words, buf = [], []
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c in " \t\n":
            if buf:
                words.append("".join(buf))
                buf = []
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            buf.append(cmd[i:i + 2])
            i += 2
            continue
        if c == "'":
            j = cmd.find("'", i + 1)
            j = n if j == -1 else j + 1
            buf.append(cmd[i:j])
            i = j
            continue
        if c == '"':
            j, i2 = i + 1, i
            while j < n:
                if cmd[j] == "\\":
                    j += 2
                    continue
                if cmd[j] == '"':
                    j += 1
                    break
                j += 1
            buf.append(cmd[i2:j])
            i = j
            continue
        if c == "$" and i + 1 < n and cmd[i + 1] == "(":
            j, depth = i + 2, 1
            while j < n and depth:
                if cmd[j] == "(":
                    depth += 1
                elif cmd[j] == ")":
                    depth -= 1
                j += 1
            buf.append(cmd[i:j])
            i = j
            continue
        if c == "`":
            j = cmd.find("`", i + 1)
            j = n if j == -1 else j + 1
            buf.append(cmd[i:j])
            i = j
            continue
        buf.append(c)
        i += 1
    if buf:
        words.append("".join(buf))
    return words


def _expansions(text: str):
    """Shell expansions the shell WOULD perform in `text` (single-quoted spans skipped)."""
    found, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "'":  # literal span - nothing expands inside
            j = text.find("'", i + 1)
            i = n if j == -1 else j + 1
            continue
        if c == "\\" and i + 1 < n:  # escaped -> inert
            i += 2
            continue
        if c == "`":
            found.append("backtick command substitution (`...`)")
            i += 1
            continue
        if c == "$" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "(":
                found.append("command substitution $(...)")
            elif nxt == "{":
                found.append("parameter expansion ${...}")
            elif nxt.isalpha() or nxt == "_":
                m = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*", text[i:])
                found.append(f"variable expansion {m.group(0) if m else '$VAR'}")
            elif nxt in "@*#?!0123456789":
                found.append(f"variable expansion ${nxt}")
            i += 1
            continue
        i += 1
    return found


def classify(raw: str):
    """Return None if the value is inert, else a human reason it is dangerous."""
    v = raw.strip()
    if not v:
        return None

    # Fully single-quoted literal -> the shell expands nothing.
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'" and "'" not in v[1:-1]:
        return None

    # Peel one layer of surrounding double quotes for the checks below.
    inner = v[1:-1] if len(v) >= 2 and v[0] == '"' and v[-1] == '"' else v

    # Inert capture: "$(cat <<'EOF' ... EOF)" / "$(cat file)" / "$(printf ...)".
    m = re.match(r"^\$\(\s*(\w+)", inner)
    if m and inner.endswith(")") and m.group(1) in LITERAL_READERS:
        # Only inert if EVERY heredoc delimiter is quoted; <<EOF expands inside.
        for hd in re.finditer(r"<<-?\s*(\S)", inner):
            if hd.group(1) not in ("'", '"'):
                return ("heredoc delimiter is not quoted (<<EOF), so the shell expands "
                        "backticks and $(...) inside the body - use <<'EOF'")
        return None

    hits = _expansions(inner)
    if hits:
        uniq = list(dict.fromkeys(hits))
        return "the shell would execute/expand " + "; ".join(uniq[:4])
    return None


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (ValueError, TypeError):
        sys.exit(0)

    if data.get("tool_name", "") != "Bash":
        sys.exit(0)

    cmd = (data.get("tool_input", {}) or {}).get("command", "") or ""
    if "bus.mjs" not in cmd or not re.search(r"(?<![-\w])send(?![-\w])", cmd):
        sys.exit(0)

    try:
        words = split_words(cmd)
        for idx, w in enumerate(words):
            if w not in PAYLOAD_FLAGS:
                continue
            value = words[idx + 1] if idx + 1 < len(words) else ""
            reason = classify(value)
            if reason:
                log(f"DENY {w}: {reason} :: {cmd[:300]!r}")
                decide("deny",
                       f"BLOCKED: unsafe claude-bus send. The value of {w} is not inert - "
                       f"{reason}. This is the exact bug that ran a repo-wide 'git restore' "
                       f"in ~/.claude on 2026-08-09.\n\n{REMEDY}")
        log(f"ALLOW :: {cmd[:200]!r}")
    except Exception as e:  # never brick Bash on a guard bug
        log(f"ERROR (failing open) {e!r}")
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
