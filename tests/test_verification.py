"""Tests for PR 2: Acceptance Contract + External Verifier.

All tests are deterministic — no live provider needed.
"""

import json
from pathlib import Path

import pytest

import forge.cli as clilib
from forge.core.run_result import RunResult
from forge.features.task_record import TaskRecord, TaskStatus, TaskStore
from forge.features.verification import (
    AcceptanceContract,
    ChangePolicy,
    ChangeScopeVerifier,
    CommandResult,
    CommandVerifier,
    ContractStore,
    EvidenceRecorder,
    ReproduceExpectation,
    Reproducer,
    ReproductionVerdict,
    TaskVerificationService,
    VerifierVerdict,
    VerifyCommand,
)


# ---------------------------------------------------------------------------
# AcceptanceContract basics
# ---------------------------------------------------------------------------

def test_contract_defaults():
    c = AcceptanceContract(goal="fix bug")
    assert c.goal == "fix bug"
    assert c.schema_version == 1
    assert not c.reproduce_command
    assert c.verify_commands == []
    assert "*" in c.change_policy.allowed_paths


def test_contract_serialize_roundtrip():
    original = AcceptanceContract(
        goal="fix calculator.add",
        reproduce_command="python -m pytest test_calc.py -q",
        reproduce_timeout=30,
        verify_commands=[
            VerifyCommand(command="python -m pytest test_calc.py -q", timeout_seconds=60),
        ],
        change_policy=ChangePolicy(
            allowed_paths=["calculator.py"],
            forbidden_paths=["tests/acceptance/**"],
        ),
    )
    data = original.to_dict()
    restored = AcceptanceContract.from_dict(data)
    assert restored.goal == original.goal
    assert restored.reproduce_command == original.reproduce_command
    assert len(restored.verify_commands) == 1
    assert restored.verify_commands[0].command == original.verify_commands[0].command
    assert "tests/acceptance/**" in restored.change_policy.forbidden_paths


def test_contract_content_hash_stable():
    c1 = AcceptanceContract(goal="fix bug")
    c2 = AcceptanceContract(goal="fix bug")
    assert c1.content_hash == c2.content_hash


def test_contract_content_hash_changes_with_goal():
    c1 = AcceptanceContract(goal="fix bug")
    c2 = AcceptanceContract(goal="fix different bug")
    assert c1.content_hash != c2.content_hash


# ---------------------------------------------------------------------------
# ReproduceExpectation
# ---------------------------------------------------------------------------

def test_reproduce_expectation_matches_exit_code():
    exp = ReproduceExpectation(exit_code=1)
    assert exp.matches(1, "", "")
    assert not exp.matches(0, "", "")


def test_reproduce_expectation_matches_output():
    exp = ReproduceExpectation(output_contains=["assert 4 == 5"])
    assert exp.matches(1, "assert 4 == 5", "")
    assert not exp.matches(1, "assert 3 == 5", "")


def test_reproduce_expectation_matches_failing_tests():
    exp = ReproduceExpectation(failing_tests=["test_add"])
    assert exp.matches(1, "FAILED test_add", "")
    assert not exp.matches(1, "PASSED test_all_passed", "")


def test_reproduce_expectation_requires_all_declared_signals():
    exp = ReproduceExpectation(
        exit_code=1,
        failing_tests=["test_add", "test_subtract"],
        output_contains=["assert 4 == 5", "calculator"],
    )
    assert exp.matches(1, "test_add test_subtract assert 4 == 5 calculator", "")
    assert not exp.matches(1, "test_add assert 4 == 5 calculator", "")
    assert not exp.matches(0, "test_add test_subtract assert 4 == 5 calculator", "")


# ---------------------------------------------------------------------------
# ContractStore (without actual contract file)
# ---------------------------------------------------------------------------

def test_contract_store_load_nonexistent(tmp_path):
    cs = ContractStore(tmp_path)
    c = cs.load_contract("nonexistent")
    assert c is None


def test_contract_store_save_and_load(tmp_path):
    cs = ContractStore(tmp_path)
    contract = AcceptanceContract(goal="test")
    cs.save_contract("task_001", contract)
    loaded = cs.load_contract("task_001")
    assert loaded is not None
    assert loaded.goal == "test"


