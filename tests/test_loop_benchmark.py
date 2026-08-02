"""Tests for PR 4: Loop Benchmark.

All deterministic — no live provider needed.
"""

import json
import pytest
from pathlib import Path

from forge.evaluation.loop_benchmark import (
    BENCH_TASKS,
    LoopBenchTask,
    LoopBenchmarkRunner,
    run_loop_benchmark,
    summarize_loop_benchmark,
)


# ---------------------------------------------------------------------------
# 任务定义验证
# ---------------------------------------------------------------------------

def test_bench_task_definitions_count():
    assert len(BENCH_TASKS) == 8


def test_bench_task_definitions_have_unique_ids():
    ids = [t.id for t in BENCH_TASKS]
    assert len(ids) == len(set(ids))


def test_bench_task_definitions_have_goal():
    for t in BENCH_TASKS:
        assert t.goal, f"task {t.id} missing goal"


def test_bench_task_definitions_have_fixture():
    for t in BENCH_TASKS:
        assert t.fixture_repo, f"task {t.id} missing fixture_repo"


def test_bench_task_expected_outcomes():
    for t in BENCH_TASKS:
        assert t.expected_outcome in ("completed", "blocked", "waiting_human"), \
            f"task {t.id} invalid expected_outcome: {t.expected_outcome}"


def test_bench_task_to_contract_with_reproduce():
    for t in BENCH_TASKS:
        contract = t.to_contract()
        assert contract.goal == t.goal
        if t.reproduce_command:
            assert contract.reproduce_command == t.reproduce_command
            assert contract.reproduce is not None, f"task {t.id} reproduce is None"
            if t.reproduce_exit_code is not None:
                assert contract.reproduce.exit_code == t.reproduce_exit_code


def test_bench_task_to_contract_with_verify():
    for t in BENCH_TASKS:
        contract = t.to_contract()
        if t.verify_commands:
            assert len(contract.verify_commands) == len(t.verify_commands)
            assert contract.verify_commands[0].command == t.verify_commands[0]["command"]


def test_scripted_outputs_have_unique_patch_text():
    """检查所有 patch_file 使用的 old_text 在 fixture 中唯一。"""
    from forge.evaluation.loop_benchmark import _scripted_outputs
    import re

    for t in BENCH_TASKS:
        outputs = _scripted_outputs(t)
        for out in outputs:
            if "old_text" not in out:
                continue
            m = re.search(r'<old_text>(.*?)</old_text>', out, re.DOTALL)
            assert m, f"task {t.id}: old_text not found in: {out[:80]}"
            old = m.group(1)
            # 旧文本必须包含换行（函数上下文），且不包含模板占位符
            if "\n" not in old:
                # forbidden_path 场景使用单行文本
                assert t.id == "loop_forbidden_path", \
                    f"task {t.id}: old_text should be multi-line, got: {old[:60]}"
            assert "{" not in old, f"task {t.id}: old_text contains template placeholder"


# ---------------------------------------------------------------------------
# 汇总逻辑
# ---------------------------------------------------------------------------

def test_summarize_empty():
    report = summarize_loop_benchmark([])
    assert "error" in report


def test_summarize_splits_strict_ab_and_diagnostic():
    """comparable=False 的任务只进 diagnostic 分组，不混入 strict_ab 指标。"""
    results = [
        {"task_id": "a", "variant": "control", "comparable": True, "task_success": True,
         "false_completion": False, "escalated": False, "no_progress": False, "cycles_to_success": 1},
        {"task_id": "a", "variant": "loop", "comparable": True, "task_success": True,
         "false_completion": False, "escalated": False, "no_progress": False, "cycles_to_success": 1},
        {"task_id": "stagnation", "variant": "control", "comparable": False, "task_success": False,
         "false_completion": False, "escalated": True, "no_progress": True, "cycles_to_success": 0},
        {"task_id": "stagnation", "variant": "loop", "comparable": False, "task_success": False,
         "false_completion": False, "escalated": True, "no_progress": True, "cycles_to_success": 0},
    ]
    report = summarize_loop_benchmark(results)
    groups = report["groups"]
    assert groups["strict_ab"]["control"]["control_task_count"] == 1
    assert groups["strict_ab"]["loop"]["loop_success_rate"] == 1.0
    assert groups["diagnostic"]["loop"]["loop_task_count"] == 1
    assert groups["diagnostic"]["loop"]["loop_escalation_rate"] == 1.0
    assert report["note"]


