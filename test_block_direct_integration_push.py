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