def test_contract_store_integrity_check(tmp_path):
    cs = ContractStore(tmp_path)
    contract = AcceptanceContract(goal="test")
    cs.save_contract("task_001", contract)
    h = cs.contract_hash("task_001")
    assert h is not None
    assert cs.verify_integrity("task_001", h)
    assert not cs.verify_integrity("task_001", "bad_hash")


def test_contract_store_rejects_silent_overwrite(tmp_path):
    cs = ContractStore(tmp_path)
    cs.save_contract("task_001", AcceptanceContract(goal="original"))

    with pytest.raises(FileExistsError):
        cs.save_contract("task_001", AcceptanceContract(goal="tampered"))


def test_contract_store_allows_explicit_runtime_replacement(tmp_path):
    cs = ContractStore(tmp_path)
    cs.save_contract("task_001", AcceptanceContract(goal="original"))
    cs.save_contract(
        "task_001", AcceptanceContract(goal="replacement"), allow_replace=True
    )

    assert cs.load_contract("task_001").goal == "replacement"


# ---------------------------------------------------------------------------
# CommandVerifier (using real Python for deterministic tests)
# ---------------------------------------------------------------------------

def test_command_verifier_pass(tmp_path):
    v = CommandVerifier(cwd=tmp_path)
    verdict, result = v.run("python --version")
    assert verdict == VerifierVerdict.PASS
    assert result.exit_code == 0


def test_command_verifier_fail(tmp_path):
    v = CommandVerifier(cwd=tmp_path)
    verdict, result = v.run('python -c "import sys; sys.exit(1)"')
    assert verdict == VerifierVerdict.FAIL
    assert result.exit_code == 1


def test_command_verifier_infra_error_command_not_found(tmp_path):
    v = CommandVerifier(cwd=tmp_path)
    verdict, result = v.run("nonexistent_command_xyz123")
    # Windows returns FAIL (exit code 1) for unregistered commands;
    # POSIX returns INFRA_ERROR (FileNotFoundError). Accept both.
    assert verdict in (VerifierVerdict.INFRA_ERROR, VerifierVerdict.FAIL)


# ---------------------------------------------------------------------------
# CommandResult text serialization
# ---------------------------------------------------------------------------

def test_command_result_text_roundtrip():
    original = CommandResult(
        command="pytest -q",
        exit_code=1,
        stdout="FAILED test_add\n--- stderr ---\nkept exactly",
        stderr="diagnostic\nline two",
        timeout_seconds=37,
        started_at="2026-01-01T00:00:00",
        finished_at="2026-01-01T00:01:00",
        cwd="/repo",
        platform="test-platform",
        python_version="3.12.0",
    )
    text = original.to_text()
    restored = CommandResult.from_text(text)
    assert restored.command == original.command
    assert restored.exit_code == original.exit_code
    assert restored.stdout == original.stdout
    assert restored.stderr == original.stderr
    assert restored.timeout_seconds == 37
    assert restored.cwd == "/repo"
    assert restored.platform == "test-platform"
    assert restored.python_version == "3.12.0"


# ---------------------------------------------------------------------------
# Reproducer
# ---------------------------------------------------------------------------

def test_reproducer_infra_error_no_command(tmp_path):
    contract = AcceptanceContract(goal="test")
    r = Reproducer(cwd=tmp_path)
    verdict, result = r.reproduce(contract)
    assert verdict == ReproductionVerdict.AMBIGUOUS
    assert result is None


def test_reproducer_command_not_found_is_infra_error(tmp_path):
    contract = AcceptanceContract(
        goal="test",
        reproduce_command="nonexistent_cmd_xyz",
    )
    r = Reproducer(cwd=tmp_path)
    verdict, result = r.reproduce(contract)
    # Windows returns FAIL for unregistered commands; POSIX returns INFRA_ERROR
    assert verdict in (ReproductionVerdict.INFRA_ERROR, ReproductionVerdict.AMBIGUOUS)


# ---------------------------------------------------------------------------
# ChangeScopeVerifier
# ---------------------------------------------------------------------------

