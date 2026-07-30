"""PR 3: Loop Controller — outer automation loop.

引进真正的外层自动循环：advance() 驱动 Task 状态机，
综合 Verifier 裁决、no-progress 检测和 Loop Policy 决定继续、停止或转人工。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


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

        # 2. failure_fingerprint 相同
        fingerprint_identical = (
            prev.get("failure_fingerprint")
            and curr.get("failure_fingerprint")
            and prev["failure_fingerprint"] == curr["failure_fingerprint"]
        )
        signals["fingerprint_identical"] = fingerprint_identical

        # 3. Diff 相同或空
        diff_stale = (
            (not prev.get("diff_fingerprint") and not curr.get("diff_fingerprint"))
            or (prev.get("diff_fingerprint") and prev["diff_fingerprint"] == curr.get("diff_fingerprint"))
        )
        signals["diff_stale"] = diff_stale

        # 4. 工具调用高度重复（粗略：调用次数不变或减少）
        tool_repeated = (
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
    ):
        self.task_store = task_store
        self.contract_store = contract_store
        self.verification_service = verification_service
        self.context_builder = context_builder or TaskContextBuilder()
        self._build_agent = build_agent_fn
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

        # 增加 cycle
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

        # 在 ask_run 之前先写入占位 current_run_id，确保崩溃后 Task 可定位
        placeholder_rid = f"pending_{_now()[:19].replace(':','-')}"
        task.current_run_id = placeholder_rid
        task.last_run_summary = "starting..."
        self.task_store.save_task(task)

        try:
            agent = self._build_agent()
            if policy.max_total_tool_steps > 0 and hasattr(agent, "max_steps"):
                remaining_steps = policy.max_total_tool_steps - self._total_tool_steps(task.task_id)
                agent.max_steps = min(agent.max_steps, max(0, remaining_steps))

            def _on_run_started(run_id, legacy_task_id):
                # 更新 task.json 中的 run_id 为真实值，并记录 attempt
                self.task_store.record_run_started(
                    task.task_id, run_id, legacy_engine_task_id=legacy_task_id
                )

            result = agent.ask_run(prompt, on_run_started=_on_run_started)
        except Exception as exc:
            # record_run_finished() 会更新 revision；完成后必须重新读取再写 phase。
            task = self.task_store.load_task(task.task_id)
            run_id = task.current_run_id
            if run_id in task.run_ids:
                try:
                    self.task_store.record_run_finished(
                        task.task_id, run_id, outcome="failed", summary=f"run_error: {exc}"
                    )
                except Exception as finish_exc:
                    task = self.task_store.load_task(task.task_id)
                    task.phase = "run_interrupted"
                    task.last_run_summary = f"run_finish_error: {finish_exc}"
                    self.task_store.save_task(task)
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
        self.task_store.record_run_finished(task.task_id, run_id, outcome=result.outcome,
                                             summary=(result.final_answer or "")[:200],
                                             tool_steps=result.metadata.get("tool_steps", 0),
                                             artifact_refs=artifact_refs)

        # 重载 task 避免 stale revision；Run 结束后再次检查预算，避免超额进入 verify。
        task = self.task_store.load_task(task.task_id)
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
        diff_fp = baseline.get("workspace_fingerprint", "")
        attempts = self.task_store.list_attempts(task.task_id)

        signal = {
            "verifier_passed": verifier_passed,
            "verifier_total": verifier_total,
            "failure_fingerprint": baseline.get("failure_fingerprint", ""),
            "diff_fingerprint": diff_fp or _now(),
            "tool_call_count": sum(getattr(attempt, "tool_steps", 0) for attempt in attempts),
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

    def _do_interrupted(self, task, policy) -> dict:
        # 简化处理：直接回到 ready
        task.phase = "ready"
        self.task_store.save_task(task)
        return {"action": "resumed", "reason": "run_interrupted → ready", "task": task}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
