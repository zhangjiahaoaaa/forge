import base64
import json

from forge.testing import ScriptedModelClient
from forge import Pico, SessionStore, WorkspaceContext
from forge.cli import handle_repl_command
from forge.core.permissions import PermissionDecision
from forge.features.sandbox.config import SandboxConfig


def build_agent(tmp_path, outputs=None, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path, repo_root_override=tmp_path)
    store = SessionStore(tmp_path / ".forge" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return Pico(
        model_client=ScriptedModelClient(outputs or []),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )


def read_session_events(agent):
    return [
        json.loads(line)
        for line in agent.session_event_bus.path.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]


def test_permission_checker_is_the_single_default_tool_gate(tmp_path):
    agent = build_agent(tmp_path, approval_policy="never")

    read_decision = agent.permission_checker.check(
        agent.tools["read_file"], {"path": "README.md"}
    )
    shell_decision = agent.permission_checker.check(
        agent.tools["run_shell"], {"command": "echo hi", "timeout": 20}
    )

    assert read_decision == PermissionDecision.allow("read_only")
    assert shell_decision == PermissionDecision.deny(
        "approval_denied", security_event_type="approval_denied"
    )

    result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

    assert result == "error: approval denied for run_shell"
    assert agent._last_tool_result_metadata["tool_error_code"] == "approval_denied"
    assert any(
        event["event"] == "permission_decision"
        and event["tool_name"] == "run_shell"
        and event["decision"] == "deny"
        and event["reason"] == "approval_denied"
        for event in read_session_events(agent)
    )


def test_agent_tools_cannot_modify_durable_task_contract(tmp_path):
    contract_path = tmp_path / ".forge" / "tasks" / "task_001" / "contract.json"
    contract_path.parent.mkdir(parents=True)
    contract_path.write_text('{"goal": "fixed"}\n', encoding="utf-8")
    agent = build_agent(tmp_path, approval_policy="auto")

    write_result = agent.run_tool(
        "write_file", {"path": ".forge/tasks/task_001/contract.json", "content": "{}\n"}
    )
    patch_result = agent.run_tool(
        "patch_file",
        {
            "path": ".forge/tasks/task_001/contract.json",
            "old_text": '"fixed"',
            "new_text": '"tampered"',
        },
    )

    evidence_result = agent.run_tool(
        "write_file", {"path": ".forge/tasks/task_001/evidence/fake.txt", "content": "fake\n"}
    )

    assert "runtime-managed" in write_result
    assert "runtime-managed" in patch_result
    assert "runtime-managed" in evidence_result
    assert contract_path.read_text(encoding="utf-8") == '{"goal": "fixed"}\n'


def test_run_shell_rolls_back_encoded_write_to_durable_task_state(tmp_path):
    contract_path = tmp_path / ".forge" / "tasks" / "task_001" / "contract.json"
    contract_path.parent.mkdir(parents=True)
    original = '{"goal": "fixed"}\n'
    contract_path.write_text(original, encoding="utf-8")
    agent = build_agent(tmp_path, approval_policy="auto")
    payload = base64.b64encode(
        f"from pathlib import Path; Path({str(contract_path)!r}).write_text('tampered')".encode()
    ).decode("ascii")
    command = (
        "python -c \"import base64; exec(base64.b64decode('"
        + payload
        + "'))\""
    )

    result = agent.run_tool("run_shell", {"command": command, "timeout": 20})

    assert "changes were rolled back" in result
    assert contract_path.read_text(encoding="utf-8") == original
    assert agent._last_tool_result_metadata["tool_error_code"] == "protected_task_state_modified"
    assert any(event["event"] == "protected_task_state_violation" for event in read_session_events(agent))


def test_run_shell_rolls_back_new_durable_task_file(tmp_path):
    task_dir = tmp_path / ".forge" / "tasks" / "task_001"
    task_dir.mkdir(parents=True)
    agent = build_agent(tmp_path, approval_policy="auto")

    result = agent.run_tool(
        "run_shell",
        {
            "command": f"python -c \"from pathlib import Path; p=Path({str(tmp_path)!r})/('.for'+'ge')/'tasks'/'task_001'/'evil.txt'; p.write_text('x')\"",
            "timeout": 20,
        },
    )

    assert "changes were rolled back" in result
    assert not (task_dir / "evil.txt").exists()


def test_run_shell_normal_workspace_command_still_works(tmp_path):
    agent = build_agent(tmp_path, approval_policy="auto")

    result = agent.run_tool("run_shell", {"command": "echo ok", "timeout": 20})

    assert "exit_code: 0" in result
    assert "ok" in result


def test_run_shell_required_sandbox_fails_closed_after_permission(tmp_path):
    agent = build_agent(
        tmp_path,
        [],
        approval_policy="auto",
        sandbox_config=SandboxConfig(mode="required", backend="bubblewrap"),
    )
    agent.sandbox_runner.which = lambda name: None

    result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

    assert "sandbox required but unavailable" in result
    assert agent._last_tool_result_metadata["tool_error_code"] == "tool_failed"


def test_run_shell_best_effort_sandbox_degrades_and_keeps_permission_gate(tmp_path):
    agent = build_agent(
        tmp_path,
        [],
        approval_policy="auto",
        sandbox_config=SandboxConfig(mode="best_effort", backend="bubblewrap"),
    )
    agent.sandbox_runner.which = lambda name: None

    result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

    assert "exit_code: 0" in result
    assert "hi" in result
    assert any(
        event["event"] == "sandbox_unavailable" for event in read_session_events(agent)
    )


def test_plan_mode_switches_tool_profile_and_allows_only_active_plan_file(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path=".forge/plans/v3-plan.md"><content># Plan\n- Gate 1\n</content></tool>',
            "<final>Plan ready.</final>",
        ],
        max_steps=3,
    )

    assert agent.active_tool_profile.name == "default"

    plan_path = agent.enter_plan_mode("v3")

    assert plan_path == ".forge/plans/v3-plan.md"
    assert agent.active_tool_profile.name == "plan"
    assert "run_shell" not in agent.active_tool_profile.allowed_tools

    rejected = agent.run_tool(
        "write_file", {"path": "src.py", "content": "print('no')\n"}
    )
    assert (
        rejected
        == "error: plan mode can only write the active plan artifact (.forge/plans/v3-plan.md)"
    )
    assert not (tmp_path / "src.py").exists()

    answer = agent.ask("draft the plan")

    assert answer == "Plan ready."
    assert agent.active_tool_profile.name == "default"
    assert (
        (tmp_path / ".forge" / "plans" / "v3-plan.md")
        .read_text(encoding="utf-8")
        .startswith("# Plan")
    )
    events = read_session_events(agent)
    assert any(
        event["event"] == "permission_decision"
        and event["tool_name"] == "write_file"
        and event["decision"] == "deny"
        and event["reason"] == "plan_mode_path_mismatch"
        for event in events
    )
    assert any(
        event["event"] == "permission_decision"
        and event["tool_name"] == "write_file"
        and event["decision"] == "allow"
        and event["reason"] == "plan_artifact_write"
        for event in events
    )


