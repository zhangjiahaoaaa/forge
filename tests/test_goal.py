"""Tests for `forge goal` — 一句话启动 Loop 任务的默认合同发现。"""

import json
from pathlib import Path

from forge.cli import main
from forge.features.verification import discover_default_contract


def test_goal_dispatch_usage(tmp_path):
    """无参数时输出 usage 并返回 1。"""
    assert main(["goal"]) == 1


def test_repl_goal_usage_without_argument():
    """/goal 无参数时给出用法提示，不执行任何动作。"""
    from forge.cli import handle_repl_command

    handled, should_exit, output = handle_repl_command(object(), "/goal")
    assert handled is True
    assert should_exit is False
    assert "Usage: /goal" in output


def test_repl_goal_dispatches_to_loop(monkeypatch):
    """/goal 带参数 → 启动任务并跑 Loop（mock 掉真实执行，验证分发与输出）。"""
    from types import SimpleNamespace

    from forge.cli import handle_repl_command

    def fake_start_goal(goal, cwd, agent=None):
        assert goal == "修复 add"
        assert cwd == agent.root.resolve()
        task = SimpleNamespace(task_id="task_x")
        contract = SimpleNamespace(
            verify_commands=[SimpleNamespace(command="python -m pytest -q")],
            change_policy=SimpleNamespace(forbidden_paths=["test_calculator.py"]),
        )
        store = SimpleNamespace(load_task=lambda tid: SimpleNamespace(
            status="completed", cycle=1, last_run_summary="done"))
        ctrl = SimpleNamespace(advance_full=lambda tid: {
            "action": "completed",
            "task": SimpleNamespace(status="completed", cycle=1, last_run_summary="done"),
        })
        return store, task, contract, ctrl

    agent = SimpleNamespace(root=Path.cwd())
    monkeypatch.setattr("forge.cli._start_goal_task", fake_start_goal)
    handled, should_exit, output = handle_repl_command(agent, "/goal 修复 add")
    assert handled is True
    assert should_exit is False
    assert "task_id:   task_x" in output
    assert "python -m pytest -q" in output
    assert "test_calculator.py" in output
    assert "action:    completed" in output


