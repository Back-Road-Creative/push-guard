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

Configure the branch list with DEFAULT_PROTECTED_BRANCHES below, or the
GIT_PROTECTED_BRANCHES environment variable (comma-separated).

Run with `--self-check` (no stdin, no git, no network, no push) to print a
JSON receipt of the effective protected branches and whether this script is
registered as a PreToolUse hook for the Bash tool. Exit 0 = active,
1 = not active. Hook mode itself is advisory and fails open.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

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


# `git push` options that consume the NEXT argv entry as their value.
# (Values attached with `=` are part of the option token and need no entry.)
_PUSH_VALUE_OPTS = {"--repo", "--receive-pack", "--exec", "--push-option"}
# `git` global options that consume the next argv entry (before the subcommand).
_GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
_SHELL_PUNCT = ";&|()"
_REDIRECT = re.compile(r"^\d*(>>?|<)&?$")


def _tokenize(cmd: str) -> list[str]:
    """Split a shell command line into argv-ish tokens.

    Quotes are honoured; `;`, `&&`, `||`, `|`, `&` and parentheses become
    their own tokens. On an unbalanced quote, fall back to a whitespace split
    so that a malformed line is still checked rather than waved through.
    """
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=_SHELL_PUNCT)
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:
        return cmd.split()


def _push_segments(tokens: list[str]) -> list[list[str]]:
    """Return the argument list after `push` for every `git push` in tokens."""
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok and all(c in _SHELL_PUNCT for c in tok):
            segments.append([])
        else:
            segments[-1].append(tok)
    out: list[list[str]] = []
    for seg in segments:
        git_idx = next(
            (i for i, t in enumerate(seg) if t == "git" or t.endswith("/git")), None
        )
        if git_idx is None:
            continue
        i = git_idx + 1
        while i < len(seg):
            tok = seg[i]
            if tok == "push":
                out.append(seg[i + 1:])
                break
            if not tok.startswith("-"):
                break  # some other git subcommand
            i += 2 if tok in _GIT_VALUE_OPTS else 1
    return out


def _parse_push_args(args: list[str]) -> tuple[list[str], bool]:
    """Split push args into (positionals, delete_flag)."""
    positional: list[str] = []
    is_delete = False
    i = 0
    while i < len(args):
        tok = args[i]
        i += 1
        if tok == "--":
            positional.extend(args[i:])
            break
        if _REDIRECT.match(tok):
            i += 1  # bare redirection operator: its target is not a refspec
            continue
        if re.match(r"^\d*[<>]", tok):
            continue  # redirection with an attached target, e.g. 2>/dev/null
        if tok.startswith("--"):
            if tok == "--delete":
                is_delete = True
            elif tok in _PUSH_VALUE_OPTS:
                i += 1
        elif tok.startswith("-") and len(tok) > 1:
            for pos, ch in enumerate(tok[1:], start=1):
                if ch == "d":
                    is_delete = True
                elif ch == "o":  # -o <value>, or -o<value> attached
                    if pos == len(tok) - 1:
                        i += 1
                    break
        else:
            positional.append(tok)
    return positional, is_delete


def _refspec_destination(
    spec: str, current: str | None
) -> tuple[str | None, bool]:
    """Return (destination branch or None, is_deletion_refspec)."""
    spec = spec.lstrip("+")
    if ":" in spec:
        src, dst = spec.split(":", 1)
        deletion = src == ""
    else:
        dst, deletion = spec, False
        if dst == "HEAD":
            dst = current or ""
    if dst.startswith("refs/heads/"):
        dst = dst[len("refs/heads/"):]
    return (dst or None), deletion


def parse_push_targets(cmd: str, current: str | None) -> list[tuple[str, bool]]:
    """Return [(destination_branch, is_delete), ...] for every `git push`.

    Every refspec of every `git push` on the command line is reported.
    Handles:
      git push / git push origin        -> current branch
      git push origin a b:c             -> a, c
      git push origin HEAD              -> current branch
      git push origin :b                -> b (deletion refspec)
      git push --delete origin a b      -> a, b (delete)
      option values (-o x, --repo x), quoting, `&&` / `;` chains,
      `git -C dir push ...`

    Not interpreted (treated as allowed): --all / --mirror, wildcard
    refspecs, `push.default` / upstream name mapping (the current branch name
    stands in for the upstream), backtick substitution.
    An empty list means "nothing to check"; callers allow it.
    """
    targets: list[tuple[str, bool]] = []
    for args in _push_segments(_tokenize(cmd)):
        positional, delete_flag = _parse_push_args(args)
        refspecs = positional[1:]  # positional[0] is the remote
        if not refspecs:
            if current:
                targets.append((current, delete_flag))
            continue
        skip_next = False
        for spec in refspecs:
            if skip_next:
                skip_next = False
                continue
            if spec == "tag":  # `git push origin tag <name>`
                skip_next = True
                continue
            dst, deletion = _refspec_destination(spec, current)
            if dst:
                targets.append((dst, delete_flag or deletion))
    return targets


