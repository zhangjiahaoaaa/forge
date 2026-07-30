"""Tests for PR 3: Loop Controller + Stop / Escalation.

Covers the 9 required scenarios from the roadmap.
All deterministic — no live provider needed.
"""

import json
from datetime import datetime, timedelta, timezone

from forge.core.run_result import RunResult
from forge.features.loop import (
    CycleContext,
    EscalationArtifact,
    FailureCategory,
    FailureClassifier,
    LoopController,
    LoopPolicy,
    NoProgressDetector,
    TaskContextBuilder,
)
from forge.features.task_record import TaskRecord, TaskPhase, TaskStatus, TaskStore
from forge.features.verification import ContractStore, TaskVerificationService


# ---------------------------------------------------------------------------
# TaskPhase & TaskStatus
# ---------------------------------------------------------------------------

def test_task_phase_enum_values():
    assert TaskPhase.BASELINING.value == "baselining"
    assert TaskPhase.REPLANNING.value == "replanning"


def test_task_status_includes_waiting_human():
    assert TaskStatus.WAITING_HUMAN.value == "waiting_human"


def test_task_record_phase_defaults():
    r = TaskRecord(task_id="t1", goal="test")
    assert r.phase == ""
    assert r.cycle == 0
    assert r.max_cycles == 4


def test_task_record_phase_roundtrip():
    original = TaskRecord(task_id="t1", goal="test", phase="verifying", cycle=2, max_cycles=6)
    data = original.to_dict()
    restored = TaskRecord.from_dict(data)
    assert restored.phase == "verifying"
    assert restored.cycle == 2
    assert restored.max_cycles == 6


def test_task_record_policy_roundtrip():
    original = TaskRecord(task_id="t1", goal="test", policy={"max_cycles": 3, "key": "val"})
    data = original.to_dict()
    restored = TaskRecord.from_dict(data)
    assert restored.policy["max_cycles"] == 3


# ---------------------------------------------------------------------------
# LoopPolicy
# ---------------------------------------------------------------------------

def test_loop_policy_defaults():
    p = LoopPolicy.default()
    assert p.max_cycles == 4
    assert p.max_total_tool_steps == 80
    assert p.require_replan_before_waiting_human is True


def test_loop_policy_roundtrip():
    p = LoopPolicy(max_cycles=3)
    data = p.to_dict()
    restored = LoopPolicy.from_dict(data)
    assert restored.max_cycles == 3


def test_loop_policy_from_none():
    p = LoopPolicy.from_dict(None)
    assert p.max_cycles == 4


# ---------------------------------------------------------------------------
# NoProgressDetector
# ---------------------------------------------------------------------------

def test_no_progress_insufficient_history():
    d = NoProgressDetector()
    result = d.assess({"verifier_passed": 0, "verifier_total": 2, "failure_fingerprint": "a",
                        "diff_fingerprint": "d1", "tool_call_count": 5})
    assert not result.is_stagnant
    assert result.reason == "insufficient_history"


def test_no_progress_strong_stagnation():
    d = NoProgressDetector()
    d.record_cycle({"verifier_passed": 0, "verifier_total": 2, "failure_fingerprint": "abc",
                     "diff_fingerprint": "d1", "tool_call_count": 5})

    result = d.assess({"verifier_passed": 0, "verifier_total": 2, "failure_fingerprint": "abc",
                        "diff_fingerprint": "d1", "tool_call_count": 5})
    assert result.is_stagnant
    assert result.reason == "strong_stagnation"


def test_no_progress_outcome_improved_not_stagnant():
    d = NoProgressDetector()
    d.record_cycle({"verifier_passed": 0, "verifier_total": 2, "failure_fingerprint": "abc",
                     "diff_fingerprint": "d1", "tool_call_count": 5})

    result = d.assess({"verifier_passed": 1, "verifier_total": 2, "failure_fingerprint": "abc",
                        "diff_fingerprint": "d1", "tool_call_count": 5})
    assert not result.is_stagnant
    assert result.reason == "outcome_improved"


def test_no_progress_all_pass_not_stagnant():
    d = NoProgressDetector()
    d.record_cycle({"verifier_passed": 1, "verifier_total": 2, "failure_fingerprint": "abc",
                     "diff_fingerprint": "d1", "tool_call_count": 5})

    result = d.assess({"verifier_passed": 2, "verifier_total": 2, "failure_fingerprint": "def",
                        "diff_fingerprint": "d2", "tool_call_count": 3})
    assert not result.is_stagnant


def test_no_progress_history_roundtrip_detects_stagnation():
    history = [{"verifier_passed": 0, "verifier_total": 1, "failure_fingerprint": "same",
                "diff_fingerprint": "same", "tool_call_count": 2}]
    result = NoProgressDetector(history).assess(dict(history[0]))
    assert result.is_stagnant
    assert result.reason == "strong_stagnation"