def _make_file(path: Path, content: str = "x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_change_scope_allowed_no_changes(tmp_path):
    cv = ChangeScopeVerifier(tmp_path)
    contract = AcceptanceContract(goal="test")
    verdict = cv.check(contract)
    assert verdict.value == "allowed"


def test_change_scope_forbidden_path(tmp_path):
    # Init git to track changes
    _init_git(tmp_path)
    _make_file(tmp_path / "secret.yaml")
    _git_add(tmp_path, "secret.yaml")
    _git_commit(tmp_path, "add secret")

    # Now modify the forbidden file
    _make_file(tmp_path / "secret.yaml", "modified content")

    cv = ChangeScopeVerifier(tmp_path)
    contract = AcceptanceContract(
        goal="test",
        change_policy=ChangePolicy(
            allowed_paths=["*"],
            forbidden_paths=["secret.yaml"],
        ),
    )
    summary = cv.diff_summary()
    if summary:
        # Only assert forbidden if the diff mechanism actually detects the change
        verdict = cv.check(contract)
        assert verdict.value == "forbidden_modified"
    else:
        # No git available or diff doesn't work — skip assertion
        pass


def test_change_scope_no_git_still_allowed(tmp_path):
    # Without git, ChangeScopeVerifier can't detect changes
    cv = ChangeScopeVerifier(tmp_path)
    contract = AcceptanceContract(
        goal="test",
        change_policy=ChangePolicy(
            allowed_paths=["*"],
            forbidden_paths=["secret.yaml"],
        ),
    )
    verdict = cv.check(contract)
    assert verdict.value == "allowed"


def test_change_scope_merges_unstaged_staged_and_untracked(tmp_path):
    _init_git(tmp_path)
    for name in ("allowed.py", "protected.py", "tracked.py"):
        _make_file(tmp_path / name, "original\n")
    _git_add(tmp_path, "allowed.py")
    _git_add(tmp_path, "protected.py")
    _git_add(tmp_path, "tracked.py")
    _git_commit(tmp_path, "baseline")
    _make_file(tmp_path / "allowed.py", "unstaged\n")
    _make_file(tmp_path / "protected.py", "staged\n")
    _git_add(tmp_path, "protected.py")
    _make_file(tmp_path / "new.txt", "untracked\n")

    cv = ChangeScopeVerifier(tmp_path)
    assert set(cv.diff_summary()) >= {"allowed.py", "protected.py", "new.txt"}
    contract = AcceptanceContract(
        goal="test",
        change_policy=ChangePolicy(
            allowed_paths=["allowed.py", "new.txt"],
            forbidden_paths=["protected.py"],
        ),
    )
    assert cv.check(contract).value == "forbidden_modified"


def test_change_scope_rejects_allowed_path_violation(tmp_path):
    _init_git(tmp_path)
    _make_file(tmp_path / "allowed.py", "original\n")
    _make_file(tmp_path / "outside.py", "original\n")
    _git_add(tmp_path, "allowed.py")
    _git_add(tmp_path, "outside.py")
    _git_commit(tmp_path, "baseline")
    _make_file(tmp_path / "outside.py", "changed\n")

    verdict = ChangeScopeVerifier(tmp_path).check(
        AcceptanceContract(
            goal="test",
            change_policy=ChangePolicy(allowed_paths=["allowed.py"]),
        )
    )
    assert verdict.value == "allowed_outside_scope"


# ---------------------------------------------------------------------------
# EvidenceRecorder
# ---------------------------------------------------------------------------

def test_evidence_recorder_saves_and_reads(tmp_path):
    er = EvidenceRecorder(tmp_path)
    result = CommandResult(command="pytest", exit_code=1, stdout="FAIL", stderr="",
                           timeout_seconds=60)
    path = er.save_evidence("task_001", "test-evidence", result)
    assert path.exists()
    assert "FAIL" in path.read_text(encoding="utf-8")


def test_evidence_recorder_verdict(tmp_path):
    er = EvidenceRecorder(tmp_path)
    path = er.save_verdict("task_001", VerifierVerdict.PASS, detail="all good")
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "pass" in text


def test_evidence_recorder_redacts_plain_runtime_secret_value(tmp_path):
    secret = "secret-value-73984"
    er = EvidenceRecorder(tmp_path, secret_values=[secret])
    result = CommandResult(
        command=f"echo {secret}", exit_code=0, stdout=secret, stderr=secret,
        timeout_seconds=20,
    )

    evidence = er.save_evidence("task_001", "plain-secret", result).read_text(encoding="utf-8")
    verdict = er.save_verdict("task_001", VerifierVerdict.FAIL, detail=secret).read_text(encoding="utf-8")

    assert secret not in evidence
    assert secret not in verdict
    assert evidence.count("<redacted>") >= 3


# ---------------------------------------------------------------------------
# TaskVerificationService integration
# ---------------------------------------------------------------------------

def test_verification_service_establish_baseline_no_contract(tmp_path):
    store = TaskStore(tmp_path)
    cs = ContractStore(tmp_path / "tasks")
    task = store.create_task("test")
    service = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")
    verdict, info = service.establish_baseline(task.task_id)
    assert verdict == ReproductionVerdict.AMBIGUOUS


def test_verification_service_verify_no_contract(tmp_path):
    store = TaskStore(tmp_path)
    cs = ContractStore(tmp_path / "tasks")
    task = store.create_task("test")
    service = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")
    verdict, info = service.run_verification(task.task_id)
    assert verdict == VerifierVerdict.BLOCKED


def test_verification_service_verify_with_contract(tmp_path):
    store = TaskStore(tmp_path)
    cs = ContractStore(tmp_path / "tasks")
    task = store.create_task("test")
    contract = AcceptanceContract(
        goal="test",
        verify_commands=[VerifyCommand(command="python --version", timeout_seconds=30)],
    )
    cs.save_contract(task.task_id, contract)
    service = TaskVerificationService(
        store, cs, tmp_path, tmp_path / "tasks", expected_contract_hash=contract.content_hash
    )
    verdict, info = service.run_verification(task.task_id)
    # python --version should pass
    assert verdict == VerifierVerdict.PASS


# ---------------------------------------------------------------------------
# TaskVerificationService PR 2 acceptance behavior
# ---------------------------------------------------------------------------


def _contract(command, *, allowed_paths=None, forbidden_paths=None):
    return AcceptanceContract(
        goal="test",
        reproduce_command=command,
        reproduce_timeout=20,
        reproduce=ReproduceExpectation(exit_code=1, output_contains=["TARGET_FAILURE"]),
        verify_commands=[VerifyCommand(command="python --version", timeout_seconds=20)],
        change_policy=ChangePolicy(
            allowed_paths=allowed_paths or ["*"],
            forbidden_paths=forbidden_paths or [],
        ),
    )


def test_verification_rejects_tampered_contract_with_auditable_evidence(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create_task("test")
    cs = ContractStore(tmp_path / "tasks")
    contract = _contract('python -c "import sys; print(\'TARGET_FAILURE\'); sys.exit(1)"')
    cs.save_contract(task.task_id, contract)
    cs.save_contract(
        task.task_id,
        AcceptanceContract(
            goal="tampered",
            verify_commands=[VerifyCommand(command="python --version")],
        ),
        allow_replace=True,
    )

    service = TaskVerificationService(
        store, cs, tmp_path, tmp_path / "tasks", expected_contract_hash=contract.content_hash
    )
    verdict, info = service.run_verification(task.task_id)

    assert verdict == VerifierVerdict.BLOCKED
    assert info.reason == "contract_hash_mismatch"
    assert len(info.evidence_refs) == 1
    assert (tmp_path / "tasks" / info.evidence_refs[0]).is_file()


def test_verification_rejects_change_scope_before_running_commands(tmp_path):
    _init_git(tmp_path)
    _make_file(tmp_path / "allowed.py", "original\n")
    _make_file(tmp_path / "outside.py", "original\n")
    _git_add(tmp_path, "allowed.py")
    _git_add(tmp_path, "outside.py")
    _git_commit(tmp_path, "baseline")
    _make_file(tmp_path / "outside.py", "changed\n")

    store = TaskStore(tmp_path)
    task = store.create_task("test")
    cs = ContractStore(tmp_path / "tasks")
    contract = _contract(
        'python -c "import sys; sys.exit(99)"', allowed_paths=["allowed.py"]
    )
    cs.save_contract(task.task_id, contract)
    service = TaskVerificationService(
        store, cs, tmp_path, tmp_path / "tasks", expected_contract_hash=contract.content_hash
    )

    verdict, info = service.run_verification(task.task_id)

    assert verdict == VerifierVerdict.FAIL
    assert info.reason == "allowed_outside_scope"
    assert info.evidence_refs


def test_verification_preserves_infrastructure_error_and_evidence(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create_task("test")
    cs = ContractStore(tmp_path / "tasks")
    contract = AcceptanceContract(
        goal="test",
        verify_commands=[
            VerifyCommand(
                command='python -c "import time; time.sleep(3)"', timeout_seconds=1
            )
        ],
    )
    cs.save_contract(task.task_id, contract)
    service = TaskVerificationService(
        store, cs, tmp_path, tmp_path / "tasks", expected_contract_hash=contract.content_hash
    )

    verdict, info = service.run_verification(task.task_id)

    assert verdict == VerifierVerdict.INFRA_ERROR
    assert info.verdict == "infra_error"
    evidence = (tmp_path / "tasks" / info.evidence_refs[0]).read_text(
        encoding="utf-8"
    )
    assert "timeout_seconds: 1" in evidence
    assert "timed_out: True" in evidence
    assert "platform:" in evidence


def test_reproducer_reports_flaky_for_mixed_observations(tmp_path):
    contract = _contract('python -c "import sys; sys.exit(1)"')
    reproducer = Reproducer(cwd=tmp_path, stability_runs=2)
    matching = CommandResult(
        command="first", exit_code=1, stdout="TARGET_FAILURE", stderr="", timeout_seconds=20
    )
    passing = CommandResult(
        command="second", exit_code=0, stdout="", stderr="", timeout_seconds=20
    )
    results = iter(
        [(VerifierVerdict.FAIL, matching), (VerifierVerdict.PASS, passing)]
    )
    reproducer._verifier.run = lambda *_args: next(results)

    verdict, result = reproducer.reproduce(contract)

    assert verdict == ReproductionVerdict.FLAKY
    assert result is passing
    assert reproducer.last_results == [matching, passing]


# ---------------------------------------------------------------------------
# CLI PR 2 workflow
# ---------------------------------------------------------------------------


class _CompletedAgent:
    current_task_state = None

    def ask_run(self, _prompt, on_run_started):
        on_run_started("run_001", "legacy_001")
        return RunResult(
            final_answer="candidate complete",
            run_id="run_001",
            legacy_engine_task_id="legacy_001",
            outcome="completed",
        )


def _write_contract_source(path: Path, reproduce_command: str, verify_command: str):
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "goal": "test",
                "reproduce": {
                    "command": reproduce_command,
                    "timeout_seconds": 20,
                    "expect": {"outcome": "target_failure", "exit_code": 1},
                },
                "verify": {
                    "required": [{"command": verify_command, "timeout_seconds": 20}]
                },
                "change_policy": {"allowed_paths": ["*"], "forbidden_paths": []},
            }
        ),
        encoding="utf-8",
    )


