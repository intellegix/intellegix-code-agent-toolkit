#!/usr/bin/env python3
"""Adversarial tests for hooks/bus-send-guard.py.

Two properties matter equally:
  1. Every shape of the 2026-08-09 injection is DENIED.
  2. The idioms ~21 live sessions already use are still ALLOWED (a guard that
     blocks legitimate sends would break the fleet, which is its own outage).

Run:  python hooks/tests/test_bus_send_guard.py
"""
import json
import os
import subprocess
import sys

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bus-send-guard.py")
BUS = "node ~/.claude/claude-bus/bus.mjs send --to all "


def run(cmd: str):
    """Invoke the hook exactly as Claude Code does; return (decision, reason)."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    p = subprocess.run([sys.executable, HOOK], input=payload,
                       capture_output=True, text=True, timeout=30)
    if not p.stdout.strip():
        return ("allow", "")  # silence == no opinion == allowed
    out = json.loads(p.stdout)["hookSpecificOutput"]
    return (out["permissionDecision"], out["permissionDecisionReason"])


# (label, command, expected decision)
CASES = [
    # ---- the real incident, both halves -------------------------------------
    ("incident: backtick in --msg",
     BUS + '--msg "everything was committed and `git restore .` brought it back"', "deny"),
    ("incident: backtick in --subject",
     BUS + '--subject "worktree - `git worktree remove --force` followed the link" --msg \'x\'',
     "deny"),

    # ---- the full metacharacter set ----------------------------------------
    ("$( ) command substitution",
     BUS + '--msg "run $(touch /tmp/pwned) now"', "deny"),
    ("${ } parameter expansion",
     BUS + '--msg "path is ${HOME}/x"', "deny"),
    ("$VAR expansion",
     BUS + '--msg "home is $HOME"', "deny"),
    ("$? special variable",
     BUS + '--msg "exit was $?"', "deny"),
    ("unquoted value with backtick",
     BUS + "--msg hello`id`world", "deny"),
    ("--body alias is covered",
     BUS + '--body "oops `id`"', "deny"),
    ("newline + semicolon inside double quotes",
     BUS + '--msg "line one\nrm -rf /tmp/x; echo $USER"', "deny"),
    ("unquoted heredoc delimiter still expands",
     BUS + '--msg "$(cat <<EOF\nvalue is `id`\nEOF\n)"', "deny"),

    # ---- idioms that MUST keep working -------------------------------------
    ("single-quoted literal with backticks",
     BUS + "--msg 'this `git restore .` is inert'", "allow"),
    ("quoted heredoc capture (the fleet's current safe idiom)",
     BUS + '--msg "$(cat <<\'EOF\'\nbody with `backticks` and $(subst) and ${braces}\nEOF\n)"',
     "allow"),
    ("plain file read",
     BUS + '--msg "$(cat body.txt)"', "allow"),
    ("--file route",
     BUS + "--subject 'plain subject' --file body.txt", "allow"),
    ("--stdin route with quoted heredoc",
     BUS + "--subject 'plain subject' --stdin <<'EOF'\nbody `id` $(id)\nEOF", "allow"),
    ("ordinary prose, no metacharacters",
     BUS + '--subject "Build green" --msg "All 178 tests passed on branch fix/x"', "allow"),
    ("escaped dollar is inert",
     BUS + '--msg "costs \\$5 today"', "allow"),

    # ---- must not touch unrelated commands ----------------------------------
    ("non-send bus command untouched",
     "node ~/.claude/claude-bus/bus.mjs inbox --peek", "allow"),
    ("unrelated command with backticks untouched",
     'echo "`date`"', "allow"),
    ("word 'send' inside another program is not a bus send",
     'python resend.py --msg "$HOME"', "allow"),
]


def main() -> int:
    failures = []
    for label, cmd, expected in CASES:
        try:
            decision, reason = run(cmd)
        except Exception as e:  # noqa: BLE001
            failures.append((label, expected, f"EXCEPTION {e!r}"))
            continue
        ok = (decision == expected)
        print(f"{'PASS' if ok else 'FAIL'}  [{expected:5}] {label}")
        if not ok:
            failures.append((label, expected, f"got {decision}: {reason[:120]}"))

    print(f"\n{len(CASES) - len(failures)}/{len(CASES)} passed")
    for label, expected, got in failures:
        print(f"  FAILED {label}: expected {expected}, {got}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
