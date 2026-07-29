"""PR 1 Run 启动回调的确定性集成测试。"""

from forge.core.workspace import WorkspaceContext
from forge.core.runtime import Pico
from forge.core.session_store import SessionStore
from forge.testing import ScriptedModelClient


def _build_agent(tmp_path, outputs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=ScriptedModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path, repo_root_override=tmp_path),
        session_store=SessionStore(tmp_path / ".forge" / "sessions"),
        approval_policy="auto",
    )


def test_ask_run_calls_start_callback_before_model_execution(tmp_path):
    agent = _build_agent(tmp_path, ["<final>done</final>"])
    events = []

    def on_started(run_id, legacy_task_id):
        # Run artifact 尚未创建，证明回调在 Engine 实际执行前发生。
        events.append((run_id, legacy_task_id, agent.current_run_dir))

    result = agent.ask_run("perform work", on_run_started=on_started)

    assert result.final_answer == "done"
    assert events == [(result.run_id, result.legacy_engine_task_id, None)]


def test_ask_run_does_not_execute_when_start_callback_fails(tmp_path):
    agent = _build_agent(tmp_path, ["<final>must not run</final>"])

    def reject_start(run_id, legacy_task_id):
        raise RuntimeError("durable persistence failed")

    try:
        agent.ask_run("perform work", on_run_started=reject_start)
        assert False, "expected callback failure"
    except RuntimeError as exc:
        assert str(exc) == "durable persistence failed"

    assert agent.model_client.prompts == []