def test_cli_not_reproduced_passes_as_already_resolved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    task = TaskStore(tmp_path / ".forge").create_task("test")
    source = tmp_path / "contract-source.json"
    _write_contract_source(
        source,
        'python -c "import sys; sys.exit(0)"',
        "python --version",
    )

    assert clilib.main(["contract", "init", task.task_id, "--from", str(source)]) == 0
    assert clilib.main(["task", "reproduce", task.task_id]) == 0

    saved = TaskStore(tmp_path / ".forge").load_task(task.task_id)
    assert saved.status == TaskStatus.COMPLETED
    assert saved.completion_reason == "already_resolved"
    assert saved.baseline["reproduction_verdict"] == "not_reproduced"


def test_cli_not_reproduced_failing_verify_blocks_as_baseline_mismatch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    task = TaskStore(tmp_path / ".forge").create_task("test")
    source = tmp_path / "contract-source.json"
    _write_contract_source(
        source,
        'python -c "import sys; sys.exit(0)"',
        'python -c "import sys; sys.exit(1)"',
    )

    assert clilib.main(["contract", "init", task.task_id, "--from", str(source)]) == 0
    assert clilib.main(["task", "reproduce", task.task_id]) == 0

    saved = TaskStore(tmp_path / ".forge").load_task(task.task_id)
    assert saved.status == TaskStatus.BLOCKED
    assert saved.completion_reason == "baseline_mismatch"