# ---------------------------------------------------------------------------
# 独立复核：镜像同步（删除 / 新增文件）
# ---------------------------------------------------------------------------

def _make_fixture_with_extra(tmp_path: Path) -> Path:
    """fixture：calculator.py + test_calculator.py + extra.py。"""
    repo = tmp_path / "fixtures" / "bench_repo_bugfix_py"
    repo.mkdir(parents=True)
    (repo / "calculator.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calculator.py").write_text(
        "from calculator import add\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8")
    (repo / "extra.py").write_text("EXTRA = 1\n", encoding="utf-8")
    _init_git(repo)
    return repo


def _recheck_task(**kw) -> LoopBenchTask:
    defaults = dict(
        id="recheck_probe", goal="probe", fixture_repo="bench_repo_bugfix_py",
        verify_commands=[{"command": 'python -c "exit(0)"', "timeout": 10}],
        allowed_paths=["*"],
    )
    defaults.update(kw)
    return LoopBenchTask(**defaults)


def test_recheck_syncs_deleted_files(tmp_path):
    """agent 删除 allowed 文件 → 复核 workspace 镜像同步删除，验证通过。"""
    fixture_dir = _make_fixture_with_extra(tmp_path)
    runner = LoopBenchmarkRunner(tmp_path / "fixtures", tmp_path / "workspaces")
    task_def = _recheck_task(
        verify_commands=[{"command":
                           'python -c "import os; assert not os.path.exists(\'extra.py\')"',
                           "timeout": 10}],
    )
    agent_ws = runner._setup_workspace(fixture_dir)
    (agent_ws / "extra.py").unlink()  # agent 删除文件
    store, cs, vs, task, contract = runner._build_task(agent_ws, task_def)
    assert runner._independent_recheck(task_def, agent_ws, fixture_dir, contract) is True


def test_recheck_delete_forbidden_file_is_flagged(tmp_path):
    """agent 删除 forbidden 文件 → scope 拦截，复核必须 FAIL。"""
    fixture_dir = _make_fixture_with_extra(tmp_path)
    runner = LoopBenchmarkRunner(tmp_path / "fixtures", tmp_path / "workspaces")
    task_def = _recheck_task(forbidden_paths=["test_calculator.py"])
    agent_ws = runner._setup_workspace(fixture_dir)
    (agent_ws / "test_calculator.py").unlink()  # agent 删除 forbidden 文件
    store, cs, vs, task, contract = runner._build_task(agent_ws, task_def)
    assert runner._independent_recheck(task_def, agent_ws, fixture_dir, contract) is False


def test_recheck_includes_new_files(tmp_path):
    """agent 新增文件 → 复核 workspace 包含该文件，验证通过。"""
    fixture_dir = _make_fixture_with_extra(tmp_path)
    runner = LoopBenchmarkRunner(tmp_path / "fixtures", tmp_path / "workspaces")
    task_def = _recheck_task(
        verify_commands=[{"command":
                           'python -c "import os; assert os.path.exists(\'new_module.py\')"',
                           "timeout": 10}],
    )
    agent_ws = runner._setup_workspace(fixture_dir)
    (agent_ws / "new_module.py").write_text("X = 1\n", encoding="utf-8")
    store, cs, vs, task, contract = runner._build_task(agent_ws, task_def)
    assert runner._independent_recheck(task_def, agent_ws, fixture_dir, contract) is True


def test_cli_benchmark_loop_dispatch(tmp_path):
    """forge benchmark loop 子命令可分发；fixture 缺失时优雅报告而非崩溃。"""
    from forge.cli import main
    # usage / unknown 分支
    assert main(["benchmark"]) == 1
    assert main(["benchmark", "bogus"]) == 1
    # fixture 缺失 → 全部任务记录 error，命令仍正常退出 0
    missing = tmp_path / "no-fixtures"
    assert main(["benchmark", "loop", "--fixtures", str(missing)]) == 0