# ---------------------------------------------------------------------------
# FailureClassifier
# ---------------------------------------------------------------------------

def test_classifier_test_failure():
    assert FailureClassifier.classify(last_verdict={"verdict": "fail"}) == FailureCategory.TEST_FAILURE


def test_classifier_infra_error():
    assert FailureClassifier.classify(last_verdict={"verdict": "infra_error"}) == FailureCategory.ENVIRONMENT_ERROR


def test_classifier_stopped_is_budget():
    assert FailureClassifier.classify(run_outcome="stopped") == FailureCategory.BUDGET_EXCEEDED


def test_classifier_forbidden_path():
    assert FailureClassifier.classify(last_verdict={"verdict": "forbidden_path_modified"}) == FailureCategory.CHANGE_SCOPE_VIOLATION


def test_classifier_approval_denied():
    assert FailureClassifier.classify(stop_reason="approval_denied") == FailureCategory.PERMISSION_DENIED


# ---------------------------------------------------------------------------
# EscalationArtifact
# ---------------------------------------------------------------------------

def test_escalation_artifact_to_text():
    e = EscalationArtifact(
        task_id="task_001",
        reason="strong_stagnation",
        decision_needed="应该终止还是扩大范围？",
        completed=["已复现目标失败", "已尝试两种策略"],
        options=["终止", "扩大范围"],
        recommended_option="终止",
        evidence_refs=["evidence/verify-cycle-2.txt"],
    )
    text = e.to_text()
    assert "task_001" in text
    assert "strong_stagnation" in text
    assert "终止" in text


def test_escalation_artifact_save(tmp_path):
    e = EscalationArtifact(task_id="t1", reason="test")
    p = e.save(tmp_path / "escalation.txt")
    assert p.exists()
    assert "t1" in p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# CycleContext
# ---------------------------------------------------------------------------

def test_cycle_context_defaults():
    ctx = CycleContext()
    assert ctx.goal == ""
    assert ctx.current_cycle == 0
    assert ctx.max_cycles == 4


def test_cycle_context_to_prompt_text():
    ctx = CycleContext(
        goal="修复 calculator.add",
        acceptance_summary="2 verify commands",
        current_phase="implementing",
        current_cycle=2,
        max_cycles=4,
        latest_verifier_failure="test_add_negative failed",
        forbidden_actions=["tests/acceptance/**"],
        requested_outcome="修复剩余失败测试",
    )
    text = ctx.to_prompt_text()
    assert "calculator.add" in text
    assert "2/4" in text
    assert "test_add_negative" in text


# ---------------------------------------------------------------------------
# TaskContextBuilder
# ---------------------------------------------------------------------------

def test_context_builder_with_task():
    builder = TaskContextBuilder()
    task = TaskRecord(task_id="t1", goal="fix bug", phase="running", cycle=2, max_cycles=4,
                       last_verdict={"verdict": "fail", "evidence_refs": ["e1.txt"]})
    ctx = builder.build(task)
    assert ctx.goal == "fix bug"
    assert ctx.current_phase == "running"
    assert ctx.current_cycle == 2


# ---------------------------------------------------------------------------
# LoopController — advance() 状态机
# ---------------------------------------------------------------------------

def _make_task(store, goal="test", phase="", cycle=0, status="created",
               baseline=None, last_verdict=None) -> TaskRecord:
    t = store.create_task(goal)
    t.phase = phase
    t.cycle = cycle
    t.status = TaskStatus(status)
    if baseline:
        t.baseline = baseline
    if last_verdict:
        t.last_verdict = last_verdict
    store.save_task(t)
    return t


def test_advance_into_terminal_stops(tmp_path):
    store = TaskStore(tmp_path)
    task = _make_task(store, status="completed")
    cs = ContractStore(tmp_path / "tasks")
    vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")
    ctrl = LoopController(store, cs, vs)
    result = ctrl.advance(task.task_id)
    assert result.get("action") == "stopped"