def parse_push_target(cmd: str, current: str | None) -> tuple[str | None, bool]:
    """Return (target_branch, is_delete) for the most relevant destination.

    Kept for callers of the original single-target API: returns the first
    protected destination if there is one, else the first destination. Use
    parse_push_targets to see every destination.

    Returns target_branch=None when nothing could be parsed; the caller
    treats None as "allow" so we never block on a command we can't read.
    """
    targets = parse_push_targets(cmd, current)
    if not targets:
        return None, False
    protected = protected_branches()
    for dst, is_delete in targets:
        if dst in protected:
            return dst, is_delete
    return targets[0]


HOOK_NAME = "block-direct-integration-push"
CONTRACT = (
    "advisory client-side guard; fails open on anything it cannot parse; "
    "not a security control and not a substitute for server-side protection"
)


def _settings_files(cwd: Path) -> list[Path]:
    """Claude Code settings files that could register the hook, deduplicated."""
    files = [d / ".claude" / n for d in [cwd, *cwd.parents]
             for n in ("settings.json", "settings.local.json")]
    home = Path(os.path.expanduser("~"))
    files += [home / ".claude" / n for n in ("settings.json", "settings.local.json")]
    seen: set[Path] = set()
    out = []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _covers_bash(matcher: object) -> bool:
    if not matcher or matcher == "*":
        return True
    try:
        return re.fullmatch(str(matcher), "Bash") is not None
    except re.error:
        return False


def _script_exists(command: str, project_dir: Path) -> bool:
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    for w in words:
        if HOOK_NAME in w:
            for var in ("CLAUDE_PROJECT_DIR",):
                w = w.replace("${%s}" % var, str(project_dir))
                w = w.replace("$" + var, str(project_dir))
            return Path(os.path.expandvars(os.path.expanduser(w))).exists()
    return False


def self_check() -> int:
    """Print a JSON receipt of the effective configuration. Read-only.

    Reads this script and Claude Code settings files only. It runs no git
    command, reads no stdin and touches no remote. Exit 0 when the hook is
    registered for the Bash tool and its script exists, 1 otherwise.
    """
    cwd = Path.cwd()
    checked: list[dict] = []
    entries: list[dict] = []
    readable = 0
    for f in _settings_files(cwd):
        if not f.is_file():
            continue
        rec: dict = {"path": str(f), "exists": True}
        checked.append(rec)
        try:
            data = json.loads(f.read_text())
            groups = data.get("hooks", {}).get("PreToolUse", [])
            readable += 1
        except Exception as exc:  # unreadable or wrong shape: report, go on
            rec["error"] = f"{type(exc).__name__}: {exc}"
            continue
        project_dir = f.parent.parent if f.parent.name == ".claude" else f.parent
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            for h in group.get("hooks") or []:
                cmd = h.get("command", "") if isinstance(h, dict) else ""
                if HOOK_NAME in cmd:
                    entries.append({
                        "settings": str(f),
                        "matcher": group.get("matcher", ""),
                        "command": cmd,
                        "covers_bash": _covers_bash(group.get("matcher")),
                        "script_exists": _script_exists(cmd, project_dir),
                    })
    if readable == 0:
        status = "no-settings"
    elif any(e["covers_bash"] and e["script_exists"] for e in entries):
        status = "active"
    elif any(e["covers_bash"] for e in entries):
        status = "registered-script-missing"
    elif entries:
        status = "registered-not-for-bash"
    else:
        status = "not-registered"

    configured = os.environ.get(BRANCHES_ENV, "").strip()
    script = Path(__file__).resolve()
    receipt = {
        "receipt": "push-guard-self-check/1",
        "script": str(script),
        "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "python": sys.version.split()[0],
        "protected_branches": sorted(protected_branches()),
        "protected_branches_source": (
            f"env:{BRANCHES_ENV}" if configured else "default"
        ),
        "bypass_env": {"name": BYPASS_ENV,
                       "set_in_environment": os.environ.get(BYPASS_ENV) == "1"},
        "contract": CONTRACT,
        "registration": {
            "status": status,
            "entries": entries,
            "settings_checked": checked,
        },
        "remote_mutation": False,
    }
    json.dump(receipt, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if status == "active" else 1


def main() -> int:
    if "--self-check" in sys.argv[1:]:
        return self_check()
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

    # Cheap prefilter (not `gh push`, not arbitrary substrings); the real
    # work is argv-aware parsing, which also handles `git -C dir push`.
    if not re.search(r"\bgit\b.*\bpush\b", cmd, re.DOTALL):
        return 0

    cwd = payload.get("cwd") or os.getcwd()
    current = get_current_branch(cwd)
    # Prefers a protected destination, so a protected ref anywhere in a
    # multi-refspec or chained command is found.
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