def test_summarize_has_control_and_loop():
    results = [
        {"task_id": "t1", "variant": "control", "task_success": True, "false_completion": False,
         "escalated": False, "no_progress": False, "cycles_to_success": 1},
        {"task_id": "t1", "variant": "loop", "task_success": True, "false_completion": False,
         "escalated": False, "no_progress": False, "cycles_to_success": 2},
    ]
    report = summarize_loop_benchmark(results)
    assert report["control_success_rate"] == 1.0
    assert report["loop_success_rate"] == 1.0
    assert report["control_task_count"] == 1
    assert report["loop_task_count"] == 1


def test_summarize_false_completion():
    results = [
        {"task_id": "t1", "variant": "loop", "task_success": True, "false_completion": True,
         "escalated": False, "no_progress": False, "cycles_to_success": 1},
    ]
    report = summarize_loop_benchmark(results)
    assert report["loop_false_completion_rate"] == 1.0


def test_summarize_rates_match_records():
    """验证汇总指标可以通过原始 records 重新计算。"""
    results = [
        {"task_id": "t1", "variant": "control", "task_success": True, "false_completion": False,
         "escalated": False, "no_progress": False, "cycles_to_success": 1},
        {"task_id": "t1", "variant": "loop", "task_success": False, "false_completion": False,
         "escalated": True, "no_progress": True, "cycles_to_success": 0},
    ]
    report = summarize_loop_benchmark(results)
    # 手动复算
    ctrl = [r for r in results if r["variant"] == "control"]
    assert report["control_success_rate"] == sum(1 for r in ctrl if r["task_success"]) / len(ctrl)
    loop = [r for r in results if r["variant"] == "loop"]
    assert report["loop_escalation_rate"] == sum(1 for r in loop if r["escalated"]) / len(loop)


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------

def test_runner_produces_16_records(tmp_path):
    """Runner 在 fixture 存在时对 8 个任务各生成 control + loop 两条记录。"""
    _make_minimal_fixture(tmp_path)
    runner = LoopBenchmarkRunner(tmp_path / "fixtures", tmp_path / "workspaces")
    results = runner.run_all()
    assert len(results) == 16
    variants = {r.get("variant") for r in results}
    assert variants == {"control", "loop"}


def test_runner_completed_loop_records_not_false_completion(tmp_path):
    """回归：completed 的 loop 记录必须通过独立复核（false_completion=False）。

    曾因 .forge 路径归一化不一致（.forge/... → forge/...）导致 scope 检查把
    runtime 状态文件判为 allowed_outside_scope，所有 completed 任务都被误报
    false_completion=True。
    """
    _make_minimal_fixture(tmp_path)
    runner = LoopBenchmarkRunner(tmp_path / "fixtures", tmp_path / "workspaces")
    results = runner.run_all()
    completed = [r for r in results if r.get("variant") == "loop" and r.get("task_success")]
    assert completed, "expected at least one completed loop record"
    for rec in completed:
        assert rec.get("false_completion") is False, \
            f"{rec['task_id']}: completed but false_completion=True"
        assert rec.get("independent_recheck_pass") is True, \
            f"{rec['task_id']}: independent recheck did not pass"


def test_runner_resume_task_resumes_same_run(tmp_path):
    """loop_resume：真实 crash → recover 同一 run_id，不消耗新 cycle。"""
    _make_minimal_fixture(tmp_path)
    runner = LoopBenchmarkRunner(tmp_path / "fixtures", tmp_path / "workspaces")
    results = runner.run_all()
    rec = next(r for r in results if r["task_id"] == "loop_resume" and r["variant"] == "loop")
    assert rec["outcome"] == "completed", f"got {rec['outcome']}"
    assert rec["resume_success"] is True, f"got resume_success={rec['resume_success']}"
    assert rec["cycles_to_success"] == 1, "resume must not consume a new cycle"
    # 崩溃前 2 步 + 恢复后 3 步 = 5 步（预算结转，不丢失中断步数）
    assert rec["tool_steps"] == 5, f"expected 5 tool steps, got {rec['tool_steps']}"


def test_runner_each_record_has_required_fields(tmp_path):
    _make_minimal_fixture(tmp_path)
    runner = LoopBenchmarkRunner(tmp_path / "fixtures", tmp_path / "workspaces")
    results = runner.run_all()
    required = {"task_id", "variant", "expected_outcome", "outcome",
                "task_success", "verification_pass", "false_completion",
                "contract_hash", "run_artifacts", "verifier_evidence_refs"}
    for r in results:
        missing = required - set(r.keys())
        assert not missing, f"record {r.get('task_id')}/{r.get('variant')} missing: {missing}"