def test_discover_contract_with_pytest_tests(tmp_path):
    """有 pytest 测试 → 验收=跑全部测试，禁改测试文件。"""
    (tmp_path / "calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "test_calculator.py").write_text(
        "from calculator import add\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    venv_dir = tmp_path / ".venv"
    venv_dir.mkdir(parents=True, exist_ok=True)
    (venv_dir / "test_venv_dummy.py").write_text("x = 1\n", encoding="utf-8")

    contract = discover_default_contract("修复 add 符号错误", tmp_path)

    assert contract.reproduce_command == "python -m pytest -q"
    assert contract.verify_commands[0].command == "python -m pytest -q"
    assert contract.reproduce is not None
    assert contract.reproduce.outcome == "target_failure"
    assert contract.default_contract is True
    # 禁改测试文件（.venv 下的测试不算）
    assert "test_calculator.py" in contract.change_policy.forbidden_paths
    assert all("test_venv_dummy" not in p for p in contract.change_policy.forbidden_paths)
    assert contract.change_policy.allowed_paths == ["*"]


def test_discover_contract_without_tests(tmp_path):
    """没有测试 → 守门员合同：compileall 语法检查，允许改任何文件。"""
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")

    contract = discover_default_contract("做点小事", tmp_path)

    assert "compileall" in contract.verify_commands[0].command
    assert contract.change_policy.forbidden_paths == []
    assert contract.change_policy.allowed_paths == ["*"]


def test_discover_contract_matches_star_test_pattern(tmp_path):
    """*_test.py 命名也能被识别。"""
    (tmp_path / "calc_test.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    contract = discover_default_contract("g", tmp_path)
    assert "calc_test.py" in contract.change_policy.forbidden_paths


def test_goal_contract_is_serializable_and_hashable(tmp_path):
    """生成的合同可以落盘并计算 content_hash（与 contract init 同构）。"""
    (tmp_path / "test_a.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    contract = discover_default_contract("g", tmp_path)
    data = contract.to_dict()
    json.dumps(data, ensure_ascii=False)  # 可序列化
    assert contract.content_hash
    assert contract.content_hash.startswith("sha256:") or len(contract.content_hash) >= 32


def test_goal_is_registered_for_help_and_completion():
    """/goal 必须作为正式 slash command 出现在帮助和补全中。"""
    from forge.commands.slash import command_help_text, resolve_command, suggest_commands

    assert "/goal <目标>" in command_help_text()
    assert resolve_command("goal").name == "goal"
    assert [command.name for command in suggest_commands("/go")] == ["goal"]


def test_default_goal_contract_not_reproduced_waits_for_precise_contract(tmp_path):
    """自动合同不能因全量检查通过就把自然语言目标误判为已完成。"""
    from forge.features.loop import LoopController
    from forge.features.task_record import TaskStore
    from forge.features.verification import ContractStore, TaskVerificationService

    (tmp_path / "main.py").write_text("print('ok')\n", encoding="utf-8")
    store = TaskStore(tmp_path / ".forge")
    task = store.create_task("修复未被测试覆盖的问题")
    contract = discover_default_contract(task.goal, tmp_path)
    contract_store = ContractStore(tmp_path / ".forge" / "tasks")
    contract_store.save_contract(task.task_id, contract)
    task.contract_hash = contract.content_hash
    store.save_task(task)
    service = TaskVerificationService(
        store, contract_store, tmp_path, tmp_path / ".forge" / "tasks"
    )
    controller = LoopController(store, contract_store, service)

    result = controller.advance(task.task_id)
    saved = store.load_task(task.task_id)

    assert result["action"] == "waiting_human"
    assert result["reason"] == "default_contract_not_reproduced"
    assert saved.status.value == "waiting_human"
    assert "补充目标测试" in saved.last_run_summary


def test_repl_goal_uses_active_agent_workspace_not_process_cwd(tmp_path, monkeypatch):
    """交互 /goal 必须使用当前 Agent 工作区，不能误写入启动终端的 CWD。"""
    from forge import Pico, SessionStore, WorkspaceContext
    from forge.cli import handle_repl_command
    from forge.testing import ScriptedModelClient

    workspace = tmp_path / "workspace"
    process_cwd = tmp_path / "process-cwd"
    workspace.mkdir()
    process_cwd.mkdir()
    (workspace / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Pico(
        model_client=ScriptedModelClient([]),
        workspace=WorkspaceContext.build(workspace, repo_root_override=workspace),
        session_store=SessionStore(workspace / ".forge" / "sessions"),
        approval_policy="auto",
    )

    seen = {}

    def fake_start(goal, cwd, agent=None):
        seen["goal"] = goal
        seen["cwd"] = cwd
        seen["agent"] = agent
        task = type("Task", (), {"task_id": "task_x"})()
        contract = type(
            "Contract",
            (), {
                "verify_commands": [],
                "change_policy": type("Policy", (), {"forbidden_paths": []})(),
            },
        )()
        controller = type(
            "Controller",
            (), {
                "advance_full": lambda self, task_id: {
                    "action": "completed",
                    "reason": "pass",
                    "task": type(
                        "FinishedTask",
                        (), {"status": "completed", "cycle": 1, "last_run_summary": "done"},
                    )(),
                }
            },
        )()
        return object(), task, contract, controller

    monkeypatch.chdir(process_cwd)
    monkeypatch.setattr("forge.cli._start_goal_task", fake_start)

    handled, should_exit, output = handle_repl_command(agent, "/goal 修复登录")

    assert handled is True
    assert should_exit is False
    assert "action:    completed" in output
    assert seen == {"goal": "修复登录", "cwd": workspace.resolve(), "agent": agent}


def test_loop_agent_inherits_active_runtime_configuration(tmp_path):
    """交互 /goal 生成的每轮 Agent 必须继承当前会话的配置与回调。"""
    from forge import Pico, SessionStore, WorkspaceContext
    from forge.cli import _build_controller
    from forge.testing import ScriptedModelClient

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Pico(
        model_client=ScriptedModelClient([]),
        workspace=WorkspaceContext.build(tmp_path, repo_root_override=tmp_path),
        session_store=SessionStore(tmp_path / ".forge" / "sessions"),
        approval_policy="ask",
        max_steps=7,
        max_new_tokens=321,
    )
    def approve(name, args):
        return True

    def ask_user(question, choices):
        return "continue"

    agent.approve = approve
    agent.ask_user_callback = ask_user

    loop_agent = _build_controller(tmp_path, source_agent=agent)._build_agent()

    assert loop_agent is not agent
    assert loop_agent.root == agent.root
    assert loop_agent.max_steps == 7
    assert loop_agent.max_new_tokens == 321
    assert loop_agent.approval_policy == "ask"
    assert loop_agent.approve is approve
    assert loop_agent.ask_user_callback is ask_user