def test_advance_baseline_no_contract(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create_task("test")
    cs = ContractStore(tmp_path / "tasks")
    vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")
    ctrl = LoopController(store, cs, vs)
    result = ctrl.advance(task.task_id)
    assert result.get("action") in ("blocked", "baseline_ready", "waiting_human")


def test_advance_no_agent_factory(tmp_path):
    store = TaskStore(tmp_path)
    task = _make_task(store, phase="ready", status="in_progress")
    cs = ContractStore(tmp_path / "tasks")
    vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")
    ctrl = LoopController(store, cs, vs)
    result = ctrl.advance(task.task_id)
    assert result.get("action") == "blocked"


def test_advance_max_cycles_reached(tmp_path):
    store = TaskStore(tmp_path)
    task = _make_task(store, phase="ready", status="in_progress", cycle=4)
    cs = ContractStore(tmp_path / "tasks")
    vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")
    ctrl = LoopController(store, cs, vs)
    result = ctrl.advance(task.task_id)
    assert result.get("action") == "blocked"
    assert "max_cycles" in result.get("reason", "")


def test_advance_full_loops_until_stop(tmp_path):
    store = TaskStore(tmp_path)
    task = _make_task(store, phase="ready", status="in_progress")
    cs = ContractStore(tmp_path / "tasks")
    vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")
    ctrl = LoopController(store, cs, vs)
    result = ctrl.advance_full(task.task_id)
    assert result.get("action") in ("blocked", "stopped")


class _StartedThenFailsAgent:
    current_task_state = None

    def ask_run(self, _prompt, on_run_started):
        on_run_started("run_failure", "legacy_failure")
        raise RuntimeError("provider exploded")


def test_run_exception_finishes_attempt_and_reloads_task(tmp_path):
    store = TaskStore(tmp_path)
    task = _make_task(store, phase="ready", status="in_progress")
    task.contract_hash = "frozen"
    store.save_task(task)
    cs = ContractStore(tmp_path / "tasks")
    vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")
    result = LoopController(store, cs, vs, build_agent_fn=_StartedThenFailsAgent).advance(task.task_id)

    saved = store.load_task(task.task_id)
    attempt = store.list_attempts(task.task_id)[0]
    assert result["action"] == "error"
    assert saved.phase == "ready"
    assert saved.current_run_id == ""
    assert attempt.outcome == "failed"
    assert attempt.finished_at


def test_not_reproduced_verify_receives_frozen_contract_and_manifest(tmp_path):
    store = TaskStore(tmp_path)
    task = _make_task(store, status="in_progress")
    task.contract_hash = "frozen-hash"
    store.save_task(task)
    cs = ContractStore(tmp_path / "tasks")

    class _Verification:
        def establish_baseline(self, _task_id, expected_contract_hash):
            assert expected_contract_hash == "frozen-hash"
            info = type("Baseline", (), {
                "commit": "", "workspace_fingerprint": "", "reproduction_verdict": "not_reproduced",
                "failure_fingerprint": "", "evidence_ref": "", "workspace_files": {"app.py": "hash"},
            })()
            from forge.features.verification import ReproductionVerdict
            return ReproductionVerdict.NOT_REPRODUCED, info

        def run_verification(self, _task_id, **kwargs):
            assert kwargs["expected_contract_hash"] == "frozen-hash"
            assert kwargs["baseline_files"] == {"app.py": "hash"}
            from forge.features.verification import VerifierVerdict
            return VerifierVerdict.PASS, type("Verdict", (), {"evidence_refs": []})()

    result = LoopController(store, cs, _Verification()).advance(task.task_id)
    assert result["action"] == "completed"


def test_single_attempt_over_wall_time_is_blocked(tmp_path):
    store = TaskStore(tmp_path)
    task = _make_task(store, phase="running", status="in_progress")
    task.policy = {"max_wall_time_seconds": 1, "max_total_tool_steps": 80, "max_cycles": 4}
    store.save_task(task)
    store.record_run_started(task.task_id, "run_slow")
    attempts_path = tmp_path / "tasks" / task.task_id / "attempts.jsonl"
    attempt = store.list_attempts(task.task_id)[0]
    attempt.started_at = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    attempts_path.write_text(json.dumps(attempt.to_dict()) + "\n", encoding="utf-8")
    cs = ContractStore(tmp_path / "tasks")
    vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")

    result = LoopController(store, cs, vs).advance(task.task_id)
    assert result["action"] == "blocked"
    assert result["reason"] == "max_wall_time"


def test_run_tool_budget_is_blocked_before_verify(tmp_path):
    store = TaskStore(tmp_path)
    task = _make_task(store, phase="ready", status="in_progress")
    task.contract_hash = "frozen"
    task.policy = {"max_total_tool_steps": 2, "max_wall_time_seconds": 1800, "max_cycles": 4}
    store.save_task(task)
    cs = ContractStore(tmp_path / "tasks")
    vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")

    class _Agent:
        current_task_state = None
        max_steps = 50

        def ask_run(self, _prompt, on_run_started):
            on_run_started("run_budget", "legacy")
            return RunResult(run_id="run_budget", outcome="stopped", metadata={"tool_steps": 2})

    result = LoopController(store, cs, vs, build_agent_fn=_Agent).advance(task.task_id)
    saved = store.load_task(task.task_id)
    assert result["action"] == "blocked"
    assert saved.completion_reason == "max_total_tool_steps_reached"
    assert saved.phase == ""
