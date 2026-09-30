"""Behavioural tests for the push guard.

Run: python3 -m pytest test_block_direct_integration_push.py
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parent / "block-direct-integration-push.py"

spec = importlib.util.spec_from_file_location("push_guard", HOOK)
assert spec and spec.loader
push_guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(push_guard)


def run_hook(command: str, env: dict[str, str] | None = None) -> int:
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "."}
    )
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        text=True,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
    )
    return proc.returncode


def test_explicit_push_to_master_is_blocked() -> None:
    assert run_hook("git push origin HEAD:master") == 2


def test_explicit_push_to_main_is_blocked() -> None:
    assert run_hook("git push origin main") == 2


def test_feature_branch_is_allowed() -> None:
    assert run_hook("git push origin feat/some-change") == 0


def test_delete_of_protected_branch_is_blocked() -> None:
    assert run_hook("git push --delete origin main") == 2


def test_inline_bypass_is_honoured() -> None:
    assert run_hook("ALLOW_INTEGRATION_PUSH=1 git push origin main") == 0


def test_non_push_command_is_ignored() -> None:
    assert run_hook("git status") == 0


def test_gh_push_lookalike_is_ignored() -> None:
    assert run_hook("gh pr create --base main") == 0


def test_branch_list_is_configurable() -> None:
    env = {"GIT_PROTECTED_BRANCHES": "release,production"}
    assert run_hook("git push origin release", env=env) == 2
    # A default-protected name is no longer protected once overridden.
    assert run_hook("git push origin main", env=env) == 0


def test_parse_push_target_reads_colon_refspec() -> None:
    target, is_delete = push_guard.parse_push_target(
        "git push origin feature:master", "feature"
    )
    assert target == "master"
    assert is_delete is False


def test_malformed_payload_fails_open() -> None:
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input="not json", text=True, capture_output=True
    )
    assert proc.returncode == 0


# --- PG-1: argv-aware parsing, every refspec checked -----------------------


def test_second_refspec_to_protected_branch_is_blocked() -> None:
    assert run_hook("git push origin feat/x main") == 2


def test_later_refspec_colon_form_is_blocked() -> None:
    assert run_hook("git push origin feat/x feat/y:master") == 2


def test_multiple_feature_refspecs_are_allowed() -> None:
    assert run_hook("git push origin feat/a feat/b:feat/c") == 0


def test_plus_prefixed_refspec_is_read() -> None:
    assert run_hook("git push origin +HEAD:main") == 2


def test_delete_colon_form_is_blocked_as_delete() -> None:
    assert push_guard.parse_push_targets("git push origin :main", "feat") == [
        ("main", True)
    ]


def test_delete_flag_applies_to_every_refspec() -> None:
    assert push_guard.parse_push_targets(
        "git push --delete origin old main", "feat"
    ) == [("old", True), ("main", True)]


def test_option_values_are_not_mistaken_for_remote_or_refspec() -> None:
    cmd = "git push -o ci.skip --receive-pack x origin feat/x main"
    assert push_guard.parse_push_targets(cmd, "feat") == [
        ("feat/x", False),
        ("main", False),
    ]


def test_attached_option_values_are_skipped() -> None:
    cmd = "git push --push-option=main:abc123 origin feat/x"
    assert push_guard.parse_push_targets(cmd, "feat") == [("feat/x", False)]


def test_quoted_refspec_is_unquoted() -> None:
    assert run_hook("git push origin 'HEAD:main'") == 2
    assert run_hook('git push origin "main"') == 2


def test_quoted_operator_inside_argument_is_not_a_second_command() -> None:
    assert run_hook("git push origin 'feat/a && main'") == 0


def test_second_command_on_the_line_is_checked() -> None:
    assert run_hook("git push origin feat/x && git push origin main") == 2
    assert run_hook("git push origin feat/x; git push origin master") == 2


def test_git_global_options_before_push_are_skipped() -> None:
    assert run_hook("git -C /tmp/repo push origin main") == 2
    assert run_hook("git -c user.name=x push origin feat/x") == 0


def test_env_prefix_before_git_is_handled() -> None:
    assert run_hook("GIT_TRACE=1 git push origin main") == 2


def test_bare_head_refspec_means_current_branch() -> None:
    assert push_guard.parse_push_targets("git push origin HEAD", "main") == [
        ("main", False)
    ]
    assert push_guard.parse_push_targets("git push origin HEAD", "feat") == [
        ("feat", False)
    ]


def test_implicit_destination_uses_current_branch() -> None:
    assert push_guard.parse_push_targets("git push", "main") == [("main", False)]
    assert push_guard.parse_push_targets("git push origin", "main") == [
        ("main", False)
    ]
    assert push_guard.parse_push_targets("git push -u origin", "feat") == [
        ("feat", False)
    ]


def test_refs_heads_prefix_is_stripped_on_every_refspec() -> None:
    assert push_guard.parse_push_targets(
        "git push origin a refs/heads/b:refs/heads/main", "feat"
    ) == [("a", False), ("main", False)]


def test_tag_refspec_named_like_a_branch_is_not_a_branch_push() -> None:
    assert run_hook("git push origin refs/tags/main") == 0


def test_unbalanced_quote_still_checks_with_whitespace_split() -> None:
    assert run_hook("git push origin main 'oops") == 2


def test_parse_push_target_returns_protected_target_when_any() -> None:
    target, is_delete = push_guard.parse_push_target(
        "git push origin feat main", "feat"
    )
    assert target == "main"
    assert is_delete is False


def test_non_push_git_command_yields_no_targets() -> None:
    assert push_guard.parse_push_targets("git status main", "main") == []
    assert push_guard.parse_push_targets("gh pr create --base main", "main") == []


# --- PG-2: --self-check, read-only registration receipt --------------------


def run_self_check(
    cwd: Path, home: Path, env: dict[str, str] | None = None
) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(HOOK), "--self-check"],
        text=True,
        capture_output=True,
        cwd=str(cwd),
        env={"PATH": "/usr/bin:/bin", "HOME": str(home), **(env or {})},
        stdin=subprocess.DEVNULL,
    )
    return proc.returncode, json.loads(proc.stdout)


def write_settings(path: Path, hooks: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}))


def make_dirs(tmp_path: Path) -> tuple[Path, Path]:
    proj, home = tmp_path / "proj", tmp_path / "home"
    proj.mkdir()
    home.mkdir()
    return proj, home


def bash_entry(command: str, matcher: str = "Bash") -> dict:
    return {
        "PreToolUse": [
            {"matcher": matcher, "hooks": [{"type": "command", "command": command}]}
        ]
    }


def test_self_check_without_any_settings_reports_no_settings(tmp_path: Path) -> None:
    proj, home = make_dirs(tmp_path)
    rc, receipt = run_self_check(proj, home)
    assert rc == 1
    assert receipt["registration"]["status"] == "no-settings"
    assert receipt["remote_mutation"] is False


def test_self_check_settings_without_the_hook_is_not_registered(
    tmp_path: Path,
) -> None:
    proj, home = make_dirs(tmp_path)
    write_settings(proj / ".claude" / "settings.json", bash_entry("/x/other-hook.py"))
    rc, receipt = run_self_check(proj, home)
    assert rc == 1
    assert receipt["registration"]["status"] == "not-registered"
    assert receipt["registration"]["entries"] == []


def test_self_check_registered_for_bash_is_active(tmp_path: Path) -> None:
    proj, home = make_dirs(tmp_path)
    write_settings(home / ".claude" / "settings.json", bash_entry(f"python3 {HOOK}"))
    rc, receipt = run_self_check(proj, home)
    assert rc == 0
    reg = receipt["registration"]
    assert reg["status"] == "active"
    assert reg["entries"][0]["matcher"] == "Bash"
    assert reg["entries"][0]["script_exists"] is True


def test_self_check_registered_for_another_tool_is_not_active(
    tmp_path: Path,
) -> None:
    proj, home = make_dirs(tmp_path)
    write_settings(
        proj / ".claude" / "settings.local.json", bash_entry(str(HOOK), matcher="Edit")
    )
    rc, receipt = run_self_check(proj, home)
    assert rc == 1
    assert receipt["registration"]["status"] == "registered-not-for-bash"


def test_self_check_registered_with_missing_script_is_not_active(
    tmp_path: Path,
) -> None:
    proj, home = make_dirs(tmp_path)
    write_settings(
        proj / ".claude" / "settings.json",
        bash_entry("/nonexistent/block-direct-integration-push.py"),
    )
    rc, receipt = run_self_check(proj, home)
    assert rc == 1
    assert receipt["registration"]["status"] == "registered-script-missing"


def test_self_check_reports_protected_branches_and_contract(tmp_path: Path) -> None:
    proj, home = make_dirs(tmp_path)
    _, receipt = run_self_check(proj, home)
    assert receipt["protected_branches"] == ["develop", "main", "master", "trunk"]
    assert receipt["protected_branches_source"] == "default"
    assert "fails open" in receipt["contract"]
    assert len(receipt["script_sha256"]) == 64
    _, receipt = run_self_check(
        proj, home, env={"GIT_PROTECTED_BRANCHES": "release, prod"}
    )
    assert receipt["protected_branches"] == ["prod", "release"]
    assert receipt["protected_branches_source"] == "env:GIT_PROTECTED_BRANCHES"


def test_self_check_unreadable_settings_does_not_crash(tmp_path: Path) -> None:
    proj, home = make_dirs(tmp_path)
    (proj / ".claude").mkdir()
    (proj / ".claude" / "settings.json").write_text("{not json")
    rc, receipt = run_self_check(proj, home)
    assert rc == 1
    assert receipt["registration"]["status"] == "no-settings"
    assert receipt["registration"]["settings_checked"][0]["error"]


def test_self_check_needs_no_stdin_and_no_git(tmp_path: Path) -> None:
    proj, home = make_dirs(tmp_path)
    _, receipt = run_self_check(proj, home, env={"PATH": "/nonexistent"})
    assert receipt["receipt"] == "push-guard-self-check/1"
