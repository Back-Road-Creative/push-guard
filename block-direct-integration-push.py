#!/usr/bin/env python3
"""PreToolUse:Bash hook — block direct git push to protected integration branches.

Refuses `git push` commands whose target ref is an integration branch, so
that changes always flow through a pull request. Server-side branch
protection is often deliberately permissive on integration branches — they
must accept merges from automation without admin-only push restrictions —
which leaves a gap this client-side guard fills.

The failure it exists to stop: an agent (or a person) finishes a PR, the
working copy is left on the integration branch, and the next `git push` of
a follow-up commit lands directly on it. Nothing is destroyed; review is
simply skipped, silently.

Configure the branch list with PROTECTED_BRANCHES below, or the
GIT_PROTECTED_BRANCHES environment variable (comma-separated).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

DEFAULT_PROTECTED_BRANCHES = {"main", "master", "develop", "trunk"}
BYPASS_ENV = "ALLOW_INTEGRATION_PUSH"
BRANCHES_ENV = "GIT_PROTECTED_BRANCHES"


def protected_branches() -> set[str]:
    """Branch names this hook refuses to push to."""
    configured = os.environ.get(BRANCHES_ENV, "").strip()
    if configured:
        return {b.strip() for b in configured.split(",") if b.strip()}
    return set(DEFAULT_PROTECTED_BRANCHES)


def get_current_branch(cwd: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return (r.stdout.strip() or None)
    except Exception:
        pass
    return None


def parse_push_target(cmd: str, current: str | None) -> tuple[str | None, bool]:
    """Return (target_branch, is_delete).

    Handles the common shapes:
      git push                          -> upstream of current branch
      git push origin                   -> upstream of current branch
      git push origin <ref>             -> <ref>
      git push origin <src>:<dst>       -> <dst>
      git push --delete origin <ref>    -> <ref>, is_delete=True

    Returns target_branch=None when the parse is ambiguous; the caller
    treats None as "allow" so we never block on a command we can't read.
    """
    tokens = re.split(r"\s+", cmd.strip())
    is_delete = "--delete" in tokens or "-d" in tokens
    try:
        push_idx = tokens.index("push")
    except ValueError:
        return None, is_delete
    args = [t for t in tokens[push_idx + 1:] if not t.startswith("-")]
    if len(args) >= 2:
        spec = args[1]
        dst = spec.split(":", 1)[1] if ":" in spec else spec
        return dst.replace("refs/heads/", "") or None, is_delete
    return current, is_delete


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # Malformed payload — not our problem; let it through.

    if payload.get("tool_name") != "Bash":
        return 0
    cmd = payload.get("tool_input", {}).get("command", "")
    if not cmd:
        return 0

    # Inline opt-in: ALLOW_INTEGRATION_PUSH=1 git push ...
    if f"{BYPASS_ENV}=1" in cmd:
        return 0
    if os.environ.get(BYPASS_ENV) == "1":
        return 0

    # Match the literal `git push` token sequence (not `gh push`,
    # not arbitrary substrings).
    if not re.search(r"\bgit\s+push\b", cmd):
        return 0

    cwd = payload.get("cwd") or os.getcwd()
    current = get_current_branch(cwd)
    target, is_delete = parse_push_target(cmd, current)

    if target and target in protected_branches():
        verb = "delete" if is_delete else "push to"
        sys.stderr.write(
            f"\n[block-direct-integration-push] Refusing to {verb} protected "
            f"integration branch '{target}'.\n\n"
            f"Workflow: branch from origin/{target}, push the feature branch, "
            f"open a PR with `gh pr create --base {target}`.\n\n"
            f"If this push is genuinely intentional (rare — e.g. recovering "
            f"from a botched merge), bypass by prefixing the command:\n"
            f"  {BYPASS_ENV}=1 git push ...\n\n"
            f"Current branch: {current}\n"
            f"Target branch:  {target}\n"
            f"Command:        {cmd}\n"
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
