"""PR 3: Loop Controller — outer automation loop.

引进真正的外层自动循环：advance() 驱动 Task 状态机，
综合 Verifier 裁决、no-progress 检测和 Loop Policy 决定继续、停止或转人工。
"""

from __future__ import annotations

import enum
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..paths import workspace_state_path
from ..core.process import pid_alive, process_started_at
from ..core.run_store import RunStore
from ..core.session_store import SessionStore


# ---------------------------------------------------------------------------
# 失败分类
# ---------------------------------------------------------------------------

class FailureCategory(str, enum.Enum):
    """失败类型，为每种类型定义不同策略。"""
    TEST_FAILURE = "test_failure"
    CHANGE_SCOPE_VIOLATION = "change_scope_violation"
    ENVIRONMENT_ERROR = "environment_error"
    PROVIDER_ERROR = "provider_error"
    TOOL_ERROR = "tool_error"
    PERMISSION_DENIED = "permission_denied"
    BUDGET_EXCEEDED = "budget_exceeded"
    FLAKY_VERIFICATION = "flaky_verification"
    HUMAN_DECISION_REQUIRED = "human_decision_required"
    STRONG_STAGNATION = "strong_stagnation"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Loop Policy
# ---------------------------------------------------------------------------

@dataclass
class LoopPolicy:
    """定义循环的预算、停止条件和升级策略。"""
    max_cycles: int = 4
    max_total_tool_steps: int = 80
    max_wall_time_seconds: int = 1800
    require_replan_before_waiting_human: bool = True

    @classmethod
    def default(cls) -> LoopPolicy:
        return cls()

    def to_dict(self) -> dict:
        return {
            "max_cycles": self.max_cycles,
            "max_total_tool_steps": self.max_total_tool_steps,
            "max_wall_time_seconds": self.max_wall_time_seconds,
            "require_replan_before_waiting_human": self.require_replan_before_waiting_human,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> LoopPolicy:
        if not data:
            return cls.default()
        return cls(
            max_cycles=int(data.get("max_cycles", 4)),
            max_total_tool_steps=int(data.get("max_total_tool_steps", 80)),
            max_wall_time_seconds=int(data.get("max_wall_time_seconds", 1800)),
            require_replan_before_waiting_human=bool(data.get("require_replan_before_waiting_human", True)),
        )


# ---------------------------------------------------------------------------
# NoProgressDetector —— 确定性停滞检测
# ---------------------------------------------------------------------------

@dataclass
class StagnationResult:
    """停滞检测结果。"""
    is_stagnant: bool = False
    reason: str = ""
    signals: dict = field(default_factory=dict)


class NoProgressDetector:
    """基于确定性信号的停滞检测，不依赖 LLM Judge。

    使用 Verifier 结果、failure_fingerprint、Diff 和工具重复度
    四个信号做合取判断。
    """

    def __init__(self, history: list[dict] | None = None):
        self._history = [dict(item) for item in (history or []) if isinstance(item, dict)][-5:]

    @property
    def history(self) -> list[dict]:
        """返回可持久化的最近检测信号。"""
        return [dict(item) for item in self._history]

    def record_cycle(self, cycle_data: dict) -> None:
        """记录一轮的检测信号。"""
        self._history.append(dict(cycle_data))
        # 只保留最近 5 轮
        if len(self._history) > 5:
            self._history.pop(0)

    def assess(self, latest: dict) -> StagnationResult:
        """判断当前是否处于停滞。

        latest 包含：
          verifier_passed: int
          verifier_total: int
          failure_fingerprint: str
          diff_fingerprint: str
          tool_call_count: int
        """
        self.record_cycle(latest)

        if len(self._history) < 2:
            return StagnationResult(is_stagnant=False, reason="insufficient_history")

        prev = self._history[-2]
        curr = latest

        signals = {}

        # 1. Verifier 结果完全相同
        verifier_identical = (
            prev.get("verifier_passed") == curr.get("verifier_passed")
            and prev.get("verifier_total") == curr.get("verifier_total")
        )
        signals["verifier_identical"] = verifier_identical

        # 2. failure_fingerprint 相同（bool 化：空字符串短路时 and 会返回 str）
        fingerprint_identical = bool(
            prev.get("failure_fingerprint")
            and curr.get("failure_fingerprint")
            and prev["failure_fingerprint"] == curr["failure_fingerprint"]
        )
        signals["fingerprint_identical"] = fingerprint_identical

        # 3. Diff 相同或空
        diff_stale = bool(
            (not prev.get("diff_fingerprint") and not curr.get("diff_fingerprint"))
            or (prev.get("diff_fingerprint") and prev["diff_fingerprint"] == curr.get("diff_fingerprint"))
        )
        signals["diff_stale"] = diff_stale

        # 4. 工具调用高度重复（粗略：调用次数不变或减少）
        tool_repeated = bool(
            prev.get("tool_call_count", 0)
            and curr.get("tool_call_count", 0) <= prev.get("tool_call_count", 0)
        )
        signals["tool_repeated"] = tool_repeated

        # 结果进展改善
        outcome_improved = (
            curr.get("verifier_passed", 0) > prev.get("verifier_passed", 0)
            or (curr.get("verifier_total", 0) > 0
                and curr.get("verifier_passed", 0) == curr.get("verifier_total", 0))
        )
        signals["outcome_improved"] = outcome_improved

        # 强停滞：4 个信号同时成立且结果未改善
        strong_stagnation = (
            verifier_identical
            and fingerprint_identical
            and diff_stale
            and tool_repeated
            and not outcome_improved
        )
        signals["strong_stagnation"] = strong_stagnation

        if strong_stagnation:
            return StagnationResult(
                is_stagnant=True,
                reason="strong_stagnation",
                signals=signals,
            )

        # 疑似停滞：部分信号成立
        suspected = sum([verifier_identical, fingerprint_identical, diff_stale, tool_repeated])
        if suspected >= 3 and not outcome_improved:
            return StagnationResult(
                is_stagnant=True,
                reason="suspected_stagnation",
                signals=signals,
            )

        if outcome_improved:
            return StagnationResult(
                is_stagnant=False,
                reason="outcome_improved",
                signals=signals,
            )

        return StagnationResult(is_stagnant=False, reason="no_clear_signal", signals=signals)


# ---------------------------------------------------------------------------
# FailureClassifier
# ---------------------------------------------------------------------------

class FailureClassifier:
    """将 Run 或 Verifier 的结果分类为 FailureCategory。"""

    @staticmethod
    def classify(
        stop_reason: str = "",
        last_verdict: dict | None = None,
        run_outcome: str = "",
        last_summary: str = "",
    ) -> FailureCategory:
        last_verdict = last_verdict or {}
        lv = str(last_verdict.get("verdict", "") or "")

        if lv == "infra_error":
            return FailureCategory.ENVIRONMENT_ERROR
        if lv == "flaky":
            return FailureCategory.FLAKY_VERIFICATION
        if lv == "blocked":
            return FailureCategory.ENVIRONMENT_ERROR
        if run_outcome == "stopped":
            return FailureCategory.BUDGET_EXCEEDED
        if lv == "forbidden_path_modified":
            return FailureCategory.CHANGE_SCOPE_VIOLATION
        if lv == "fail":
            return FailureCategory.TEST_FAILURE
        if stop_reason == "approval_denied":
            return FailureCategory.PERMISSION_DENIED
        if stop_reason in ("model_error", "provider_error"):
            return FailureCategory.PROVIDER_ERROR
        if stop_reason == "tool_timeout":
            return FailureCategory.TOOL_ERROR
        return FailureCategory.UNKNOWN


# ---------------------------------------------------------------------------
# EscalationArtifact
# ---------------------------------------------------------------------------

@dataclass
class EscalationArtifact:
    """当 Loop 无法继续时，生成结构化的人工升级信息。"""
    task_id: str = ""
    reason: str = ""
    decision_needed: str = ""
    completed: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)
    recommended_option: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    created_at: str = ""

    def to_text(self) -> str:
        lines = [
            f"task_id:           {self.task_id}",
            f"reason:            {self.reason}",
            f"decision_needed:   {self.decision_needed}",
            "",
            "completed:",
        ]
        for item in self.completed:
            lines.append(f"  - {item}")
        if self.options:
            lines.extend(["", "options:"])
            for opt in self.options:
                lines.append(f"  - {opt}")
        if self.recommended_option:
            lines.extend(["", f"recommended: {self.recommended_option}"])
        if self.evidence_refs:
            lines.extend(["", "evidence:"])
            for r in self.evidence_refs:
                lines.append(f"  - {r}")
        return "\n".join(lines)

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_text(), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# CycleContext —— 注入到 Agent Prompt 的结构化上下文
# ---------------------------------------------------------------------------