def test_repeated_plan_mode_denial_is_blocked_before_hitting_step_limit(tmp_path):
    bad_call = '<tool name="write_file" path="src.py"><content>print("no")\n</content></tool>'
    agent = build_agent(
        tmp_path,
        [
            bad_call,
            bad_call,
            bad_call,
            '<tool name="write_file" path=".forge/plans/repeat-plan.md"><content># Plan\n- Retry stopped.\n</content></tool>',
            "<final>Plan ready.</final>",
        ],
        max_steps=6,
    )
    agent.enter_plan_mode("repeat")

    assert agent.ask("verify repeated plan-mode denial handling") == "Plan ready."
    assert not (tmp_path / "src.py").exists()

    trace = [
        json.loads(line)
        for line in (agent.current_run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    write_events = [
        event
        for event in trace
        if event["event"] == "tool_executed" and event.get("name") == "write_file"
    ]

    assert [event.get("tool_error_code") for event in write_events[:3]] == [
        "plan_mode_path_mismatch",
        "repeated_identical_call",
        "repeated_identical_call",
    ]
    assert write_events[3]["status"] == "ok"


def test_plan_mode_does_not_allow_retargeting_active_plan_with_enter_tool(tmp_path):
    agent = build_agent(tmp_path)

    agent.enter_plan_mode("original")

    rejected = agent.run_tool(
        "enter_plan_mode", {"topic": "retarget", "path": "src.py"}
    )

    assert "plan mode" in rejected
    assert agent.plan_mode.plan_path == ".forge/plans/original-plan.md"


def test_plan_mode_rejects_arbitrary_workspace_plan_path(tmp_path):
    agent = build_agent(tmp_path)

    rejected = agent.run_tool(
        "enter_plan_mode", {"topic": "retarget", "path": "src/auth.py"}
    )

    assert "plan path must stay under .forge/plans/" in rejected
    assert agent.runtime_mode == "default"


def test_repl_plan_command_reports_bad_plan_path_without_crashing(tmp_path):
    agent = build_agent(tmp_path)

    handled, should_exit, output = handle_repl_command(
        agent, "/plan auth .forge/plans/../escape.md"
    )

    assert handled is True
    assert should_exit is False
    assert output == "error: plan path must stay under .forge/plans/"
    assert agent.runtime_mode == "default"
