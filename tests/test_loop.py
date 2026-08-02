"""Tests for PR 3: Loop Controller + Stop / Escalation.

Covers the 9 required scenarios from the roadmap.
All deterministic — no live provider needed.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def test_assess_uses_latest_cycle_tool_count(tmp_path):
    """相同的连续尝试不能因累计工具数增加而逃过停滞检测。"""
    store = TaskStore(tmp_path / ".forge")
    task = _make_task(
        store,
        phase="assessing_progress",
        status="in_progress",
        baseline={"workspace_fingerprint": "same", "failure_fingerprint": "same"},
        last_verdict={"verdict": "fail"},
    )
    task.replan_already_done = True
    store.save_task(task)
    store.record_run_started(task.task_id, "run_1")
    store.record_run_finished(task.task_id, "run_1", tool_steps=1)

    ctrl = LoopController(
        store, ContractStore(tmp_path / "tasks"),
        TaskVerificationService(store, ContractStore(tmp_path / "tasks"), tmp_path, tmp_path / "tasks"),
    )
    first = ctrl.advance(task.task_id)
    assert first["action"] == "continue"

    saved = store.load_task(task.task_id)
    saved.phase = "assessing_progress"
    store.save_task(saved)
    store.record_run_started(task.task_id, "run_2")
    store.record_run_finished(task.task_id, "run_2", tool_steps=1)

    second = ctrl.advance(task.task_id)
    assert second["action"] == "waiting_human"
    assert second["task"].completion_reason == "strong_stagnation"


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


# ---------------------------------------------------------------------------
# PR 3b — checkpoint resume
# ---------------------------------------------------------------------------

class _ResumeRecordingAgent:
    """记录 ask_run 收到的 run_id 与 session，用于断言恢复语义。"""

    current_task_state = None

    def __init__(self):
        self.calls = []

    def current_runtime_identity(self):
        return {"model": "scripted", "tool_signature": "test-sig"}

    def ask_run(self, prompt, on_run_started=None, run_id=None):
        self.calls.append({"prompt": prompt, "run_id": run_id})
        actual_run_id = run_id or f"new_run_{len(self.calls)}"
        if on_run_started:
            on_run_started(actual_run_id, "legacy")
        return RunResult(
            run_id=actual_run_id, outcome="completed",
            final_answer="done", metadata={"tool_steps": 1},
        )


def _make_resumable_task(store, tmp_path, valid=True):
    """构造一个 run_interrupted 状态、带可恢复引用的 Task。

    valid=True 时同时落盘真实的 session 文件（含 checkpoint），
    使 _checkpoint_valid 的 session/checkpoint 校验与真实运行一致。
    """
    task = _make_task(store, phase="run_interrupted", status="in_progress", cycle=2)
    task.contract_hash = "frozen"
    store.save_task(task)
    if valid:
        sess_dir = tmp_path / ".forge" / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "session_orig.json").write_text(
            json.dumps({
                "id": "session_orig",
                "history": [],
                "checkpoints": {
                    "current_id": "ckpt_1",
                    "items": {"ckpt_1": {"checkpoint_id": "ckpt_1", "status": "ok"}},
                },
            }),
            encoding="utf-8",
        )
        store.record_run_started(task.task_id, "run_orig", "legacy_orig")
        attempts = store.list_attempts(task.task_id)
        attempts[0].session_id = "session_orig"
        attempts[0].checkpoint_ref = "ckpt_1"
        attempts[0].runtime_identity = {"model": "scripted", "tool_signature": "test-sig"}
        attempts[0].owner_pid = 0  # 模拟崩溃遗留：owner 进程已死
        store._write_attempts_unlocked(task.task_id, attempts)
    else:
        store.record_run_started(task.task_id, "run_bad", "legacy_bad")
        attempts = store.list_attempts(task.task_id)
        attempts[0].owner_pid = 0  # 模拟崩溃遗留：owner 进程已死
        store._write_attempts_unlocked(task.task_id, attempts)
    return task


def test_resume_valid_checkpoint_keeps_run_and_cycle(tmp_path):
    """有效 checkpoint：resume 同一 run_id，不增加 cycle。"""
    store = TaskStore(tmp_path)
    task = _make_resumable_task(store, tmp_path, valid=True)
    cs = ContractStore(tmp_path / "tasks")
    vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")

    resume_agent = _ResumeRecordingAgent()

    def _build_resume_agent(session_id):
        assert session_id == "session_orig", f"expected session_orig, got {session_id}"
        return resume_agent

    ctrl = LoopController(store, cs, vs, build_agent_fn=lambda: _ResumeRecordingAgent(),
                          build_resume_agent_fn=_build_resume_agent)

    # 第一次 advance：校验 checkpoint → resume_ready
    r1 = ctrl.advance(task.task_id)
    assert r1.get("action") == "resume_ready", f"got {r1.get('action')}"
    saved = store.load_task(task.task_id)
    assert saved.resume_ref is not None
    assert saved.resume_ref["run_id"] == "run_orig"

    # 第二次 advance：_do_run 恢复路径 — 不增加 cycle、注入原 run_id
    cycle_before = saved.cycle
    r2 = ctrl.advance(task.task_id)
    assert r2.get("action") == "run_completed", f"got {r2.get('action')}"
    assert resume_agent.calls, "resume agent was not called"
    assert resume_agent.calls[0]["run_id"] == "run_orig", \
        f"expected injected run_id run_orig, got {resume_agent.calls[0]['run_id']}"

    saved2 = store.load_task(task.task_id)
    assert saved2.cycle == cycle_before, f"resume must not increment cycle: {cycle_before} -> {saved2.cycle}"
    assert saved2.resume_ref is None, "resume_ref should be cleared after resume"
    # 同一 run_id 只记录一次 attempt
    run_ids = saved2.run_ids
    assert run_ids.count("run_orig") == 1, f"run_orig duplicated: {run_ids}"


def test_resume_invalid_checkpoint_falls_back_to_new_run(tmp_path):
    """无效 checkpoint（缺 session_id）：关闭旧 Run，启动新 Run，cycle 增加。"""
    store = TaskStore(tmp_path)
    task = _make_resumable_task(store, tmp_path, valid=False)
    cs = ContractStore(tmp_path / "tasks")
    vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")

    def _build_resume_agent(session_id):
        raise AssertionError("resume agent should not be called for invalid checkpoint")

    ctrl = LoopController(store, cs, vs, build_agent_fn=_ResumeRecordingAgent,
                          build_resume_agent_fn=_build_resume_agent)

    cycle_before = task.cycle
    result = ctrl.advance(task.task_id)
    # 第一次 advance：降级路径返回 resumed（关闭旧 run）
    assert result.get("action") == "resumed", f"got {result.get('action')}"
    saved = store.load_task(task.task_id)
    assert saved.resume_ref is None
    # 旧 attempt 已标记 interrupted
    attempts = store.list_attempts(task.task_id)
    assert attempts and attempts[0].outcome == "interrupted", \
        f"expected interrupted attempt, got {attempts}"

    # 第二次 advance：新 Run，cycle 增加
    r2 = ctrl.advance(task.task_id)
    saved2 = store.load_task(task.task_id)
    assert saved2.cycle == cycle_before + 1, "new run must increment cycle"
    assert r2.get("action") in ("run_completed", "blocked", "error")


def test_resume_session_only_checkpoint_is_valid(tmp_path):
    """有效 checkpoint 不强制要求 checkpoint_ref：session 历史本身即可恢复现场。"""
    store = TaskStore(tmp_path)
    # 手工构造：真实 session 文件存在，attempt 只有 session_id（无 checkpoint_ref）
    task = _make_task(store, phase="run_interrupted", status="in_progress", cycle=1)
    task.contract_hash = "frozen"
    store.save_task(task)
    sess_dir = tmp_path / ".forge" / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / "session_only.json").write_text(
        json.dumps({"id": "session_only", "history": [], "checkpoints": {"current_id": "", "items": {}}}),
        encoding="utf-8",
    )
    store.record_run_started(task.task_id, "run_s", "legacy_s")
    attempts = store.list_attempts(task.task_id)
    attempts[0].session_id = "session_only"
    attempts[0].runtime_identity = {"model": "scripted", "tool_signature": "test-sig"}
    attempts[0].owner_pid = 0  # 模拟崩溃遗留：owner 进程已死
    store._write_attempts_unlocked(task.task_id, attempts)

    cs = ContractStore(tmp_path / "tasks")
    vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")
    resume_agent = _ResumeRecordingAgent()

    def _build_resume_agent(session_id):
        assert session_id == "session_only"
        return resume_agent

    ctrl = LoopController(store, cs, vs, build_agent_fn=lambda: _ResumeRecordingAgent(),
                          build_resume_agent_fn=_build_resume_agent)
    r1 = ctrl.advance(task.task_id)
    assert r1.get("action") == "resume_ready", f"got {r1.get('action')}"
    saved = store.load_task(task.task_id)
    assert saved.resume_ref["run_id"] == "run_s"


def test_resume_identity_mismatch_degrades_to_new_run(tmp_path):
    """恢复 agent 的 model 身份与中断 attempt 不一致 → 降级新 Run（cycle 增加）。"""
    store = TaskStore(tmp_path)
    task = _make_resumable_task(store, tmp_path, valid=True)
    # 中断 attempt 记录的身份是 scripted
    attempts = store.list_attempts(task.task_id)
    attempts[0].runtime_identity = {"model": "scripted", "tool_signature": "sig-a"}
    store._write_attempts_unlocked(task.task_id, attempts)

    cs = ContractStore(tmp_path / "tasks")
    vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")

    class _MismatchAgent(_ResumeRecordingAgent):
        def current_runtime_identity(self):
            return {"model": "different-model", "tool_signature": "sig-b"}

    mismatched = _MismatchAgent()
    normal_agent = _ResumeRecordingAgent()

    def _build_resume_agent(session_id):
        return mismatched

    ctrl = LoopController(store, cs, vs, build_agent_fn=lambda: normal_agent,
                          build_resume_agent_fn=_build_resume_agent)

    cycle_before = task.cycle
    r1 = ctrl.advance(task.task_id)
    assert r1.get("action") == "resume_ready", f"got {r1.get('action')}"
    ctrl.advance(task.task_id)
    saved = store.load_task(task.task_id)
    assert saved.cycle == cycle_before + 1, "identity mismatch must degrade to a new run"
    assert saved.resume_ref is None
    # 降级后的新 Run 使用普通 agent 工厂，未注入原 run_id
    assert mismatched.calls == []
    assert normal_agent.calls and normal_agent.calls[0]["run_id"] is None


def test_recover_transitions_running_to_resume(tmp_path):
    """recover()：崩溃遗留的 running 任务 + active attempt → resume_ready。"""
    store = TaskStore(tmp_path)
    task = _make_task(store, phase="running", status="in_progress", cycle=2)
    task.contract_hash = "frozen"
    store.save_task(task)
    sess_dir = tmp_path / ".forge" / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / "session_r.json").write_text(
        json.dumps({"id": "session_r", "history": [], "checkpoints": {"current_id": "", "items": {}}}),
        encoding="utf-8",
    )
    store.record_run_started(task.task_id, "run_r", "legacy_r")
    attempts = store.list_attempts(task.task_id)
    attempts[0].session_id = "session_r"
    attempts[0].runtime_identity = {"model": "scripted", "tool_signature": "test-sig"}
    attempts[0].owner_pid = 0  # 模拟崩溃遗留：owner 进程已死
    store._write_attempts_unlocked(task.task_id, attempts)

    cs = ContractStore(tmp_path / "tasks")
    vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")

    def _build_resume_agent(session_id):
        return _ResumeRecordingAgent()

    ctrl = LoopController(store, cs, vs, build_agent_fn=lambda: _ResumeRecordingAgent(),
                          build_resume_agent_fn=_build_resume_agent)
    result = ctrl.recover(task.task_id)
    assert result.get("action") == "resume_ready", f"got {result.get('action')}"
    saved = store.load_task(task.task_id)
    assert saved.resume_ref["run_id"] == "run_r"
    # 无 active run 时 recover 不误伤
    idle = _make_task(store, phase="ready", status="in_progress")
    store.save_task(idle)
    assert ctrl.recover(idle.task_id).get("action") == "nothing"


def test_recover_restores_tool_steps_from_run_store(tmp_path):
    """崩溃后从 run_store 的 task_state.json 恢复中断 tool_steps 计入预算。"""
    store = TaskStore(tmp_path)
    task = _make_resumable_task(store, tmp_path, valid=False)  # 无 session → 降级
    run_id = "run_bad"
    run_dir = tmp_path / ".forge" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "task_state.json").write_text(
        json.dumps({"tool_steps": 7, "checkpoint_id": "ck_x"}), encoding="utf-8"
    )

    cs = ContractStore(tmp_path / "tasks")
    vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")
    ctrl = LoopController(store, cs, vs, build_agent_fn=_ResumeRecordingAgent,
                          build_resume_agent_fn=lambda sid: _ResumeRecordingAgent())
    result = ctrl.recover(task.task_id)
    assert result.get("action") == "resumed", f"got {result.get('action')}"
    attempts = store.list_attempts(task.task_id)
    assert attempts[0].outcome == "interrupted"
    assert attempts[0].tool_steps == 7, f"interrupted tool_steps must be 7, got {attempts[0].tool_steps}"
    assert attempts[0].checkpoint_ref == "ck_x"


def _recover_owner_worker(root: str, task_id: str, queue):
    """子进程：启动 Run 后保持存活（模拟正在执行），通知主进程。"""
    import time

    from forge.features.task_record import TaskStore
    store = TaskStore(root)
    store.record_run_started(task_id, "run_owned")
    queue.put("started")
    time.sleep(3)


def _claim_and_block_worker(workspace: str, task_id: str, ready_file: str, hold_file: str):
    """子进程 A：recover → resume_ready（认领 Run）→ _do_run 阻塞在 ask_run。

    模拟"恢复执行正在进行中"：主进程应看到 owner=A 存活而无法再次接管。
    """
    import time
    from pathlib import Path

    from forge.core.run_result import RunResult
    from forge.features.loop import LoopController
    from forge.features.task_record import TaskStore
    from forge.features.verification import ContractStore, TaskVerificationService

    ws = Path(workspace)
    store = TaskStore(ws)
    cs = ContractStore(ws / "tasks")
    vs = TaskVerificationService(store, cs, ws, ws / "tasks")

    class BlockingAgent:
        def current_runtime_identity(self):
            return {"model": "scripted", "tool_signature": "test-sig"}

        def ask_run(self, prompt, on_run_started=None, run_id=None):
            rid = run_id or "run_claim"
            if on_run_started:
                on_run_started(rid, "legacy")
            Path(ready_file).write_text("running", encoding="utf-8")
            while Path(hold_file).exists():
                time.sleep(0.1)
            return RunResult(run_id=rid, outcome="completed", final_answer="done",
                             metadata={"tool_steps": 1})

    ctrl = LoopController(store, cs, vs,
                          build_agent_fn=lambda: BlockingAgent(),
                          build_resume_agent_fn=lambda sid: BlockingAgent())
    rec = ctrl.recover(task_id)  # resume_ready + 原子认领
    if rec.get("action") != "resume_ready":
        Path(ready_file).write_text(f"recover_failed:{rec.get('action')}", encoding="utf-8")
        return
    ctrl.advance_full(task_id)  # _do_run：恢复执行，阻塞在 ask_run


def test_second_recover_cannot_steal_claimed_resume(tmp_path):
    """高风险回归：A 认领并恢复执行中，B 的 recover() 必须返回 in_progress，
    且不得修改 phase / resume_ref（A 的收尾不被干扰）。"""
    import subprocess
    import sys
    import time

    store = TaskStore(tmp_path)
    task = _make_resumable_task(store, tmp_path, valid=True)
    hold = tmp_path / "hold.txt"
    ready = tmp_path / "ready.txt"
    hold.write_text("hold", encoding="utf-8")

    tests_dir = str(Path(__file__).resolve().parent)
    forge_root = str(Path(__file__).resolve().parent.parent)
    script = (
        "import sys;"
        f"sys.path.insert(0, {forge_root!r});"
        f"sys.path.insert(0, {tests_dir!r});"
        "from test_loop import _claim_and_block_worker;"
        f"_claim_and_block_worker({str(tmp_path)!r}, {task.task_id!r}, "
        f"{str(ready)!r}, {str(hold)!r})"
    )
    proc = subprocess.Popen([sys.executable, "-c", script],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        # 等 A 进入阻塞的恢复执行（ready 文件出现）
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.1)
        assert ready.exists(), f"worker did not reach running: {ready.read_text() if ready.exists() else 'no file'}"
        saved = store.load_task(task.task_id)
        assert saved.phase == "running", f"expected running, got {saved.phase}"
        assert saved.resume_ref is not None

        # B 调 recover：owner（A）存活 → 不接管、不改 phase、不改 resume_ref
        cs = ContractStore(tmp_path / "tasks")
        vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")
        ctrl_b = LoopController(store, cs, vs, build_agent_fn=_ResumeRecordingAgent,
                                build_resume_agent_fn=lambda sid: _ResumeRecordingAgent())
        result = ctrl_b.recover(task.task_id)
        assert result.get("action") == "in_progress", f"got {result.get('action')}"
        saved2 = store.load_task(task.task_id)
        assert saved2.phase == "running", "B must not change phase"
        assert saved2.resume_ref is not None, "B must not clear resume_ref"

        # 放行 A：恢复执行完成，收尾不被干扰
        hold.unlink()
        proc.wait(timeout=30)
        worker_stderr = proc.stderr.read() if proc.stderr else ""
        assert proc.returncode == 0, f"worker rc={proc.returncode}, stderr: {worker_stderr}"
        attempts = store.list_attempts(task.task_id)
        assert attempts[-1].outcome == "completed", \
            f"A's completion must survive: {attempts[-1].outcome}"
    finally:
        if proc.poll() is None:
            proc.kill()
            hold.unlink(missing_ok=True)


def test_recover_after_claim_returns_in_progress(tmp_path):
    """resume_ready 已认领（owner=当前进程）后，再次 recover 不得重复接管。"""
    store = TaskStore(tmp_path)
    task = _make_resumable_task(store, tmp_path, valid=True)
    cs = ContractStore(tmp_path / "tasks")
    vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")
    ctrl = LoopController(store, cs, vs, build_agent_fn=_ResumeRecordingAgent,
                          build_resume_agent_fn=lambda sid: _ResumeRecordingAgent())
    r1 = ctrl.recover(task.task_id)
    assert r1.get("action") == "resume_ready", f"got {r1.get('action')}"
    r2 = ctrl.recover(task.task_id)
    assert r2.get("action") == "in_progress", f"got {r2.get('action')}"
    saved = store.load_task(task.task_id)
    assert saved.phase == "ready", "second recover must not flip phase"
    assert saved.resume_ref is not None


def test_resume_invalid_when_session_file_missing(tmp_path):
    """session_id 指向不存在的 session 文件 → checkpoint 无效 → 降级新 Run。"""
    store = TaskStore(tmp_path)
    task = _make_task(store, phase="run_interrupted", status="in_progress", cycle=1)
    task.contract_hash = "frozen"
    store.save_task(task)
    store.record_run_started(task.task_id, "run_ns", "legacy_ns")
    attempts = store.list_attempts(task.task_id)
    attempts[0].session_id = "missing_session"  # 没有对应 session 文件
    attempts[0].runtime_identity = {"model": "scripted", "tool_signature": "test-sig"}
    attempts[0].owner_pid = 0
    store._write_attempts_unlocked(task.task_id, attempts)

    cs = ContractStore(tmp_path / "tasks")
    vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")
    ctrl = LoopController(store, cs, vs, build_agent_fn=_ResumeRecordingAgent,
                          build_resume_agent_fn=lambda sid: _ResumeRecordingAgent())
    r = ctrl.recover(task.task_id)
    assert r.get("action") == "resumed", f"got {r.get('action')}"  # 降级：关闭旧 run
    saved = store.load_task(task.task_id)
    assert saved.resume_ref is None


def test_resume_missing_tool_signature_fails_closed(tmp_path):
    """历史 identity 缺 tool_signature → 无法验证工具集 → 降级新 Run。"""
    store = TaskStore(tmp_path)
    task = _make_task(store, phase="run_interrupted", status="in_progress", cycle=1)
    task.contract_hash = "frozen"
    store.save_task(task)
    sess_dir = tmp_path / ".forge" / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / "session_ts.json").write_text(
        json.dumps({"id": "session_ts", "history": [],
                    "checkpoints": {"current_id": "", "items": {}}}),
        encoding="utf-8",
    )
    store.record_run_started(task.task_id, "run_ts", "legacy_ts")
    attempts = store.list_attempts(task.task_id)
    attempts[0].session_id = "session_ts"
    attempts[0].runtime_identity = {"model": "scripted"}  # 缺 tool_signature
    attempts[0].owner_pid = 0
    store._write_attempts_unlocked(task.task_id, attempts)

    cs = ContractStore(tmp_path / "tasks")
    vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")
    ctrl = LoopController(store, cs, vs, build_agent_fn=_ResumeRecordingAgent,
                          build_resume_agent_fn=lambda sid: _ResumeRecordingAgent())
    r1 = ctrl.advance(task.task_id)
    assert r1.get("action") == "resume_ready", f"got {r1.get('action')}"
    ctrl.advance(task.task_id)  # _do_run：缺 tool_signature → fail closed 降级
    saved = store.load_task(task.task_id)
    assert saved.resume_ref is None
    assert saved.last_resume_attempt["reason"] == "resume_identity_mismatch"
    assert saved.cycle == 2


def test_recover_does_not_steal_live_run(tmp_path):
    """并发 recover() 不得接管 owner 进程仍存活的 Run（fail closed）。"""
    import subprocess
    import sys

    store = TaskStore(tmp_path)
    task = store.create_task("owned run")
    # 真子进程：record_run_started 后保持存活，模拟正在执行的 Run
    script = (
        "import sys, time;"
        "sys.path.insert(0, sys.argv[1]);"
        "from forge.features.task_record import TaskStore;"
        "TaskStore(sys.argv[2]).record_run_started(sys.argv[3], 'run_owned');"
        "time.sleep(2)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script,
         str(Path(__file__).resolve().parent.parent),  # forge 包根（Z:\forge）
         str(tmp_path), task.task_id],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # 等子进程落盘 attempt
        import time
        for _ in range(50):
            if store.recover_active_run(task.task_id) is not None:
                break
            time.sleep(0.1)

        cs = ContractStore(tmp_path / "tasks")
        vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")
        ctrl = LoopController(store, cs, vs, build_agent_fn=_ResumeRecordingAgent,
                              build_resume_agent_fn=lambda sid: _ResumeRecordingAgent())

        # 子进程还活着 → recover 不接管、不改 phase
        assert proc.poll() is None, "owner process should still be alive"
        result = ctrl.recover(task.task_id)
        assert result.get("action") == "in_progress", f"got {result.get('action')}"
        assert store.load_task(task.task_id).phase != "run_interrupted"
        assert store.recover_active_run(task.task_id) is not None

        # 子进程退出后 → 可接管（attempt 无 session → 降级关闭）
        # 用短轮询等待退出（环境对长阻塞等待会注入 KeyboardInterrupt）
        import time
        for _ in range(100):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        assert proc.poll() is not None, "owner process should have exited"
        result2 = ctrl.recover(task.task_id)
        assert result2.get("action") == "resumed", f"got {result2.get('action')}"
        attempts = store.list_attempts(task.task_id)
        assert attempts[0].outcome == "interrupted"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_resume_build_failure_degrades_to_new_run(tmp_path):
    """恢复 agent 构建失败（如 session 损坏）不得冒出：降级新 Run 并记录原因。"""
    store = TaskStore(tmp_path)
    task = _make_resumable_task(store, tmp_path, valid=True)
    cs = ContractStore(tmp_path / "tasks")
    vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")
    normal_agent = _ResumeRecordingAgent()

    def _build_resume_agent(session_id):
        raise RuntimeError("session file corrupted")

    ctrl = LoopController(store, cs, vs, build_agent_fn=lambda: normal_agent,
                          build_resume_agent_fn=_build_resume_agent)

    cycle_before = task.cycle
    r1 = ctrl.advance(task.task_id)
    assert r1.get("action") == "resume_ready", f"got {r1.get('action')}"
    ctrl.advance(task.task_id)  # _do_run：构建失败 → 降级
    saved = store.load_task(task.task_id)
    assert saved.cycle == cycle_before + 1, "build failure must start a new run"
    assert saved.resume_ref is None
    assert saved.last_resume_attempt is not None
    assert saved.last_resume_attempt["result"] == "degraded"
    assert "resume_build_failed" in saved.last_resume_attempt["reason"]
    # 新 Run 使用普通 agent 工厂，未注入原 run_id，任务可继续前进
    assert normal_agent.calls and normal_agent.calls[0]["run_id"] is None
    assert saved.phase == "verifying", f"task must not be stuck, got phase={saved.phase}"


def test_resume_missing_identity_fails_closed(tmp_path):
    """attempt 未记录 runtime_identity → 无法验证身份 → 降级新 Run。"""
    store = TaskStore(tmp_path)
    task = _make_task(store, phase="run_interrupted", status="in_progress", cycle=1)
    task.contract_hash = "frozen"
    store.save_task(task)
    sess_dir = tmp_path / ".forge" / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / "session_ni.json").write_text(
        json.dumps({"id": "session_ni", "history": [],
                    "checkpoints": {"current_id": "", "items": {}}}),
        encoding="utf-8",
    )
    store.record_run_started(task.task_id, "run_ni", "legacy_ni")
    attempts = store.list_attempts(task.task_id)
    attempts[0].session_id = "session_ni"
    attempts[0].owner_pid = 0  # 模拟崩溃遗留：owner 进程已死
    store._write_attempts_unlocked(task.task_id, attempts)

    cs = ContractStore(tmp_path / "tasks")
    vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")
    ctrl = LoopController(store, cs, vs, build_agent_fn=_ResumeRecordingAgent,
                          build_resume_agent_fn=lambda sid: _ResumeRecordingAgent())

    r1 = ctrl.advance(task.task_id)
    assert r1.get("action") == "resume_ready", f"got {r1.get('action')}"
    ctrl.advance(task.task_id)  # _do_run：身份缺失 → fail closed 降级
    saved = store.load_task(task.task_id)
    assert saved.cycle == 2, "missing identity must not resume"
    assert saved.resume_ref is None
    assert saved.last_resume_attempt["reason"] == "resume_identity_mismatch"


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


def test_interrupted_run_is_closed_before_new_cycle(tmp_path):
    """中断阶段必须清理 active attempt，避免下一轮被 active run 拒绝。"""
    store = TaskStore(tmp_path)
    task = _make_task(store, phase="run_interrupted", status="in_progress")
    store.record_run_started(task.task_id, "run_interrupted")
    attempts = store.list_attempts(task.task_id)
    attempts[0].owner_pid = 0  # 模拟崩溃遗留：owner 进程已死
    store._write_attempts_unlocked(task.task_id, attempts)
    task = store.load_task(task.task_id)
    task.last_run_summary = "worker crashed"
    store.save_task(task)

    cs = ContractStore(tmp_path / "tasks")
    vs = TaskVerificationService(store, cs, tmp_path, tmp_path / "tasks")
    result = LoopController(store, cs, vs).advance(task.task_id)

    saved = store.load_task(task.task_id)
    attempts = store.list_attempts(task.task_id)
    assert result["action"] == "resumed"
    assert saved.phase == "ready"
    assert saved.current_run_id == ""
    assert attempts[-1].outcome == "interrupted"
    assert attempts[-1].finished_at


def test_assess_uses_current_workspace_diff_fingerprint(tmp_path):
    """停滞比较应记录每轮真实 Diff，而不是永久复用 Cycle 0 基线指纹。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "calculator.py"
    source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    store = TaskStore(workspace / ".forge")
    cs = ContractStore(workspace / ".forge" / "tasks")
    vs = TaskVerificationService(store, cs, workspace, workspace / ".forge" / "tasks")
    task = _make_task(
        store,
        phase="assessing_progress",
        status="in_progress",
        baseline={
            "workspace_fingerprint": "baseline-fingerprint",
            "workspace_files": vs._workspace_file_manifest(),
            "failure_fingerprint": "same-failure",
        },
        last_verdict={"verdict": "fail"},
    )
    task.replan_already_done = True
    store.save_task(task)

    store.record_run_started(task.task_id, "run_1")
    store.record_run_finished(task.task_id, "run_1", tool_steps=1)
    first = LoopController(store, cs, vs).advance(task.task_id)
    assert first["action"] == "continue"
    first_fp = store.load_task(task.task_id).progress_history[-1]["diff_fingerprint"]
    assert first_fp != "baseline-fingerprint"

    saved = store.load_task(task.task_id)
    saved.phase = "assessing_progress"
    store.save_task(saved)
    source.write_text("def add(a, b):\n    return a * b\n", encoding="utf-8")
    store.record_run_started(task.task_id, "run_2")
    store.record_run_finished(task.task_id, "run_2", tool_steps=1)
    second = LoopController(store, cs, vs).advance(task.task_id)

    second_fp = second["task"].progress_history[-1]["diff_fingerprint"]
    assert second["action"] == "continue"
    assert second_fp != first_fp
