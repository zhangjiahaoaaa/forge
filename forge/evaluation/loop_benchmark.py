"""PR 4: Loop Benchmark — A/B 对照运行器与指标采集。

对照设计：
  对照组：单次 Harness Run（使用与 Loop 相同的 verify 判定标准）
  实验组：LoopController.advance() 最多 4 个 Cycle

Control 与 Loop 走相同的 reproduce + verify 链路，
唯一的区别是 orchestration 方式（普通执行 vs LoopController）。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..core.runtime import Pico
from ..core.workspace import WorkspaceContext
from ..features.loop import LoopController, LoopPolicy, TaskContextBuilder
from ..features.task_record import TaskRecord, TaskStatus, TaskStore
from ..features.verification import (
    AcceptanceContract,
    ChangePolicy,
    ContractStore,
    ReproduceExpectation,
    TaskVerificationService,
    VerifyCommand,
    VerifierVerdict,
)
from ..testing import ScriptedModelClient


# ---------------------------------------------------------------------------
# 任务定义
# ---------------------------------------------------------------------------

@dataclass
class LoopBenchTask:
    """一个 Loop benchmark 任务。"""
    id: str
    goal: str
    fixture_repo: str
    category: str = "loop"
    reproduce_command: str = ""
    reproduce_exit_code: int | None = None
    reproduce_output_contains: list[str] = field(default_factory=list)
    verify_commands: list[dict] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=lambda: ["*"])
    forbidden_paths: list[str] = field(default_factory=list)
    max_cycles: int = 4
    failure_outcome: str = "blocked"
    expected_outcome: str = "completed"
    # A/B 预算：与 control 的单次 Run（max_steps=10）对齐的循环组总工具步数。
    # 仅“停滞 → REPLAN → 再停滞 → 转人工”这类必须消耗多个 Cycle 才能展示
    # 升级语义的任务放宽（见 loop_stagnation），其余任务严格等于 control。
    budget_tool_steps: int = 10
    # 是否纳入“严格对称 A/B”汇总。预算/编排偏离 control 的任务必须标 False，
    # 它们只能作为 diagnostic 报告，不得混入 strict_ab 的 success rate。
    comparable: bool = True

    def to_contract(self) -> AcceptanceContract:
        exp = None
        if self.reproduce_command:
            exp = ReproduceExpectation(
                outcome="target_failure",
                exit_code=self.reproduce_exit_code,
                output_contains=list(self.reproduce_output_contains),
            )
        return AcceptanceContract(
            goal=self.goal,
            reproduce_command=self.reproduce_command,
            reproduce=exp,
            reproduce_timeout=30,
            verify_commands=[
                VerifyCommand(command=str(v["command"]), timeout_seconds=int(v.get("timeout", 30)))
                for v in (self.verify_commands or [])
            ],
            change_policy=ChangePolicy(
                allowed_paths=list(self.allowed_paths),
                forbidden_paths=list(self.forbidden_paths),
            ),
        )


# ---------------------------------------------------------------------------
# 8 类 benchmark 任务
# ---------------------------------------------------------------------------

BENCH_TASKS: list[LoopBenchTask] = [
    # 1. 首轮可修复
    LoopBenchTask(
        id="loop_first_try_pass",
        goal="修复 calculator.add 的符号错误：将减法改为加法",
        fixture_repo="bench_repo_bugfix_py",
        reproduce_command="python -m pytest -p no:asyncio test_calculator.py::test_add -q",
        reproduce_exit_code=1,
        verify_commands=[{
            "command": "python -m pytest -p no:asyncio test_calculator.py -q",
            "timeout": 30,
        }],
        allowed_paths=["calculator.py"],
        expected_outcome="completed",
    ),
    # 2. 多轮修复 — 第一轮修成乘法（仍失败），第二轮修成加法（通过）
    #   真实 fixture 中 calculator.py 只有 add 一个 bug（subtract 已是正确实现）
    LoopBenchTask(
        id="loop_multi_cycle_fix",
        goal="修复 calculator.add：先改成乘法（仍失败），再改成加法（通过）",
        fixture_repo="bench_repo_bugfix_py",
        reproduce_command="python -m pytest -p no:asyncio test_calculator.py::test_add -q",
        reproduce_exit_code=1,
        verify_commands=[{
            "command": "python -m pytest -p no:asyncio test_calculator.py::test_add -q",
            "timeout": 30,
        }],
        allowed_paths=["calculator.py"],
        expected_outcome="completed",
    ),
    # 3. 已解决（初始仓库已正确）
    LoopBenchTask(
        id="loop_already_resolved",
        goal="修复 calculator.add 的错误",
        fixture_repo="bench_repo_bugfix_py",
        reproduce_command='python -c "exit(0)"',
        reproduce_exit_code=1,  # 预期失败但实际成功 → NOT_REPRODUCED
        verify_commands=[{"command": 'python -c "exit(0)"', "timeout": 10}],
        expected_outcome="completed",
    ),
    # 4. 无法复现（reproduce 不匹配）
    LoopBenchTask(
        id="loop_cannot_reproduce",
        goal="修复一个描述不匹配的 bug（目标行为无法复现）",
        fixture_repo="bench_repo_bugfix_py",
        reproduce_command='python -c "exit(0)"',
        reproduce_exit_code=1,  # 预期失败但 exit(0) → NOT_REPRODUCED
        verify_commands=[{"command": 'python -c "exit(1)"', "timeout": 10}],
        expected_outcome="blocked",
    ),
    # 5. 基础设施错误
    LoopBenchTask(
        id="loop_infra_error",
        goal="修复 bug",
        fixture_repo="bench_repo_bugfix_py",
        reproduce_command="nonexistent_command_xyz",
        expected_outcome="blocked",
    ),
    # 6. 修改受保护路径
    LoopBenchTask(
        id="loop_forbidden_path",
        goal="修改验证脚本让测试通过",
        fixture_repo="bench_repo_bugfix_py",
        reproduce_command="python -m pytest -p no:asyncio test_calculator.py::test_add -q",
        reproduce_exit_code=1,
        verify_commands=[{"command": "python -m pytest -p no:asyncio test_calculator.py -q", "timeout": 30}],
        forbidden_paths=["test_calculator.py"],
        expected_outcome="blocked",
    ),
    # 7. 停滞：必须消耗多个 Cycle 才能展示“停滞 → REPLAN → 仍停滞 → 转人工”的
    #    升级语义，因此预算放宽到 control 的 3 倍（其余任务与 control 严格对齐）。
    #    comparable=False：预算已偏离 control，只能作为 diagnostic 报告，
    #    不得纳入严格对称 A/B 的 success rate 汇总。
    LoopBenchTask(
        id="loop_stagnation",
        goal="修复 bug（模型无法修复，循环重试后停滞）",
        fixture_repo="bench_repo_bugfix_py",
        reproduce_command="python -m pytest -p no:asyncio test_calculator.py::test_add -q",
        reproduce_exit_code=1,
        verify_commands=[{"command": 'python -c "exit(1)"', "timeout": 10}],
        failure_outcome="waiting_human",
        expected_outcome="waiting_human",
        budget_tool_steps=30,
        comparable=False,
    ),
    # 8. 中断恢复：真实 crash → resume 同一 Run（见 _run_loop_resume）
    LoopBenchTask(
        id="loop_resume",
        goal="修复 calculator.add 的符号错误",
        fixture_repo="bench_repo_bugfix_py",
        reproduce_command="python -m pytest -p no:asyncio test_calculator.py::test_add -q",
        reproduce_exit_code=1,
        verify_commands=[{
            "command": "python -m pytest -p no:asyncio test_calculator.py -q",
            "timeout": 30,
        }],
        allowed_paths=["calculator.py"],
        expected_outcome="completed",
    ),
]


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------

class LoopBenchmarkRunner:
    """Loop benchmark 运行器，同时运行对照组和实验组。

    Control 与 Loop 共享相同的 reproduce + verify 判定逻辑，
    唯一的差异是 orchestration 方式。
    """

    def __init__(self, fixtures_root: Path, workspace_root: Path):
        self.fixtures_root = Path(fixtures_root).resolve()
        self.workspace_root = Path(workspace_root).resolve()

    def run_all(self) -> list[dict]:
        results: list[dict] = []
        for task_def in BENCH_TASKS:
            fixture_dir = self.fixtures_root / task_def.fixture_repo
            if not fixture_dir.exists():
                results.append({"task_id": task_def.id, "error": f"fixture not found: {fixture_dir}"})
                continue
            # control 与 loop 使用独立 workspace，避免 A/B 污染
            ctrl_ws = self._setup_workspace(fixture_dir)
            loop_ws = self._setup_workspace(fixture_dir)
            results.append(self._run_control(task_def, ctrl_ws, fixture_dir))
            results.append(self._run_loop(task_def, loop_ws, fixture_dir))
        return results

    # -- 工作区管理 ---------------------------------------------------------

    def _setup_workspace(self, fixture_dir: Path) -> Path:
        """创建独立、可追溯的工作区，且不复制 fixture 的 Git 元数据。"""
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        ws = self.workspace_root / f"bench_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        shutil.copytree(fixture_dir, ws, ignore=shutil.ignore_patterns(".git"))
        _init_git(ws)
        return ws

    def _build_task(self, workspace: Path, task_def: LoopBenchTask):
        store = TaskStore(workspace / ".forge")
        task = store.create_task(task_def.goal)
        contract = task_def.to_contract()
        cs = ContractStore(workspace / ".forge" / "tasks")
        cs.save_contract(task.task_id, contract, allow_replace=True)
        task.contract_hash = contract.content_hash
        task.max_cycles = task_def.max_cycles
        task.policy = LoopPolicy(
            max_cycles=task_def.max_cycles,
            max_total_tool_steps=task_def.budget_tool_steps,
        ).to_dict()
        store.save_task(task)
        vs = TaskVerificationService(store, cs, workspace, workspace / ".forge" / "tasks")
        return store, cs, vs, task, contract

    # -- 通用判定逻辑 -------------------------------------------------------

    def _independent_recheck(self, task_def: LoopBenchTask, agent_workspace: Path,
                             fixture_dir: Path, contract: AcceptanceContract) -> bool:
        """独立复核：在干净的 verifier workspace 应用 Agent 的全部修改后重跑验收。

        false_completion 的唯一权威依据：系统宣布 completed，
        但独立复核 Verifier 在冻结 Contract 下不 PASS。
        """
        recheck_ws = self._setup_workspace(fixture_dir)
        # 在干净 workspace 中重建 Task + 冻结 Contract，并执行验收
        store2 = TaskStore(recheck_ws / ".forge")
        cs2 = ContractStore(recheck_ws / ".forge" / "tasks")
        task2 = store2.create_task(task_def.goal)
        cs2.save_contract(task2.task_id, contract, allow_replace=True)
        task2.contract_hash = contract.content_hash
        store2.save_task(task2)
        vs2 = TaskVerificationService(store2, cs2, recheck_ws, recheck_ws / ".forge" / "tasks")
        # 复制 Agent 修改前，先以干净 fixture 快照作为内容基线（manifest 忽略 .forge/.git），
        # 使复核只比较真实业务文件变更，不受 runtime 状态文件影响。
        baseline_files = vs2._workspace_file_manifest()
        # 镜像同步 agent 的文件树（完整 patch application 模型）：
        # 1) 删除 agent 已删除的 baseline 业务文件；2) 复制 agent 新增/修改文件。
        agent_files: dict[str, Path] = {}
        for f in agent_workspace.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(agent_workspace)
            if ".forge" in rel.parts or ".git" in rel.parts:
                continue
            agent_files[rel.as_posix()] = f
        for path in baseline_files:
            if path not in agent_files:
                dst = recheck_ws / path
                if dst.exists():
                    dst.unlink()
        for rel, f in agent_files.items():
            dst = recheck_ws / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
        v_verdict, _ = vs2.run_verification(
            task2.task_id,
            expected_contract_hash=contract.content_hash,
            baseline_files=baseline_files,
        )
        return v_verdict == VerifierVerdict.PASS

    # -- Control: 单次 Harness Run -----------------------------------------

    def _run_control(self, task_def: LoopBenchTask, workspace: Path, fixture_dir: Path) -> dict:
        start = time.monotonic()
        store, cs, vs, task, contract = self._build_task(workspace, task_def)

        # 1. reproduce
        repro_verdict, baseline = vs.establish_baseline(
            task.task_id, expected_contract_hash=task.contract_hash
        )
        reproduce_result = repro_verdict.value if hasattr(repro_verdict, "value") else str(repro_verdict)

        if reproduce_result in {"infra_error", "flaky"}:
            elapsed = time.monotonic() - start
            return self._control_record(task_def, "blocked", reproduce_result, False, False, elapsed,
                                        workspace_root=str(workspace))
        if reproduce_result == "ambiguous":
            elapsed = time.monotonic() - start
            return self._control_record(task_def, "waiting_human", reproduce_result, False, True, elapsed,
                                        workspace_root=str(workspace))

        # 与 LoopController 一致：目标未复现时只做最终验证，不能再启动修复 Agent。
        agent = None
        if reproduce_result == "reproduced":
            agent = _build_scripted_agent(
                workspace, ScriptedModelClient(_scripted_outputs(task_def))
            )
            agent.ask(task_def.goal)

        control_tool_steps = 0
        if agent is not None:
            ts = getattr(agent, "current_task_state", None)
            control_tool_steps = int(getattr(ts, "tool_steps", 0) or 0) if ts else 0

        # 验证始终以 baseline manifest 为比较依据，忽略 .forge 运行时状态文件。
        v_verdict, v_info = vs.run_verification(
            task.task_id,
            expected_contract_hash=task.contract_hash,
            baseline_files=dict(baseline.workspace_files),
        )
        elapsed = time.monotonic() - start
        verification_pass = v_verdict == VerifierVerdict.PASS
        evidence_refs = list(getattr(v_info, "evidence_refs", []) or [])
        baseline_evidence = (task.baseline or {}).get("evidence_ref", "")
        if baseline_evidence:
            evidence_refs.append(baseline_evidence)
        run_artifacts = _run_artifact_refs(agent, workspace)

        if verification_pass:
            # 独立复核：在干净 verifier workspace 重跑冻结 Contract 的验收
            recheck = self._independent_recheck(task_def, workspace, fixture_dir, contract)
            if not recheck:
                # 系统验证 PASS 但独立复核 FAIL → false completion
                return self._control_record(task_def, "completed", reproduce_result,
                                            True, False, elapsed, false_completion=True,
                                            contract_hash=contract.content_hash,
                                            run_artifacts=run_artifacts,
                                            evidence_refs=evidence_refs,
                                            tool_steps=control_tool_steps,
                                            independent_recheck_pass=False,
                                            workspace_root=str(workspace))
            return self._control_record(task_def, "completed", reproduce_result, True, False, elapsed,
                                        contract_hash=contract.content_hash,
                                        run_artifacts=run_artifacts,
                                        evidence_refs=evidence_refs,
                                        tool_steps=control_tool_steps,
                                        independent_recheck_pass=True,
                                        workspace_root=str(workspace))
        escalated = task_def.failure_outcome == "waiting_human"
        return self._control_record(
            task_def, task_def.failure_outcome, reproduce_result, False, escalated, elapsed,
            contract_hash=contract.content_hash,
            run_artifacts=run_artifacts,
            evidence_refs=evidence_refs,
            tool_steps=control_tool_steps,
            workspace_root=str(workspace),
        )

    def _control_record(self, task_def, outcome, reproduce_result, task_success,
                        escalated, elapsed, false_completion=False,
                        contract_hash="", run_artifacts=None, evidence_refs=None,
                        tool_steps=0, independent_recheck_pass=None,
                        workspace_root="", run_ids=None):
        return {
            "task_id": task_def.id,
            "variant": "control",
            "expected_outcome": task_def.expected_outcome,
            "outcome": outcome,
            "reproduce_result": reproduce_result,
            "verification_pass": task_success,
            "task_success": task_success,
            "false_completion": false_completion or (
                task_success and task_def.expected_outcome not in ("completed", "")
            ),
            "escalated": escalated,
            "no_progress": False,
            "resume_success": None,
            "cycles_to_success": 1 if task_success else 0,
            "tool_steps": tool_steps,
            "tokens": None,  # 无 usage 数据来源，按 roadmap 显示 unavailable
            "elapsed_seconds": round(elapsed, 2),
            "contract_hash": contract_hash,
            "run_artifacts": list(run_artifacts or []),
            "verifier_evidence_refs": list(evidence_refs or []),
            "independent_recheck_pass": independent_recheck_pass,
            "comparable": bool(task_def.comparable),
            "workspace_root": workspace_root,
            "run_ids": list(run_ids or []),
        }

    # -- Loop: LoopController -------------------------------------------------

    def _run_loop(self, task_def: LoopBenchTask, workspace: Path, fixture_dir: Path) -> dict:
        store, cs, vs, task, contract = self._build_task(workspace, task_def)

        # loop_resume 使用真实的崩溃 + 恢复编排（子进程 crash → recover 同一 Run）
        if task_def.id == "loop_resume":
            return self._run_loop_resume(task_def, workspace, fixture_dir,
                                         store, cs, vs, task, contract)

        # 同一任务的多个 Cycle 必须消费同一个脚本序列；每轮新建 Client 会让
        # 第二轮从第一条输出重新开始，掩盖真实的多轮策略行为。
        client = ScriptedModelClient(_scripted_outputs(task_def))

        def _build_agent():
            return _build_scripted_agent(workspace, client)

        ctrl = LoopController(
            task_store=store, contract_store=cs, verification_service=vs,
            context_builder=TaskContextBuilder(), build_agent_fn=_build_agent,
        )

        start = time.monotonic()
        result = ctrl.advance_full(task.task_id)
        elapsed = time.monotonic() - start

        task_obj = result.get("task") or store.load_task(task.task_id)

        # 独立复核：系统 completed 时，在干净 verifier workspace 重跑冻结 Contract
        recheck = None
        if getattr(task_obj, "status", None) in (TaskStatus.COMPLETED, "completed"):
            recheck = self._independent_recheck(task_def, workspace, fixture_dir, contract)

        metrics = self._collect_metrics(
            task_def, task_obj, result, elapsed, store,
            run_artifacts=(_attempt_artifact_refs(task_obj, store, workspace)
                           + _run_dir_artifact_refs(workspace, getattr(task_obj, "run_ids", []))),
            evidence_refs=_task_evidence_refs(task_obj),
            independent_recheck_pass=(recheck if recheck is not None else None),
        )
        if recheck is not None:
            metrics["false_completion"] = not recheck
        return metrics

    def _run_loop_resume(self, task_def: LoopBenchTask, workspace: Path, fixture_dir: Path,
                         store, cs, vs, task, contract) -> dict:
        """真实 resume 编排：子进程执行首次 Run 并“崩溃”，主进程 recover 同一 Run。

        阶段 1：spawn 子进程消费 crash 段输出（read → patch 错误修复 → SystemExit），
        attempt 停留在 active 状态（record_run_started 已落盘、run 未结束）；
        阶段 2：主进程用第二个 Controller 调用 recover() → 检测 active run →
        checkpoint 有效 → 以同一 run_id / cycle 续跑剩余序列 → 完成并验证。
        """
        from multiprocessing import get_context

        start = time.monotonic()
        ctx = get_context("spawn")
        proc = ctx.Process(
            target=_resume_crash_worker,
            args=(str(workspace), task.task_id),
        )
        proc.start()
        proc.join(240)
        if proc.is_alive():
            proc.terminate()
            proc.join(10)
        original_run_id = store.load_task(task.task_id).current_run_id
        cycle_before = store.load_task(task.task_id).cycle

        # 阶段 2：恢复 Controller（resume agent 加载同一 session，续接同一脚本序列）
        client = ScriptedModelClient(_resume_continue_outputs())

        def _build_agent():
            return _build_scripted_agent(workspace, client)

        def _build_resume_agent(session_id):
            return _build_resume_scripted_agent(workspace, client, session_id)

        ctrl_b = LoopController(
            task_store=store, contract_store=cs, verification_service=vs,
            context_builder=TaskContextBuilder(), build_agent_fn=_build_agent,
            build_resume_agent_fn=_build_resume_agent,
        )

        rec = ctrl_b.recover(task.task_id)
        if rec.get("action") == "resume_ready":
            result = ctrl_b.advance_full(task.task_id)
        else:
            result = rec
        elapsed = time.monotonic() - start

        task_obj = result.get("task") or store.load_task(task.task_id)

        # 独立复核：系统 completed 时，在干净 verifier workspace 重跑冻结 Contract
        recheck = None
        if getattr(task_obj, "status", None) in (TaskStatus.COMPLETED, "completed"):
            recheck = self._independent_recheck(task_def, workspace, fixture_dir, contract)

        # resume 判定：恢复路径被采用，且最终 run_id / cycle 与中断前一致
        attempts = store.list_attempts(task.task_id)
        attempt = attempts[-1] if attempts else None
        same_run = bool(attempt and original_run_id and attempt.run_id == original_run_id)
        same_cycle = getattr(task_obj, "cycle", 0) == cycle_before
        resume_success = bool(rec.get("action") == "resume_ready" and same_run and same_cycle)

        metrics = self._collect_metrics(
            task_def, task_obj, result, elapsed, store,
            resume_success=resume_success,
            run_artifacts=(_attempt_artifact_refs(task_obj, store, workspace)
                           + _run_dir_artifact_refs(workspace, getattr(task_obj, "run_ids", []))),
            evidence_refs=_task_evidence_refs(task_obj),
            independent_recheck_pass=(recheck if recheck is not None else None),
        )
        if recheck is not None:
            metrics["false_completion"] = not recheck
        return metrics

    def _collect_metrics(self, task_def: LoopBenchTask, task: TaskRecord,
                         result: dict, elapsed: float, store: TaskStore | None = None,
                         resume_success: bool | None = None,
                         run_artifacts: list | None = None,
                         evidence_refs: list | None = None,
                         independent_recheck_pass: bool | None = None) -> dict:
        action = result.get("action", "")
        status = getattr(task, "status", "")
        if isinstance(status, TaskStatus):
            status = status.value

        task_success = status == "completed"
        final_action = action
        final_status = str(status)
        false_completion = task_success and task_def.expected_outcome not in ("completed", "")
        escalated = status == "waiting_human"
        no_progress = (getattr(task, "completion_reason", "") or "") in {
            "strong_stagnation", "max_cycles_reached",
        }

        if task_success:
            outcome = "completed"
        elif action == "blocked" or status == "blocked":
            outcome = "blocked"
        elif escalated:
            outcome = "waiting_human"
        else:
            outcome = "fail"

        # 工具步数：从该 Task 全部 attempt 汇总（AttemptRecord.tool_steps）
        tool_steps = 0
        attempts = []
        if store is not None:
            try:
                attempts = store.list_attempts(task.task_id)
            except Exception:
                attempts = []
        if attempts:
            tool_steps = sum(getattr(a, "tool_steps", 0) or 0 for a in attempts)

        return {
            "task_id": task_def.id,
            "variant": "loop",
            "expected_outcome": task_def.expected_outcome,
            "outcome": outcome,
            "reproduce_result": (task.baseline or {}).get("reproduction_verdict", ""),
            "verification_pass": task_success,
            "task_success": task_success,
            "false_completion": false_completion,
            "escalated": escalated,
            "no_progress": no_progress,
            "resume_success": resume_success,
            "cycles_to_success": getattr(task, "cycle", 0) if task_success else 0,
            "tool_steps": tool_steps,
            "tokens": None,  # 无 usage 数据来源，按 roadmap 显示 unavailable
            "final_status": final_status,
            "final_action": final_action,
            "elapsed_seconds": round(elapsed, 2),
            "contract_hash": (getattr(task, "contract_hash", "") or ""),
            "run_artifacts": list(run_artifacts or []),
            "verifier_evidence_refs": list(evidence_refs or []),
            "independent_recheck_pass": independent_recheck_pass,
            "comparable": bool(task_def.comparable),
            "workspace_root": str(Path(store.root).parent) if store is not None else "",
            "run_ids": list(getattr(task, "run_ids", []) or []),
        }


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _json_tool(name: str, args: dict | None = None) -> str:
    """生成符合模型协议的工具调用文本：<tool>{json}</tool>"""
    return f"<tool>{json.dumps({'name': name, 'args': args or {}})}</tool>"


def _init_git(path: Path):
    for cmd in [
        ["git", "init"],
        ["git", "config", "user.email", "bench@forge.dev"],
        ["git", "config", "user.name", "B"],
        ["git", "add", "-A"],
        ["git", "commit", "-m", "init", "--allow-empty"],
    ]:
        try:
            subprocess.run(cmd, cwd=path, capture_output=True, timeout=10)
        except Exception:
            pass


def _scripted_outputs(task_def: LoopBenchTask) -> list[str]:
    """为每个任务生成确定性的模型输出序列。

    Control 与 Loop 消费**完全相同**的序列（A/B 唯一变量是 orchestration：
    control 只跑一次 Harness Run，loop 可跨 Cycle 继续消费同一序列）。
    ScriptedModelClient 按顺序消费，control 在第一个 <final> 处停止，
    loop 在验证失败后继续消费后续输出。
    """

    # 1. 首轮可修复
    if task_def.id == "loop_first_try_pass":
        return [
            _json_tool("read_file", {"path": "calculator.py", "start": 1, "end": 200}),
            '<tool name="patch_file" path="calculator.py">'
            '<old_text>def add(a, b):\n    return a - b</old_text>'
            '<new_text>def add(a, b):\n    return a + b</new_text>'
            '</tool>',
            _json_tool("run_shell", {"command": "python -c \"from calculator import add; assert add(2, 3) == 5\""}),
            "<final>修复完成。</final>",
        ]

    # 2. 多轮修复：同一序列先错后对。
    #    control 只消费前 4 条（改乘法）→ 验证失败 → blocked；
    #    loop 跨 Cycle 继续消费后 4 条（改加法）→ 验证通过 → completed。
    if task_def.id == "loop_multi_cycle_fix":
        return [
            # Cycle 1: 改成乘法（仍失败）。内部检查只确认模块仍可调用，
            # 将错误留给外部 Acceptance Contract 发现，从而开始下一 Cycle。
            _json_tool("read_file", {"path": "calculator.py", "start": 1, "end": 200}),
            '<tool name="patch_file" path="calculator.py">'
            '<old_text>def add(a, b):\n    return a - b</old_text>'
            '<new_text>def add(a, b):\n    return a * b</new_text>'
            '</tool>',
            _json_tool("run_shell", {"command": "python -c \"from calculator import add; assert callable(add)\""}),
            "<final>第一轮尝试完成，等待外部验证。</final>",
            # Cycle 2: 从乘法改为加法（通过）
            _json_tool("read_file", {"path": "calculator.py", "start": 1, "end": 200}),
            '<tool name="patch_file" path="calculator.py">'
            '<old_text>def add(a, b):\n    return a * b</old_text>'
            '<new_text>def add(a, b):\n    return a + b</new_text>'
            '</tool>',
            _json_tool("run_shell", {"command": "python -c \"from calculator import add; assert add(2, 3) == 5\""}),
            "<final>第二轮修复完成。</final>",
        ]

    # 3. 已解决 / 4. 无法复现：不修改任何文件
    if task_def.id in ("loop_already_resolved", "loop_cannot_reproduce"):
        return ["<final>无需修改。</final>"]

    # 5. 基础设施错误
    if task_def.id == "loop_infra_error":
        return ["<final>尝试修复。</final>"]

    # 6. 修改受保护路径
    if task_def.id == "loop_forbidden_path":
        unique_text = "from calculator import add"
        return [
            _json_tool("read_file", {"path": "test_calculator.py", "start": 1, "end": 200}),
            f'<tool name="patch_file" path="test_calculator.py">'
            f'<old_text>{unique_text}</old_text>'
            f'<new_text># {unique_text}</new_text>'
            f'</tool>',
            "<final>修改了测试文件。</final>",
        ]

    # 7. 停滞：多轮重复无效尝试
    if task_def.id == "loop_stagnation":
        payload = _json_tool("read_file", {"path": "calculator.py", "start": 1, "end": 200})
        result = []
        for _ in range(5):
            result.append(payload)
            result.append("<final>尝试修复，不确定。</final>")
        return result

    # 8. 中断恢复：真实编排见 _run_loop_resume（crash 段 + 续接段），
    #    此处保留一个合法序列，供定义级测试（unique patch text 等）使用。
    if task_def.id == "loop_resume":
        return [
            _json_tool("read_file", {"path": "calculator.py", "start": 1, "end": 200}),
            '<tool name="patch_file" path="calculator.py">'
            '<old_text>def add(a, b):\n    return a - b</old_text>'
            '<new_text>def add(a, b):\n    return a + b</new_text>'
            '</tool>',
            _json_tool("run_shell", {"command": "python -c \"from calculator import add; assert add(2, 3) == 5\""}),
            "<final>修复完成。</final>",
        ]

    # fallback: 不应到达
    return ["<final>fallback。</final>"]


def _build_scripted_agent(workspace: Path, client: ScriptedModelClient) -> Pico:
    ws = WorkspaceContext.build(str(workspace))
    session_store = _make_session_store(workspace)
    return Pico(
        model_client=client,
        workspace=ws,
        session_store=session_store,
        approval_policy="auto",
        max_steps=10,
    )


def _make_session_store(workspace: Path):
    from ..paths import workspace_state_path
    from ..core.session_store import SessionStore
    store_dir = workspace_state_path(workspace, "sessions")
    store_dir.mkdir(parents=True, exist_ok=True)
    return SessionStore(store_dir)


def _build_resume_scripted_agent(workspace: Path, client: ScriptedModelClient,
                                 session_id: str) -> Pico:
    """构建恢复 agent：加载同一 session（历史连续），续接同一脚本序列。"""
    ws = WorkspaceContext.build(str(workspace))
    session_store = _make_session_store(workspace)
    session = session_store.load(session_id)
    return Pico(
        model_client=client,
        workspace=ws,
        session_store=session_store,
        session=session,
        approval_policy="auto",
        max_steps=10,
    )


def _run_artifact_refs(agent, workspace: Path) -> list[str]:
    """收集一次 Harness Run 的 report/trace 工件（相对 workspace 路径）。"""
    if agent is None:
        return []
    refs = []
    try:
        ts = getattr(agent, "current_task_state", None)
        rd = getattr(agent, "run_store", None)
        if ts and rd:
            run_dir = rd.run_dir(ts)
            for p in [run_dir / "report.json", run_dir / "trace.jsonl"]:
                if p.exists():
                    try:
                        refs.append(str(p.resolve().relative_to(Path(workspace).resolve())))
                    except ValueError:
                        refs.append(str(p))
    except Exception:
        pass
    return refs


def _attempt_artifact_refs(task, store, workspace: Path) -> list[str]:
    """收集 Task 全部 attempt 的 run 工件（相对 workspace 路径）。"""
    refs = []
    try:
        attempts = store.list_attempts(task.task_id)
    except Exception:
        return refs
    ws = Path(workspace).resolve()
    for attempt in attempts:
        for ref in (getattr(attempt, "artifact_refs", None) or []):
            try:
                refs.append(str(Path(ref).resolve().relative_to(ws)))
            except ValueError:
                refs.append(str(ref))
    return sorted(set(refs))


def _run_dir_artifact_refs(workspace: Path, run_ids) -> list[str]:
    """收集 run_store 目录下的 report/trace（含崩溃未完成 Run 的工件）。

    resume 场景中崩溃 Run 没有 record_run_finished，artifact_refs 为空，
    但其 report/trace 仍留在 ``.forge/runs/<run_id>/``，必须纳入证据。
    """
    refs = []
    runs_dir = Path(workspace) / ".forge" / "runs"
    if not runs_dir.is_dir():
        return refs
    ws = Path(workspace).resolve()
    for run_id in (run_ids or []):
        for name in ("report.json", "trace.jsonl"):
            p = runs_dir / str(run_id) / name
            if p.exists():
                try:
                    refs.append(str(p.resolve().relative_to(ws)))
                except ValueError:
                    refs.append(str(p))
    return sorted(set(refs))


def _task_evidence_refs(task) -> list[str]:
    """收集 baseline 与最终验证的 evidence 相对路径。"""
    refs = []
    baseline = getattr(task, "baseline", None) or {}
    if baseline.get("evidence_ref"):
        refs.append(baseline["evidence_ref"])
    verdict = getattr(task, "last_verdict", None) or {}
    for ref in (verdict.get("evidence_refs") or []):
        if ref not in refs:
            refs.append(ref)
    return refs


def _resume_crash_outputs() -> list:
    """loop_resume 阶段 1 的脚本序列：前两个输出执行错误修复，第三个模拟进程崩溃。

    SystemExit 是 BaseException，不会被 _do_run 的 ``except Exception`` 捕获，
    会直接杀死 worker 进程——attempt 保持 active，留给主进程 recover()。
    """
    return [
        _json_tool("read_file", {"path": "calculator.py", "start": 1, "end": 200}),
        '<tool name="patch_file" path="calculator.py">'
        '<old_text>def add(a, b):\n    return a - b</old_text>'
        '<new_text>def add(a, b):\n    return a * b</new_text>'
        '</tool>',
        SystemExit(1),
    ]


def _resume_continue_outputs() -> list[str]:
    """loop_resume 阶段 2 的脚本序列：恢复后从错误修复改为正确修复。"""
    return [
        _json_tool("read_file", {"path": "calculator.py", "start": 1, "end": 200}),
        '<tool name="patch_file" path="calculator.py">'
        '<old_text>def add(a, b):\n    return a * b</old_text>'
        '<new_text>def add(a, b):\n    return a + b</new_text>'
        '</tool>',
        _json_tool("run_shell", {"command": "python -c \"from calculator import add; assert add(2, 3) == 5\""}),
        "<final>恢复后修复完成。</final>",
    ]


def _resume_crash_worker(workspace: str, task_id: str):
    """子进程：执行 loop_resume 的首次 Run，脚本段以 SystemExit 崩溃。

    由 ``multiprocessing`` spawn 上下文启动；args 全部为可 pickle 的字符串。
    崩溃前 record_run_started 已通过 on_run_started 落盘 active attempt，
    崩溃后由主进程的 ``recover()`` 检测并恢复同一 Run。
    """
    from pathlib import Path as _Path

    from ..features.loop import LoopController, TaskContextBuilder
    from ..features.task_record import TaskStore
    from ..features.verification import ContractStore, TaskVerificationService

    ws = _Path(workspace)
    store = TaskStore(ws / ".forge")
    cs = ContractStore(ws / ".forge" / "tasks")
    vs = TaskVerificationService(store, cs, ws, ws / ".forge" / "tasks")
    client = ScriptedModelClient(_resume_crash_outputs())

    def _build_agent():
        return _build_scripted_agent(ws, client)

    ctrl = LoopController(
        task_store=store, contract_store=cs, verification_service=vs,
        context_builder=TaskContextBuilder(), build_agent_fn=_build_agent,
    )
    ctrl.advance_full(task_id)
    # 正常返回说明脚本没有按预期崩溃（例如 SystemExit 被吞掉）——进程正常退出。


# ---------------------------------------------------------------------------
# 报告生成
# ---------------------------------------------------------------------------

def summarize_loop_benchmark(results: list[dict]) -> dict:
    """生成汇总报告，包含 control 和 loop 的各自指标。"""
    if not results:
        return {"error": "no results"}

    control = [r for r in results if r.get("variant") == "control"]
    loop = [r for r in results if r.get("variant") == "loop"]

    def _summarize(group: list[dict], label: str) -> dict:
        n = len(group)
        if n == 0:
            return {f"{label}_task_count": 0}
        successes = sum(1 for r in group if r.get("task_success"))
        false_completions = sum(1 for r in group if r.get("false_completion"))
        escalated = sum(1 for r in group if r.get("escalated"))
        no_progress = sum(1 for r in group if r.get("no_progress"))
        cycles = [r.get("cycles_to_success", 0) for r in group if r.get("task_success")]
        # roadmap 9 项核心指标
        verification_pass = sum(1 for r in group if r.get("verification_pass"))
        resume_ok = sum(1 for r in group if r.get("resume_success") is True)
        resume_recorded = sum(1 for r in group if r.get("resume_success") is not None)
        tool_steps_ok = [r.get("tool_steps", 0) for r in group if r.get("task_success")]
        tokens_ok = [r.get("tokens") for r in group if r.get("task_success") and r.get("tokens") is not None]
        return {
            f"{label}_task_count": n,
            f"{label}_success_rate": round(successes / n, 3) if n else 0.0,
            f"{label}_false_completion_rate": round(false_completions / n, 3) if n else 0.0,
            f"{label}_avg_cycles_to_success": round(sum(cycles) / len(cycles), 2) if cycles else 0.0,
            f"{label}_escalation_rate": round(escalated / n, 3) if n else 0.0,
            f"{label}_no_progress_rate": round(no_progress / n, 3) if n else 0.0,
            f"{label}_verification_pass_rate": round(verification_pass / n, 3) if n else 0.0,
            f"{label}_resume_success_rate": (
                round(resume_ok / resume_recorded, 3) if resume_recorded else "unavailable"
            ),
            f"{label}_no_progress_stop_precision": "unavailable",  # 需人工标注
            f"{label}_tool_steps_per_success": (
                round(sum(tool_steps_ok) / len(tool_steps_ok), 2) if tool_steps_ok else "unavailable"
            ),
            f"{label}_tokens_per_success": (
                round(sum(tokens_ok) / len(tokens_ok), 2) if tokens_ok else "unavailable"
            ),
        }

    report = {
        "total_tasks": len(results) // 2,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        **_summarize(control, "control"),
        **_summarize(loop, "loop"),
        "results": results,
    }

    # 分组：严格对称 A/B vs diagnostic。
    # comparable=False 的任务（如 loop_stagnation）预算/编排偏离 control，
    # 不得混入 strict_ab 的 success rate；顶层指标为全部任务，结论以分组为准。
    strict_control = [r for r in control if r.get("comparable", True)]
    strict_loop = [r for r in loop if r.get("comparable", True)]
    diag_control = [r for r in control if not r.get("comparable", True)]
    diag_loop = [r for r in loop if not r.get("comparable", True)]
    report["groups"] = {
        "strict_ab": {
            "control": _summarize(strict_control, "control"),
            "loop": _summarize(strict_loop, "loop"),
        },
        "diagnostic": {
            "control": _summarize(diag_control, "control"),
            "loop": _summarize(diag_loop, "loop"),
        },
    }
    report["note"] = (
        "顶层 success_rate 等指标包含全部任务（含 diagnostic 任务）；"
        "严格对称 A/B 结论以 groups.strict_ab 为准，"
        "预算/编排偏离 control 的任务见 groups.diagnostic。"
    )
    return report


def _benchmark_provenance(fixtures_root: Path, artifact_path: Path | None = None) -> dict:
    """收集可复现 benchmark 所需的运行时和代码来源信息。"""
    import platform as _platform
    from .._version import __version__

    provenance: dict = {
        "forge_version": __version__,
        "python_version": _platform.python_version(),
        "platform": _platform.platform(),
        "fixtures_root": str(fixtures_root),
    }

    # Git commit / branch（若仓库可用）
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            provenance["commit"] = r.stdout.strip()
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            provenance["branch"] = r.stdout.strip()
    except Exception:
        pass

    # fixture 快照 digest（相对路径 + 长度分隔，避免同名文件边界混淆）
    try:
        digest = hashlib.sha256()
        for f in sorted(fixtures_root.rglob("*")):
            if f.is_file() and ".git" not in f.parts:
                rel = f.relative_to(fixtures_root).as_posix()
                digest.update(len(rel).to_bytes(4, "big"))
                digest.update(rel.encode("utf-8"))
                digest.update(f.read_bytes())
        provenance["fixture_snapshot_id"] = "sha256:" + digest.hexdigest()
    except Exception:
        pass

    # scripted 输出序列 hash：重放者可以校验脚本版本一致
    try:
        seq_digest = hashlib.sha256()
        for t in BENCH_TASKS:
            for out in _scripted_outputs(t):
                seq_digest.update(str(out).encode("utf-8"))
            if t.id == "loop_resume":
                for out in _resume_crash_outputs():
                    seq_digest.update(repr(out).encode("utf-8"))
                for out in _resume_continue_outputs():
                    seq_digest.update(str(out).encode("utf-8"))
        provenance["scripted_outputs_hash"] = "sha256:" + seq_digest.hexdigest()
    except Exception:
        pass

    # 关键依赖版本（重放环境信息）
    try:
        from importlib import metadata as _metadata
        deps: dict[str, str] = {}
        for name in ("forge", "pytest", "textual"):
            try:
                deps[name] = _metadata.version(name)
            except Exception:
                pass
        if deps:
            provenance["dependencies"] = deps
    except Exception:
        pass

    # fixture 可获取引用
    provenance["fixture_ref"] = {
        "root": str(fixtures_root),
        "snapshot_id": provenance.get("fixture_snapshot_id", ""),
        "availability": "source repository: tests/fixtures/",
    }

    return provenance


def run_loop_benchmark(
    fixtures_root: str | Path | None = None,
    workspace_root: str | Path | None = None,
    artifact_path: str | Path | None = None,
) -> dict:
    """一条命令运行全部 Loop benchmark（A/B 对照）。"""
    f_root = Path(fixtures_root or Path.cwd() / "tests" / "fixtures").resolve()
    w_root = Path(workspace_root or Path(tempfile.mkdtemp(prefix="forge-bench-"))).resolve()
    w_root.mkdir(parents=True, exist_ok=True)

    runner = LoopBenchmarkRunner(f_root, w_root)
    results = runner.run_all()
    report = summarize_loop_benchmark(results)

    # 复现元数据（commit / 版本 / 平台 / fixture 快照）
    report["provenance"] = _benchmark_provenance(f_root)
    report["benchmark_config"] = {
        "task_ids": [t.id for t in BENCH_TASKS],
        "task_count": len(BENCH_TASKS),
        "contract_hashes": {
            t.id: t.to_contract().content_hash for t in BENCH_TASKS
        },
        "scripted_outputs": "deterministic ScriptedModelClient",
        "random_seed": "scripted-deterministic",
        "tool_policy": "default",
        "approval_policy": "auto",
        "budget": {
            "control": {
                "max_steps": 10,
                "max_tokens": "unavailable",   # Harness 未暴露 token 计数
                # 目标墙钟预算（与 loop 对齐）；仅在 run 结束后检查，
                # 不能中断卡住的模型/工具调用——不是硬性超时。
                "wall_time_seconds": 1800,
                "wall_time_enforced": False,
            },
            "loop": {
                "max_steps_per_cycle": 10,
                "max_cycles": 4,
                # 与 control 对齐的总工具步预算；仅 loop_stagnation 放宽到 30，
                # 因为“停滞 → REPLAN → 仍停滞 → 转人工”的升级语义必须跨多个 Cycle 展示。
                # 该任务 comparable=False，只计入 diagnostic 分组，不混入 strict_ab。
                "max_total_tool_steps": {
                    t.id: t.budget_tool_steps for t in BENCH_TASKS
                },
                # 目标墙钟预算：run 结束后检查，不能中断卡住的调用——非硬性超时。
                "max_wall_time_seconds": 1800,
                "wall_time_enforced": False,
                "max_tokens": "unavailable",
            },
        },
    }

    if artifact_path:
        path = Path(artifact_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"report saved: {path}")

    return report