# push-guard

A client-side hook that refuses `git push` to protected integration branches, so
changes always arrive through a pull request.

Server-side branch protection usually cannot close this gap on its own. Integration
branches must accept merges from automation, which means push restrictions are often
left permissive; and protection rules do not apply at all to a repository you have
admin rights on unless you explicitly opt in. This hook sits in front of the command.

The failure it exists to stop is mundane: a pull request finishes, the working copy is
left sitting on the integration branch, and the next `git push` of a follow-up commit
lands directly on it. Nothing is destroyed. Review is simply skipped, and nobody
notices until later.

## Behaviour

Blocks a push when the resolved target is a protected branch. The target is read from:

- an explicit refspec — `git push origin main`, `git push origin feature:main`
- the upstream of the current branch, when no refspec is given

It also blocks `git push --delete origin <protected>`, on the grounds that deleting an
integration branch is worse than pushing to one.

It allows everything else: feature branches, ad-hoc branches, and any command it cannot
confidently parse. **The hook fails open by design** — a parser bug should never stand
between you and your work. It is a guard rail, not a security control; anyone who wants
to get past it can, and that is the intent.

## Configuration

Protected branches default to `main`, `master`, `develop`, `trunk`. Override with a
comma-separated environment variable:

```bash
export GIT_PROTECTED_BRANCHES="main,release,production"
```

Setting the variable **replaces** the defaults rather than adding to them.

## Bypass

For the rare legitimate case — recovering from a botched merge, say:

```bash
ALLOW_INTEGRATION_PUSH=1 git push origin main
```

## Install

The hook reads a JSON payload on stdin and exits `2` to block, `0` to allow. That is the
contract used by Claude Code's `PreToolUse` hooks, and it is simple enough to adapt to
any wrapper that can run a command and read an exit code.

```bash
cp block-direct-integration-push.py ~/.claude/hooks/
chmod +x ~/.claude/hooks/block-direct-integration-push.py
```

Then register it as a `PreToolUse` hook matching the `Bash` tool. The payload it expects:

```json
{
  "tool_name": "Bash",
  "tool_input": { "command": "git push origin main" },
  "cwd": "/path/to/repo"
}
```

Exit codes: `0` allow, `2` block (the message is written to stderr).

## Tests

```bash
python3 -m pytest test_block_direct_integration_push.py
```

No dependencies beyond the standard library and pytest.

## Licence

MIT — see [LICENSE](LICENSE).