@dataclass
class CycleContext:
    """由 TaskContextBuilder 生成，供 Harness 组装 prompt 时使用。

    只注入目标、当前阶段、未满足验收项、最新失败证据等。
    完整历史保存在 artifact 中通过引用访问。
    """
    goal: str = ""
    acceptance_summary: str = ""
    current_phase: str = ""
    current_cycle: int = 0
    max_cycles: int = 4
    previous_attempts: list[dict] = field(default_factory=list)
    latest_verifier_failure: str = ""
    forbidden_actions: list[str] = field(default_factory=list)
    requested_outcome: str = ""

    def to_prompt_text(self) -> str:
        """生成给 Agent 的任务简报文本。"""
        parts = [f"任务目标：{self.goal}"]

        if self.acceptance_summary:
            parts.append(f"验收标准：{self.acceptance_summary}")
        parts.append(f"当前阶段：{self.current_phase}，Cycle {self.current_cycle}/{self.max_cycles}")

        if self.previous_attempts:
            parts.append("\n前轮结果：")
            for att in self.previous_attempts[-3:]:
                seq = att.get("sequence", "?")
                summary = att.get("summary", "")[:80]
                parts.append(f"  Cycle {seq}: {summary}")

        if self.latest_verifier_failure:
            parts.append(f"\n最新验证失败：\n{self.latest_verifier_failure[:200]}")

        if self.forbidden_actions:
            parts.append(f"\n禁止操作：{', '.join(self.forbidden_actions)}")

        parts.append(f"\n本轮要求：{self.requested_outcome}")

        return "\n".join(parts)


# ---------------------------------------------------------------------------
# TaskContextBuilder
# ---------------------------------------------------------------------------

class TaskContextBuilder:
    """从 TaskStore 读取任务状态，生成 CycleContext。

    TaskContextBuilder 不直接拼 prompt，输出给现有 ContextManager 使用。
    """

    def build(self, task, contract=None, attempts=None) -> CycleContext:
        """从 Task 记录构建 CycleContext。"""
        ctx = CycleContext(
            goal=getattr(task, "goal", "") or "",
            acceptance_summary=self._acceptance_summary(task, contract),
            current_phase=getattr(task, "phase", "") or "ready",
            current_cycle=getattr(task, "cycle", 0) or 0,
            max_cycles=getattr(task, "max_cycles", 4) or 4,
            previous_attempts=[],
            latest_verifier_failure=self._latest_failure(task),
            forbidden_actions=self._forbidden_actions(task, contract),
            requested_outcome=self._requested_outcome(task),
        )

        if attempts:
            ctx.previous_attempts = [
                {"sequence": a.sequence, "summary": a.summary, "outcome": a.outcome}
                for a in attempts[-5:]
            ]

        return ctx

    def _acceptance_summary(self, task, contract) -> str:
        if contract:
            n_verify = len(getattr(contract, "verify_commands", []) or [])
            reproduce = getattr(contract, "reproduce_command", "") or ""
            return f"{n_verify} verify commands" + (f", reproduce: {reproduce}" if reproduce else "")
        if task.baseline:
            return f"baseline: {task.baseline.get('reproduction_verdict', '?')}"
        return ""

    def _latest_failure(self, task) -> str:
        lv = getattr(task, "last_verdict", None) or {}
        verdict = str(lv.get("verdict", "") or "")
        if verdict in ("fail", "forbidden_path_modified"):
            refs = lv.get("evidence_refs", [])
            if refs:
                return f"Verifier {verdict}, evidence: {refs[-1]}"
            return f"Verifier {verdict}"
        return ""

    def _forbidden_actions(self, task, contract) -> list[str]:
        if contract and hasattr(contract, "change_policy"):
            return list(getattr(contract.change_policy, "forbidden_paths", []) or [])
        return []

    def _requested_outcome(self, task) -> str:
        phase = getattr(task, "phase", "") or ""
        if phase == "replanning":
            return "重新规划策略，分析前几轮失败原因，提出新的修复方案"
        if phase == "verifying":
            return "本轮 Agent 工作结束，提交外部验证"
        return "完成当前阶段工作后进入验证"


