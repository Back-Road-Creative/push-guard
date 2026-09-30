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

Blocks a push when **any** destination it resolves is a protected branch. The command
line is parsed as argv (quotes honoured), and every refspec of every `git push` in it is
checked:

- explicit refspecs — `git push origin main`, `git push origin feature:main`,
  `git push origin feat/a main` (the second refspec is caught), `+HEAD:main`,
  `refs/heads/x:refs/heads/main`
- a bare `HEAD` refspec, which means the current branch
- the current branch, when no refspec is given (`git push`, `git push origin`). The
  current branch name stands in for the upstream; `push.default` is not consulted
- chained commands — `git push origin feat && git push origin main`, `a; b`
- `git -C <dir> push ...` and other git global options before `push`
- option values are skipped, not mistaken for refspecs (`-o <value>`,
  `--receive-pack <value>`, `--push-option=<value>`)

It also blocks deleting a protected branch — `git push --delete origin <protected>`
(the flag applies to every refspec) and the empty-source refspec form — on the grounds
that deleting an integration branch is worse than pushing to one.

**Not interpreted** (these are allowed): `--all` and `--mirror`, wildcard refspecs
(`refs/heads/*:refs/heads/*`), tag refspecs, backtick substitution, shell aliases and
`git` wrappers, and pushes made without going through the Bash tool. Unbalanced quotes
fall back to a plain whitespace split.

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

## Self-check

`--self-check` prints a JSON receipt and exits, without reading stdin, running `git`, or
contacting any remote:

```bash
python3 block-direct-integration-push.py --self-check
```

The receipt carries the script path and SHA-256, the effective protected branches and
where they came from (`default` or `env:GIT_PROTECTED_BRANCHES`), whether the bypass
variable is set, the advisory/fail-open contract, and a `registration` block. The hook
is looked for in Claude Code `settings.json` / `settings.local.json` files in the current
directory, its parents, and `~/.claude`. `registration.status` is one of:

| status | meaning | exit |
|---|---|---|
| `active` | registered as a `PreToolUse` hook matching `Bash`, and the script exists | 0 |
| `registered-script-missing` | registered for `Bash` but the script path does not exist | 1 |
| `registered-not-for-bash` | registered, but its matcher does not cover `Bash` | 1 |
| `not-registered` | settings files found, none registers this script | 1 |
| `no-settings` | no readable settings file (other wrappers are not inspected) | 1 |

Registration is detected by the script's file name, so a renamed copy reads as
`not-registered`. `remote_mutation` is always `false`.

## Tests

```bash
python3 -m pytest test_block_direct_integration_push.py
```

No dependencies beyond the standard library and pytest.

## Licence

MIT — see [LICENSE](LICENSE).