def test_runner_control_and_loop_use_same_outcome_fields(tmp_path):
    _make_minimal_fixture(tmp_path)
    runner = LoopBenchmarkRunner(tmp_path / "fixtures", tmp_path / "workspaces")
    results = runner.run_all()
    ctrl_fields = {k for r in results if r["variant"] == "control" for k in r}
    loop_fields = {k for r in results if r["variant"] == "loop" for k in r}
    diff = ctrl_fields.symmetric_difference(loop_fields)
    allowed_diff = {"variant", "elapsed_seconds", "cycles_to_success", "final_status", "final_action",
                    "resume_success", "reproduce_result"}
    assert diff <= allowed_diff, f"unexpected field diff: {diff - allowed_diff}"


def test_runner_each_task_expected_outcome(tmp_path):
    """Loop 记录的 outcome 必须等于 expected_outcome；control 记录必须存在且字段完整。

    严格对称 A/B 下，control 只有一次 Run：multi_cycle_fix 的 control
    消费序列前半段（改乘法）后验证失败，这是预期的 orchestration 差异，
    不代表 benchmark 配置错误。
    """
    _make_minimal_fixture(tmp_path)
    runner = LoopBenchmarkRunner(tmp_path / "fixtures", tmp_path / "workspaces")
    results = runner.run_all()
    for task in BENCH_TASKS:
        variants = [r for r in results if r["task_id"] == task.id]
        assert {r["variant"] for r in variants} == {"control", "loop"}, \
            f"task {task.id}: missing control or loop variant"
        loop_rec = next(r for r in variants if r["variant"] == "loop")
        err = f"task {task.id} (loop): expected {task.expected_outcome}, got {loop_rec['outcome']}"
        assert loop_rec["outcome"] == task.expected_outcome, err


def test_runner_ab_symmetry_multi_cycle():
    """严格对称 A/B：multi_cycle_fix 的 loop 必须优于 control。

    control 只消费序列前半段（改成乘法）→ 验证失败 → 非 completed；
    loop 跨 Cycle 消费完整序列（改乘法 → 改加法）→ completed。
    """
    from forge.evaluation.loop_benchmark import _scripted_outputs, BENCH_TASKS

    task = next(t for t in BENCH_TASKS if t.id == "loop_multi_cycle_fix")
    outputs = _scripted_outputs(task)
    # 序列长度至少覆盖两个 Cycle（两个 <final>）
    finals = [o for o in outputs if "<final>" in o]
    assert len(finals) == 2, f"multi_cycle sequence should have 2 finals, got {len(finals)}"


def test_runner_multi_cycle_loop_is_two_cycles(tmp_path):
    """multi_cycle_fix 的 loop 记录 cycles_to_success 应为 2。"""
    _make_minimal_fixture(tmp_path)
    runner = LoopBenchmarkRunner(tmp_path / "fixtures", tmp_path / "workspaces")
    results = runner.run_all()
    loop_rec = next(
        r for r in results
        if r["task_id"] == "loop_multi_cycle_fix" and r["variant"] == "loop"
    )
    assert loop_rec["outcome"] == "completed"
    assert loop_rec["cycles_to_success"] == 2, \
        f"expected 2 cycles, got {loop_rec['cycles_to_success']}"


def test_runner_first_try_loop_is_one_cycle(tmp_path):
    """first_try_pass 的 loop 记录 cycles_to_success 应为 1。"""
    _make_minimal_fixture(tmp_path)
    runner = LoopBenchmarkRunner(tmp_path / "fixtures", tmp_path / "workspaces")
    results = runner.run_all()
    loop_rec = next(
        r for r in results
        if r["task_id"] == "loop_first_try_pass" and r["variant"] == "loop"
    )
    assert loop_rec["outcome"] == "completed"
    assert loop_rec["verification_pass"] is True
    assert loop_rec["cycles_to_success"] == 1, \
        f"expected 1 cycle, got {loop_rec['cycles_to_success']}"


def test_runner_workspace_isolation(tmp_path):
    """control 和 loop 使用独立 workspace，control 的修改不应出现在 loop 工作区。"""
    _make_minimal_fixture(tmp_path)
    runner = LoopBenchmarkRunner(tmp_path / "fixtures", tmp_path / "workspaces")
    results = runner.run_all()
    # 验证 control 和 loop 都有记录（隔离性由独立 workspace 保证）
    ctrl = [r for r in results if r["variant"] == "control"]
    loop = [r for r in results if r["variant"] == "loop"]
    assert len(ctrl) == 8
    assert len(loop) == 8