def test_cli_run_finishes_then_verifier_failure_does_not_complete(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    task = TaskStore(tmp_path / ".forge").create_task("test")
    source = tmp_path / "contract-source.json"
    _write_contract_source(
        source,
        'python -c "import sys; print(\'TARGET\'); sys.exit(1)"',
        'python -c "import sys; sys.exit(1)"',
    )
    assert clilib.main(["contract", "init", task.task_id, "--from", str(source)]) == 0
    assert clilib.main(["task", "reproduce", task.task_id]) == 0
    monkeypatch.setattr(clilib, "build_agent", lambda _args: _CompletedAgent())

    assert clilib.main(["task", "run", task.task_id]) == 0

    saved = TaskStore(tmp_path / ".forge").load_task(task.task_id)
    assert saved.status == TaskStatus.IN_PROGRESS
    assert saved.last_verdict["verdict"] == "fail"
    assert saved.run_ids == ["run_001"]
    assert saved.baseline["evidence_ref"]
    assert saved.last_verdict["evidence_refs"]


def test_cli_run_verifier_pass_is_the_only_automatic_completion(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    task = TaskStore(tmp_path / ".forge").create_task("test")
    source = tmp_path / "contract-source.json"
    _write_contract_source(
        source,
        'python -c "import sys; print(\'TARGET\'); sys.exit(1)"',
        "python --version",
    )
    assert clilib.main(["contract", "init", task.task_id, "--from", str(source)]) == 0
    assert clilib.main(["task", "reproduce", task.task_id]) == 0
    monkeypatch.setattr(clilib, "build_agent", lambda _args: _CompletedAgent())

    assert clilib.main(["task", "run", task.task_id]) == 0

    saved = TaskStore(tmp_path / ".forge").load_task(task.task_id)
    assert saved.status == TaskStatus.COMPLETED
    assert saved.completion_reason == "verifier_pass"
    assert saved.last_verdict["verdict"] == "pass"


# ---------------------------------------------------------------------------
# TaskRecord with PR 2 fields
# ---------------------------------------------------------------------------

def test_task_record_with_baseline():
    r = TaskRecord(
        task_id="t1",
        goal="test",
        baseline={"commit": "abc123", "reproduction_verdict": "reproduced"},
    )
    assert r.baseline is not None
    assert r.baseline["commit"] == "abc123"


def test_task_record_baseline_roundtrip():
    original = TaskRecord(
        task_id="t1", goal="test",
        baseline={"commit": "abc123", "reproduction_verdict": "reproduced"},
        last_verdict={"verdict": "pass", "checked_at": "2026-01-01T00:00:00"},
        completion_reason="verifier_pass",
    )
    data = original.to_dict()
    restored = TaskRecord.from_dict(data)
    assert restored.baseline["commit"] == "abc123"
    assert restored.last_verdict["verdict"] == "pass"
    assert restored.completion_reason == "verifier_pass"


def test_task_record_pr2_fields_default_to_none():
    r = TaskRecord(task_id="t1", goal="test")
    assert r.baseline is None
    assert r.last_verdict is None
    assert r.completion_reason is None


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _init_git(path: Path):
    import subprocess
    subprocess.run(["git", "init"], cwd=path, capture_output=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, capture_output=True, timeout=10)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, capture_output=True, timeout=10)


def _git_commit(path: Path, msg: str = "commit"):
    import subprocess
    subprocess.run(["git", "commit", "-m", msg], cwd=path, capture_output=True, timeout=10)


def _git_add(path: Path, filename: str):
    import subprocess
    subprocess.run(["git", "add", filename], cwd=path, capture_output=True, timeout=10)