# ---------------------------------------------------------------------------
# LoopController —— 外循环状态机
# ---------------------------------------------------------------------------

class LoopController:
    """综合 Task 历史、Verifier 裁决和 NoProgress 检测，
    决定继续、完成、失败、阻塞或转人工。
    """

    def __init__(
        self,
        task_store,
        contract_store,
        verification_service,
        context_builder: TaskContextBuilder | None = None,
        build_agent_fn: Callable | None = None,
        build_resume_agent_fn: Callable | None = None,
    ):
        self.task_store = task_store
        self.contract_store = contract_store
        self.verification_service = verification_service
        self.context_builder = context_builder or TaskContextBuilder()
        self._build_agent = build_agent_fn
        # PR 3b：按 session_id 恢复同一 Run 的 agent 工厂。
        # 未提供时，RUN_INTERRUPTED 采用安全降级（关闭旧 Run 开新 Run）。
        self._build_resume_agent = build_resume_agent_fn
        self.detector = NoProgressDetector()

    def advance(self, task_id: str, run_prompt: str | None = None) -> dict:
        """最多推进一个受控阶段。返回包含本次推进结果的 dict。"""
        task = self.task_store.load_task(task_id)
        phase = getattr(task, "phase", "") or ""
        status = getattr(task, "status", "") or ""

        # 终端状态
        if status in ("completed", "failed", "blocked", "waiting_human"):
            return {"action": "stopped", "reason": f"task is {status}", "task": task}

        policy = LoopPolicy.from_dict(getattr(task, "policy", None))

        # — Policy 检查 —
        # 最后一轮 Agent Run 仍应完成 verify；仅在准备启动下一轮时限制 cycle。
        if phase == "ready" and task.cycle >= policy.max_cycles:
            task.status = "blocked"
            task.phase = ""
            task.completion_reason = "max_cycles_reached"
            self.task_store.save_task(task)
            return {"action": "blocked", "reason": "max_cycles_reached", "task": task}

        budget_limit = self._budget_limit_reached(task, policy)
        if budget_limit:
            return self._block_for_budget(task, *budget_limit)

        # ── 状态机 ──
        if not phase or phase == "baselining":
            return self._do_baseline(task, policy)
        elif phase == "ready":
            return self._do_run(task, policy, run_prompt)
        elif phase == "running":
            return {"action": "in_progress", "reason": "run already in progress", "task": task}
        elif phase == "verifying":
            return self._do_verify(task, policy)
        elif phase == "assessing_progress":
            return self._do_assess(task, policy)
        elif phase == "replanning":
            return self._do_replan(task, policy)
        elif phase == "run_interrupted":
            return self._do_interrupted(task, policy)
        else:
            # 没有 phase → 初始状态
            task.phase = "baselining"
            self.task_store.save_task(task)
            return self._do_baseline(task, policy)

    def advance_full(self, task_id: str, run_prompt: str | None = None) -> dict:
        """重复调用 advance() 直到进入停止状态。"""
        last = {}
        while True:
            result = self.advance(task_id, run_prompt)
            last = result
            action = result.get("action", "")
            if action in ("completed", "stopped", "blocked", "waiting_human", "error"):
                return result
            run_prompt = None
        return last

    # ── 内部阶段推进 ──

    def _total_tool_steps(self, task_id: str) -> int:
        return sum(getattr(attempt, "tool_steps", 0) for attempt in self.task_store.list_attempts(task_id))

    def _budget_limit_reached(self, task, policy) -> tuple[str, str] | None:
        if policy.max_total_tool_steps > 0 and self._total_tool_steps(task.task_id) >= policy.max_total_tool_steps:
            return "max_total_tool_steps_reached", "max_total_tool_steps"
        if policy.max_wall_time_seconds <= 0:
            return None
        starts = [attempt.started_at for attempt in self.task_store.list_attempts(task.task_id) if attempt.started_at]
        if not starts:
            return None
        try:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(min(starts))).total_seconds()
        except (ValueError, TypeError):
            return None
        if elapsed >= policy.max_wall_time_seconds:
            return "max_wall_time_exceeded", "max_wall_time"
        return None

    def _block_for_budget(self, task, completion_reason: str, reason: str) -> dict:
        task.status = "blocked"
        task.phase = ""
        task.completion_reason = completion_reason
        self.task_store.save_task(task)
        return {"action": "blocked", "reason": reason, "task": task}

    def _do_baseline(self, task, policy) -> dict:
        contract_hash = getattr(task, "contract_hash", "") or ""
        if not contract_hash:
            # 没有冻结的 contract_hash → 无法验证合约完整性
            task.status = "blocked"
            task.phase = ""
            task.completion_reason = "contract_not_frozen"
            self.task_store.save_task(task)
            return {"action": "blocked", "reason": "contract_not_frozen", "task": task}

        task.phase = "baselining"
        self.task_store.save_task(task)
        from .verification import ReproductionVerdict
        try:
            contract_hash = getattr(task, "contract_hash", "") or None
            verdict, info = self.verification_service.establish_baseline(
                task.task_id, expected_contract_hash=contract_hash
            )
        except Exception as exc:
            task.status = "blocked"
            task.phase = ""
            task.completion_reason = f"baseline_infra_error: {exc}"
            self.task_store.save_task(task)
            return {"action": "blocked", "reason": str(exc), "task": task}

        task.baseline = {
            "commit": info.commit,
            "workspace_fingerprint": info.workspace_fingerprint,
            "reproduction_verdict": info.reproduction_verdict,
            "failure_fingerprint": info.failure_fingerprint,
            "evidence_ref": info.evidence_ref,
            "workspace_files": info.workspace_files,
        }

        if verdict == ReproductionVerdict.REPRODUCED:
            task.phase = "ready"
            self.task_store.save_task(task)
            return {"action": "baseline_ready", "reason": "reproduced", "task": task}

        if verdict == ReproductionVerdict.NOT_REPRODUCED:
            # 执行最终验证，显式携带冻结的 Contract 和基线快照。
            v_verdict, v_info = self.verification_service.run_verification(
                task.task_id,
                expected_contract_hash=contract_hash,
                baseline_files=dict(info.workspace_files),
            )
            task.last_verdict = {"verdict": v_verdict.value, "checked_at": _now(),
                                 "evidence_refs": list(v_info.evidence_refs)}
            if v_verdict.value == "pass":
                task.status = "completed"
                task.phase = ""
                task.completion_reason = "already_resolved"
            else:
                task.status = "blocked"
                task.phase = ""
                task.completion_reason = (
                    "baseline_mismatch" if v_verdict.value == "fail"
                    else f"baseline_verify_{v_verdict.value}"
                )
            self.task_store.save_task(task)
            return {"action": "completed" if v_verdict.value == "pass" else "blocked",
                    "reason": task.completion_reason, "task": task}

        # INFRA_ERROR / FLAKY / AMBIGUOUS
        rv = verdict.value if hasattr(verdict, "value") else str(verdict)
        if rv in ("infra_error", "flaky"):
            task.status = "blocked"
            task.phase = ""
            task.completion_reason = f"baseline_{rv}"
        else:
            task.status = "waiting_human"
            task.phase = ""
            task.completion_reason = f"baseline_{rv}"
        self.task_store.save_task(task)
        return {"action": task.status, "reason": task.completion_reason, "task": task}

    def _do_run(self, task, policy, run_prompt=None) -> dict:
        if self._build_agent is None:
            task.status = "blocked"
            task.phase = ""
            task.completion_reason = "no_agent_factory"
            self.task_store.save_task(task)
            return {"action": "blocked", "reason": "no agent factory", "task": task}

        # PR 3b：checkpoint 恢复路径 — 不增加 cycle、注入原 run_id、复用同一 session
        resume_ref = getattr(task, "resume_ref", None)
        is_resume = bool(resume_ref)
        resume_run_id = resume_ref.get("run_id", "") if resume_ref else ""

        # 恢复 agent 构建 + 实时身份校验：model / tool 签名不一致 → 降级为新 Run。
        # 构建失败（session 文件被删 / JSON 损坏 / provider 配置失败）不允许冒出：
        # 必须落到底层降级路径，否则 task 会反复卡在 resume_ready 且无法解释。
        agent = None
        resume_degrade_reason = ""
        if is_resume and self._build_resume_agent is not None:
            try:
                resume_agent = self._build_resume_agent(resume_ref.get("session_id", ""))
            except Exception as exc:
                resume_agent = None
                resume_degrade_reason = f"resume_build_failed:{type(exc).__name__}"
            if (resume_agent is not None
                    and self._resume_identity_ok(task.task_id, resume_run_id, resume_agent)):
                agent = resume_agent
            else:
                resume_degrade_reason = resume_degrade_reason or "resume_identity_mismatch"
            if resume_degrade_reason:
                old_run_id = resume_run_id
                is_resume = False
                task.resume_ref = None
                resume_ref = None
                resume_run_id = ""
                task.last_resume_attempt = {
                    "attempted_at": _now(),
                    "result": "degraded",
                    "reason": resume_degrade_reason,
                }
                # 降级 = 关闭旧 attempt（resume_ready 保留它处于 active），
                # 否则新 Run 会被 record_run_started 的 active-run 检查拒绝。
                try:
                    task = self.task_store.record_run_interrupted(
                        task.task_id, old_run_id,
                        summary=f"resume_degraded: {resume_degrade_reason}"[:200],
                    )
                    task.resume_ref = None
                    task.last_resume_attempt = {
                        "attempted_at": _now(),
                        "result": "degraded",
                        "reason": resume_degrade_reason,
                    }
                except Exception as exc:
                    # 清理失败不能静默：旧 attempt 仍 active，新 Run 无法启动，
                    # 必须落到底层可观察状态（waiting_human）而不是假装 ready。
                    task = self.task_store.load_task(task.task_id)
                    task.status = "waiting_human"
                    task.phase = ""
                    task.completion_reason = "resume_degrade_cleanup_failed"
                    task.last_run_summary = f"resume_degrade_cleanup_failed: {exc}"
                    task.last_resume_attempt = {
                        "attempted_at": _now(),
                        "result": "degraded_cleanup_failed",
                        "reason": f"{resume_degrade_reason}; cleanup error: {exc}",
                    }
                    self.task_store.save_task(task)
                    return {"action": "waiting_human",
                            "reason": f"resume degrade cleanup failed: {exc}", "task": task}

        if not is_resume:
            task.cycle += 1
        task.phase = "running"
        task.status = "in_progress"
        self.task_store.save_task(task)

        prompt = run_prompt or task.goal

        # 使用 TaskContextBuilder 构建结构化的 cycle 上下文
        try:
            contract = self.contract_store.load_contract(task.task_id)
            attempts = self.task_store.list_attempts(task.task_id)
            ctx = self.context_builder.build(task, contract=contract, attempts=attempts)
            cycle_prompt = ctx.to_prompt_text()
            if run_prompt:
                cycle_prompt += f"\n\n用户附加指令：{run_prompt}"
            prompt = cycle_prompt
        except Exception:
            prompt = run_prompt or task.goal  # fallback

        # 在 ask_run 之前先写入占位 current_run_id，确保崩溃后 Task 可定位。
        # 降级原因（resume_build_failed / identity_mismatch）保留在 summary 中可观察。
        placeholder_rid = f"pending_{_now()[:19].replace(':','-')}"
        task.current_run_id = placeholder_rid
        if resume_degrade_reason:
            task.last_run_summary = f"starting... (resume_degraded: {resume_degrade_reason})"
        else:
            task.last_run_summary = "starting..."
        self.task_store.save_task(task)

        try:
            if agent is None:
                agent = self._build_agent()
            if policy.max_total_tool_steps > 0 and hasattr(agent, "max_steps"):
                remaining_steps = policy.max_total_tool_steps - self._total_tool_steps(task.task_id)
                agent.max_steps = min(agent.max_steps, max(0, remaining_steps))

            def _on_run_started(run_id, legacy_task_id):
                # 更新 task.json 中的 run_id 为真实值，并记录 attempt
                self.task_store.record_run_started(
                    task.task_id, run_id, legacy_engine_task_id=legacy_task_id
                )
                # 运行中即落盘 session / runtime identity：
                # 进程崩溃后 attempt 仍携带恢复引用，可走真实 resume 路径。
                # 记录失败不静默：把原因写入 summary；恢复端因 identity 缺失
                # 会 fail-closed 降级，不会用未经验证的身份恢复。
                try:
                    sid = str(getattr(agent, "session", {}).get("id", ""))
                    identity = (agent.current_runtime_identity()
                                if hasattr(agent, "current_runtime_identity") else None)
                    self.task_store.update_attempt(
                        task.task_id, run_id, session_id=sid, runtime_identity=identity
                    )
                except Exception as exc:
                    try:
                        t = self.task_store.load_task(task.task_id)
                        t.last_run_summary = f"identity_record_failed: {exc}"
                        self.task_store.save_task(t)
                    except Exception:
                        pass

            # checkpoint 恢复时注入原 run_id，保持同一 Run 身份
            ask_kwargs = {}
            if is_resume and resume_run_id:
                ask_kwargs["run_id"] = resume_run_id
            result = agent.ask_run(prompt, on_run_started=_on_run_started, **ask_kwargs)
        except Exception as exc:
            # record_run_finished() 会更新 revision；完成后必须重新读取再写 phase。
            task = self.task_store.load_task(task.task_id)
            run_id = task.current_run_id
            if run_id in task.run_ids:
                try:
                    # 异常收尾也要持 token（与正常收尾一致），否则新 Run 的
                    # fencing 校验会误把异常路径导到 run_interrupted。
                    exc_token = ""
                    try:
                        exc_attempt = next(
                            (a for a in self.task_store.list_attempts(task.task_id)
                             if a.run_id == run_id),
                            None,
                        )
                        if exc_attempt is not None:
                            exc_token = str(getattr(exc_attempt, "owner_token", "") or "")
                    except Exception:
                        pass
                    self.task_store.record_run_finished(
                        task.task_id, run_id, outcome="failed",
                        summary=f"run_error: {exc}", owner_token=exc_token,
                    )
                except Exception as finish_exc:
                    task = self.task_store.load_task(task.task_id)
                    task.phase = "run_interrupted"
                    task.last_run_summary = f"run_finish_error: {finish_exc}"
                    self.task_store.save_task(task)
                    # 同进程内已确认 Run 死亡：清除 owner_pid，
                    # 避免后续 recover() 因“owner 进程仍存活”而拒绝接管。
                    try:
                        self.task_store.update_attempt(task.task_id, run_id, owner_pid=0)
                    except Exception:
                        pass
                    return {"action": "error", "reason": str(finish_exc), "task": task}
                task = self.task_store.load_task(task.task_id)
            else:
                # ask_run 尚未分配真实 run_id 时，清除仅用于崩溃诊断的占位值。
                task.current_run_id = ""
            task.phase = "ready"
            task.last_run_summary = f"run_error: {exc}"
            self.task_store.save_task(task)
            return {"action": "error", "reason": str(exc), "task": task}

        task.last_run_summary = (result.final_answer or "")[:200]
        artifact_refs = []
        ts = getattr(agent, "current_task_state", None)
        if ts:
            rd = getattr(agent.run_store, "run_dir", None)
            if rd:
                run_dir = rd(ts)
                for p in [run_dir / "report.json", run_dir / "trace.jsonl"]:
                    if p.exists():
                        artifact_refs.append(str(p))

        run_id = result.run_id or self.task_store.load_task(task.task_id).current_run_id

        # PR 3a：持久化恢复引用（session / checkpoint / runtime identity）
        session_id = str(getattr(agent, "session", {}).get("id", ""))
        checkpoint_ref = ""
        try:
            ckpt = agent.current_checkpoint()
            if ckpt:
                checkpoint_ref = str(ckpt.get("checkpoint_id", ""))
        except Exception:
            pass
        runtime_identity = {}
        try:
            runtime_identity = agent.current_runtime_identity()
        except Exception:
            pass

        # 中断前已消耗的 tool_steps 结转进本次 Run，避免恢复后绕过总预算。
        carry_steps = 0
        owner_token = ""
        if is_resume:
            try:
                interrupted = next(
                    (a for a in self.task_store.list_attempts(task.task_id) if a.run_id == run_id),
                    None,
                )
                if interrupted is not None:
                    carry_steps = int(getattr(interrupted, "tool_steps", 0) or 0)
                    owner_token = str(getattr(interrupted, "owner_token", "") or "")
            except Exception:
                carry_steps = 0

        # fencing token：收尾写入必须持有当前认领者的 token。
        # 新 Run 场景 token 由 record_run_started 生成；resume 场景由 claim 签发。
        if not owner_token:
            try:
                attempt = next(
                    (a for a in self.task_store.list_attempts(task.task_id) if a.run_id == run_id),
                    None,
                )
                if attempt is not None:
                    owner_token = str(getattr(attempt, "owner_token", "") or "")
            except Exception:
                pass

        self.task_store.record_run_finished(task.task_id, run_id, outcome=result.outcome,
                                             summary=(result.final_answer or "")[:200],
                                             tool_steps=carry_steps + result.metadata.get("tool_steps", 0),
                                             artifact_refs=artifact_refs,
                                             session_id=session_id,
                                             checkpoint_ref=checkpoint_ref,
                                             runtime_identity=runtime_identity,
                                             owner_token=owner_token)

        # 重载 task 避免 stale revision；Run 结束后再次检查预算，避免超额进入 verify。
        task = self.task_store.load_task(task.task_id)
        # resume 完成后清除 resume_ref，避免下次 advance 重复恢复
        if is_resume and getattr(task, "resume_ref", None):
            task.resume_ref = None
        if task.status == "failed":
            return {"action": "blocked", "reason": task.completion_reason or "run_failed", "task": task}
        budget_limit = self._budget_limit_reached(task, policy)
        if budget_limit:
            return self._block_for_budget(task, *budget_limit)
        task.phase = "verifying"
        self.task_store.save_task(task)
        return {"action": "run_completed", "run_id": result.run_id, "task": task}

    def _do_verify(self, task, policy) -> dict:
        contract_hash = getattr(task, "contract_hash", "") or None
        baseline_files = None
        if task.baseline and "workspace_files" in task.baseline:
            baseline_files = dict(task.baseline["workspace_files"])
        attempts = self.task_store.list_attempts(task.task_id)
        run_id = attempts[-1].run_id if attempts else ""
        try:
            verdict, info = self.verification_service.run_verification(
                task.task_id,
                run_id=run_id,
                expected_contract_hash=contract_hash,
                baseline_files=baseline_files,
            )
        except Exception as exc:
            task.phase = "ready"
            task.last_run_summary = f"verify_error: {exc}"
            self.task_store.save_task(task)
            return {"action": "error", "reason": str(exc), "task": task}

        task.last_verdict = {"verdict": verdict.value, "checked_at": _now(),
                             "evidence_refs": list(info.evidence_refs)}

        if verdict.value == "pass":
            task.status = "completed"
            task.phase = ""
            task.completion_reason = "verifier_pass"
            self.task_store.save_task(task)
            return {"action": "completed", "reason": "verifier_pass", "task": task}

        if verdict.value in ("infra_error", "flaky"):
            task.status = "blocked"
            task.phase = ""
            task.completion_reason = f"verify_{verdict.value}"
            self.task_store.save_task(task)
            return {"action": "blocked", "reason": task.completion_reason, "task": task}

        # FAIL → 使用实际 Attempt outcome 分类，不能把 run_id 当成 outcome。
        run_outcome = attempts[-1].outcome if attempts else ""
        cat = FailureClassifier.classify(
            last_verdict={"verdict": verdict.value}, run_outcome=run_outcome
        )
        task.last_run_summary = f"failure: {cat.value}"

        task.phase = "assessing_progress"
        self.task_store.save_task(task)
        return {"action": "assessing", "reason": f"verifier_fail: {cat.value}", "task": task}

    def _do_assess(self, task, policy) -> dict:
        # 收集检测信号
        lv = getattr(task, "last_verdict", None) or {}
        verifier_passed = 0 if lv.get("verdict") == "fail" else 1
        verifier_total = 1
        baseline = getattr(task, "baseline", None) or {}
        baseline_files = baseline.get("workspace_files", {})
        diff_fp = ""
        if isinstance(baseline_files, dict):
            # 每个 Cycle 都对当前工作区生成指纹，才能识别空 Diff、撤销改动和
            # 高度重复的改动；基线指纹只代表 Cycle 0，不能作为当前 Diff 信号。
            current_manifest = self.verification_service._workspace_file_manifest()
            changed_files = {
                path: current_manifest.get(path, "")
                for path in set(baseline_files) | set(current_manifest)
                if baseline_files.get(path) != current_manifest.get(path)
            }
            diff_fp = self.verification_service._manifest_fingerprint(changed_files)
        attempts = self.task_store.list_attempts(task.task_id)

        # 停滞检测比较的是相邻 Cycle 的工具行为，不能使用累计总数；
        # 否则每轮都会比前一轮大，重复的相同尝试永远无法被识别为停滞。
        signal = {
            "verifier_passed": verifier_passed,
            "verifier_total": verifier_total,
            "failure_fingerprint": baseline.get("failure_fingerprint", ""),
            "diff_fingerprint": diff_fp,
            "tool_call_count": getattr(attempts[-1], "tool_steps", 0) if attempts else 0,
        }

        # 从 Task 恢复历史并在本轮检测后立即持久化，支持 CLI 进程重启。
        self.detector = NoProgressDetector(getattr(task, "progress_history", []))
        stagnation = self.detector.assess(signal)
        task.progress_history = self.detector.history

        if not stagnation.is_stagnant:
            task.phase = "ready"
            self.task_store.save_task(task)
            return {"action": "continue", "reason": stagnation.reason, "task": task}

        if stagnation.reason == "strong_stagnation":
            # 检查是否已经 replan 过
            if task.replan_already_done or not policy.require_replan_before_waiting_human:
                # 生成升级 artifact
                esc = EscalationArtifact(
                    task_id=task.task_id,
                    reason="strong_stagnation",
                    decision_needed="Agent 多次尝试后无法改善验证结果，需要人工判断",
                    completed=[f"Cycle {task.cycle}: {task.last_run_summary[:80]}"],
                    options=["终止任务", "检查需求是否清晰", "扩大修改范围"],
                    recommended_option="终止任务",
                    evidence_refs=list((getattr(task, "last_verdict", {}) or {}).get("evidence_refs", [])),
                )
                esc_path = self.task_store._tasks_root / (task.task_id.replace("/", "_")) / "escalation.txt"
                esc.save(esc_path)
                task.status = "waiting_human"
                task.phase = ""
                task.completion_reason = "strong_stagnation"
                self.task_store.save_task(task)
                return {"action": "waiting_human", "reason": "strong_stagnation", "task": task}
            else:
                task.phase = "replanning"
                self.task_store.save_task(task)
                return {"action": "replanning", "reason": "strong_stagnation_first", "task": task}

        if stagnation.reason == "suspected_stagnation":
            if not task.replan_already_done:
                task.phase = "replanning"
                self.task_store.save_task(task)
                return {"action": "replanning", "reason": "suspected_stagnation", "task": task}

        task.phase = "ready"
        self.task_store.save_task(task)
        return {"action": "continue", "reason": stagnation.reason, "task": task}

    def _do_replan(self, task, policy) -> dict:
        # 标记已做过 replan（持久化到 task.json）
        task.replan_already_done = True
        task.phase = "ready"
        task.last_run_summary = "REPLAN: strategy reset"
        self.task_store.save_task(task)
        return {"action": "replanned", "reason": "ready after replan", "task": task}

    def recover(self, task_id: str, force: bool = False) -> dict:
        """显式恢复：进程崩溃遗留的 running 任务 → 检测 active run → resume 或降级。

        ``advance()`` 在 phase == "running" 时保持 in_progress（避免与正在运行
        的其他进程竞争误判）；进程重启后由本方法（CLI: ``forge loop recover``）
        显式进入 RUN_INTERRUPTED 恢复路径。

        Run 所有权保护：active attempt 记录了 owner_pid。若该 PID 仍存活
        （另一进程正在正常执行），本方法 fail closed——不接管、不改 phase；
        确认进程已死后才恢复。``force=True`` 可跳过存活检查（CLI ``--force``）。
        """
        task = self.task_store.load_task(task_id)
        phase = getattr(task, "phase", "") or ""
        if phase == "run_interrupted":
            return self._do_interrupted(
                task, LoopPolicy.from_dict(getattr(task, "policy", None)), force=force
            )
        interrupted = self.task_store.recover_active_run(task_id)
        if interrupted is None:
            return {"action": "nothing", "reason": "no active run to recover", "task": task}
        owner_pid = int(getattr(interrupted, "owner_pid", 0) or 0)
        if (not force and owner_pid
                and pid_alive(owner_pid, getattr(interrupted, "owner_started_at", ""))):
            return {
                "action": "in_progress",
                "reason": f"run still owned by live process (pid {owner_pid}); "
                          f"use --force only after confirming it is dead",
                "task": task,
            }
        task.phase = "run_interrupted"
        task.last_run_summary = (task.last_run_summary or "interrupted")[:200]
        self.task_store.save_task(task)
        return self._do_interrupted(
            task, LoopPolicy.from_dict(getattr(task, "policy", None)), force=force
        )

    def _checkpoint_valid(self, task, attempt) -> tuple[bool, str]:
        """校验中断 Run 的 checkpoint 是否可恢复。

        有效条件（roadmap PR 3 验收条款）：
        1. 存在未结束的 attempt；
        2. 有 session_id —— 会话历史是恢复现场的核心：恢复 agent 加载同一
           session 后，模型能看到已完成的历史，不必重做已完成的工具动作；
        3. 有 checkpoint_ref 时，须能在对应 session 中找到该 checkpoint。

        说明：不比较工作区指纹。Run 运行期间工作区必然变化（Run 自身的
        修改），保存时与恢复时的指纹不同不等于外部污染；模型与工具签名的
        一致性在 ``_do_run`` 构建恢复 agent 后实时校验。
        """
        if attempt is None:
            return False, "no_interrupted_attempt"
        if not attempt.session_id:
            return False, "missing_session_id"
        # 无论是否有 checkpoint_ref，session 文件本身必须真实存在且 id 一致
        if self._session_load(attempt.session_id) is None:
            return False, f"missing_session:{attempt.session_id}"
        if attempt.checkpoint_ref and not self._session_checkpoint_exists(
            attempt.session_id, attempt.checkpoint_ref
        ):
            return False, f"missing_checkpoint:{attempt.checkpoint_ref}"
        return True, "valid"

    def _session_load(self, session_id: str) -> dict | None:
        """加载 session 文件；不存在 / 损坏 / id 不一致（含空 id）返回 None。"""
        try:
            ws_root = self.verification_service.workspace_root
            store = SessionStore(workspace_state_path(ws_root, "sessions"))
            session = store.load(session_id)
            if not isinstance(session, dict):
                return None
            if session.get("id") != session_id:
                return None
            return session
        except Exception:
            return None

    def _session_checkpoint_exists(self, session_id: str, checkpoint_ref: str) -> bool:
        """检查 session 文件中是否确实存在该 checkpoint。"""
        session = self._session_load(session_id)
        if session is None:
            return False
        items = ((session.get("checkpoints") or {}).get("items") or {})
        return checkpoint_ref in items

    def _recover_run_artifacts(self, run_id: str) -> dict:
        """崩溃后从 run_store 恢复中断时刻的 tool_steps 与 checkpoint_id。

        engine 每轮都会原子写 ``.forge/runs/<run_id>/task_state.json``，
        其中记录了崩溃前最后已知的工具步数与 checkpoint 位置。
        """
        try:
            ws_root = self.verification_service.workspace_root
            path = RunStore(workspace_state_path(ws_root, "runs")).task_state_path(run_id)
            data = json.loads(path.read_text(encoding="utf-8"))
            return {
                "tool_steps": int(data.get("tool_steps", 0) or 0),
                "checkpoint_ref": str(data.get("checkpoint_id", "") or ""),
            }
        except Exception:
            return {}

    def _do_interrupted(self, task, policy, force: bool = False) -> dict:
        """RUN_INTERRUPTED：优先恢复同一 Run，仅降级时关闭旧 Run。

        PR 3 验收条款：
        - 有效 checkpoint：保持同一 task_id / cycle / run_id，从 checkpoint 继续；
        - 无效 checkpoint：记录失效原因，关闭旧 Run，启动新 Run。

        所有权保护：active attempt 的 owner_pid 仍存活时视为 Run 仍在执行，
        fail closed 不接管（force=True 跳过，仅限确认进程已死后使用）。
        """
        interrupted = self.task_store.recover_active_run(task.task_id)

        if interrupted is not None:
            owner_pid = int(getattr(interrupted, "owner_pid", 0) or 0)
            if (not force and owner_pid
                    and pid_alive(owner_pid, getattr(interrupted, "owner_started_at", ""))):
                return {
                    "action": "in_progress",
                    "reason": f"run still owned by live process (pid {owner_pid})",
                    "task": task,
                }
            # 从 run_store 恢复崩溃前的 tool_steps / checkpoint_ref，并落盘到 attempt，
            # 使预算累计不因中断丢失、checkpoint 校验可用。
            recovered = self._recover_run_artifacts(interrupted.run_id)
            if recovered:
                try:
                    self.task_store.update_attempt(
                        task.task_id, interrupted.run_id, **recovered
                    )
                    interrupted = self.task_store.recover_active_run(task.task_id)
                except Exception:
                    pass
            valid, reason = self._checkpoint_valid(task, interrupted)
            if valid and self._build_resume_agent is not None:
                # 原子认领：把 owner 更新为当前恢复进程，并签发新的 fencing token。
                # 这是 recover → resume 的所有权协议——认领后其他 recover()
                # 会看到 owner 存活而返回 in_progress，不会再接管同一 Run；
                # 旧执行者的延迟写入也会因 token 不匹配而被拒绝。
                try:
                    claimed = self.task_store.claim_active_run(
                        task.task_id,
                        interrupted.run_id,
                        owner_pid=os.getpid(),
                        owner_started_at=process_started_at(os.getpid()),
                    )
                except Exception as exc:
                    # 认领失败（另一活进程持有）→ fail closed
                    task.last_resume_attempt = {
                        "attempted_at": _now(),
                        "result": "claim_failed",
                        "reason": f"claim_active_run: {exc}",
                    }
                    self.task_store.save_task(task)
                    return {"action": "in_progress",
                            "reason": f"claim failed: {exc}", "task": task}
                interrupted = self.task_store.recover_active_run(task.task_id)
                # 恢复路径：保留 attempt，标记 resume 信息，回到 ready 由 _do_run 续跑
                task.resume_ref = {
                    "run_id": interrupted.run_id,
                    "session_id": interrupted.session_id,
                    "checkpoint_ref": interrupted.checkpoint_ref,
                    "cycle": getattr(task, "cycle", 0),
                    "owner_token": str(getattr(claimed, "owner_token", "") or ""),
                }
                task.last_resume_attempt = {
                    "attempted_at": _now(),
                    "result": "resume_ready",
                    "reason": f"valid checkpoint {interrupted.checkpoint_ref or interrupted.run_id}",
                }
                task.phase = "ready"
                task.last_run_summary = f"resume from checkpoint {interrupted.checkpoint_ref or interrupted.run_id}"
                self.task_store.save_task(task)
                return {"action": "resume_ready",
                        "reason": f"valid checkpoint {interrupted.checkpoint_ref or interrupted.run_id}", "task": task}
            # 无效或无恢复工厂 → 安全降级：关闭旧 attempt
            try:
                task = self.task_store.record_run_interrupted(
                    task.task_id,
                    interrupted.run_id,
                    summary=f"checkpoint invalid ({reason}): {task.last_run_summary or 'interrupted'}"[:200],
                    tool_steps=int(recovered.get("tool_steps", 0) or 0),
                    checkpoint_ref=str(recovered.get("checkpoint_ref", "") or ""),
                )
            except Exception as exc:
                task = self.task_store.load_task(task.task_id)
                task.status = "waiting_human"
                task.phase = ""
                task.completion_reason = "interrupted_run_unrecoverable"
                task.last_run_summary = f"interrupted_run_cleanup_error: {exc}"
                self.task_store.save_task(task)
                return {"action": "waiting_human", "reason": str(exc), "task": task}

        task.phase = "ready"
        self.task_store.save_task(task)
        return {"action": "resumed", "reason": "interrupted run closed; starting new run", "task": task}

    def _resume_identity_ok(self, task_id: str, run_id: str, agent) -> bool:
        """恢复 agent 的 model / tool 签名须与中断 attempt 一致，否则降级新 Run。

        fail-closed：恢复是风险动作，任何无法验证的情况（当前身份获取失败、
        历史身份读取失败、attempt 未记录身份）一律不恢复，降级为新 Run。
        """
        try:
            current = agent.current_runtime_identity()
        except Exception:
            return False  # 当前身份无法获取 → 不恢复
        try:
            attempts = self.task_store.list_attempts(task_id)
            attempt = next((a for a in attempts if a.run_id == run_id), None)
        except Exception:
            return False  # 历史身份无法读取 → 不恢复
        saved = (attempt.runtime_identity or {}) if attempt else {}
        if not saved:
            return False  # 未记录身份 → 无法验证 → 不恢复
        for key in ("model", "tool_signature"):
            # 缺任一字段都无法验证工具集/模型一致性 → fail closed 降级
            if saved.get(key) is None or current.get(key) is None:
                return False
            if saved[key] != current[key]:
                return False
        return True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