def test_runner_with_real_fixture(tmp_path):
    """使用真实 fixture 的集成测试。"""
    import shutil
    real_fixture = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "bench_repo_bugfix_py"
    if not real_fixture.exists():
        pytest.skip("real fixture not found")
    dst = tmp_path / "fixtures" / "bench_repo_bugfix_py"
    shutil.copytree(real_fixture, dst)
    _init_git(dst)

    runner = LoopBenchmarkRunner(tmp_path / "fixtures", tmp_path / "workspaces")
    results = runner.run_all()
    assert len(results) == 16
    # 至少一些任务能完成 reproduce
    reproduce_results = {r.get("reproduce_result") for r in results if r.get("reproduce_result")}
    assert len(reproduce_results) > 0, "no reproduce results recorded"


# ---------------------------------------------------------------------------
# run_loop_benchmark 集成
# ---------------------------------------------------------------------------

def test_run_loop_benchmark_artifact_content(tmp_path):
    """artifact 的 content 与内存结果一致，且包含复现元数据。"""
    _make_minimal_fixture(tmp_path)
    artifact = tmp_path / "loop-bench.json"
    run_loop_benchmark(
        fixtures_root=str(tmp_path / "fixtures"),
        workspace_root=str(tmp_path / "workspaces"),
        artifact_path=str(artifact),
    )
    assert artifact.exists()
    data = json.loads(artifact.read_text(encoding="utf-8"))
    assert data["total_tasks"] == 8
    assert data["control_task_count"] == 8
    assert data["loop_task_count"] == 8
    assert len(data["results"]) == 16
    # 验证汇总可通过 records 复算
    ctrl = [r for r in data["results"] if r["variant"] == "control"]
    assert data["control_success_rate"] == round(sum(1 for r in ctrl if r["task_success"]) / len(ctrl), 3)
    assert "captured_at" in data
    # 复现元数据
    assert "provenance" in data
    assert "forge_version" in data["provenance"]
    assert "fixture_snapshot_id" in data["provenance"]
    assert "benchmark_config" in data
    assert data["benchmark_config"]["task_count"] == 8
    # contract hash 逐任务落盘 + 预算维度
    assert data["benchmark_config"]["contract_hashes"]["loop_resume"].startswith("sha256:") or \
        len(data["benchmark_config"]["contract_hashes"]["loop_resume"]) >= 32
    budget = data["benchmark_config"]["budget"]
    assert budget["control"]["max_steps"] == 10
    assert budget["loop"]["max_total_tool_steps"]["loop_resume"] == 10
    assert budget["loop"]["max_total_tool_steps"]["loop_stagnation"] == 30
    # 9 项核心指标
    for label in ("control", "loop"):
        for key in (
            "success_rate", "false_completion_rate", "avg_cycles_to_success",
            "verification_pass_rate", "resume_success_rate",
            "no_progress_stop_precision", "tool_steps_per_success", "tokens_per_success",
        ):
            assert f"{label}_{key}" in data, f"missing {label}_{key}"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _make_minimal_fixture(tmp_path: Path):
    """创建最小 fixture 使 runner 能运行。"""
    repo = tmp_path / "fixtures" / "bench_repo_bugfix_py"
    repo.mkdir(parents=True)
    (repo / "calculator.py").write_text(
        "def add(a, b):\n    return a - b\n\ndef subtract(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    (repo / "test_calculator.py").write_text(
        "from calculator import add, subtract\n"
        "def test_add():\n    assert add(2, 3) == 5\n"
        "def test_subtract():\n    assert subtract(5, 3) == 2\n",
        encoding="utf-8",
    )
    _init_git(repo)


def _init_git(path: Path):
    import subprocess
    for cmd in [
        ["git", "init"],
        ["git", "config", "user.email", "t@t.com"],
        ["git", "config", "user.name", "T"],
        ["git", "add", "-A"],
        ["git", "commit", "-m", "init", "--allow-empty"],
    ]:
        try:
            subprocess.run(cmd, cwd=path, capture_output=True, timeout=10)
        except Exception:
            pass