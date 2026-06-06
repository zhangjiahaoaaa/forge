import json

from forge import Pico, SessionStore, WorkspaceContext
from forge.testing import ScriptedModelClient


def build_agent(tmp_path, outputs, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=ScriptedModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        **kwargs,
    )


def read_jsonl(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_hooks_cover_model_tool_checkpoint_and_final_lifecycle(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="notes/result.txt"><content>ok\n</content></tool>',
            "<final>Wrote it.</final>",
        ],
    )
    calls = []

    def record(context):
        calls.append(
            (
                context["hook_name"],
                context.get("parsed_kind") or context.get("tool_name") or context.get("trigger") or context.get("final_answer"),
            )
        )

    for name in (
        "before_model",
        "after_model",
        "before_tool",
        "after_tool",
        "on_checkpoint",
        "on_final",
    ):
        agent.register_hook(name, record)

    answer = agent.ask("create the result file")

    assert answer == "Wrote it."
    assert calls == [
        ("before_model", None),
        ("after_model", "tool"),
        ("before_tool", "write_file"),
        ("after_tool", "write_file"),
        ("on_checkpoint", "tool_executed"),
        ("before_model", None),
        ("after_model", "final"),
        ("on_checkpoint", "run_finished"),
        ("on_final", "Wrote it."),
    ]


def test_hook_failure_is_isolated_and_recorded(tmp_path):
    agent = build_agent(tmp_path, ["<final>Done.</final>"])

    def broken(_context):
        raise RuntimeError("hook exploded")

    agent.register_hook("on_final", broken)

    answer = agent.ask("finish the task")

    assert answer == "Done."
    failures = agent.hook_failures()
    assert len(failures) == 1
    assert failures[0]["hook_name"] == "on_final"
    assert failures[0]["error_type"] == "RuntimeError"
    report = json.loads((agent.current_run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["hook_failures"][0]["hook_name"] == "on_final"
    persisted = read_jsonl(agent.session_event_bus.path)
    assert any(event["event"] == "hook_failed" for event in persisted)


def test_on_error_hook_runs_for_retryable_model_failures(tmp_path):
    from forge.providers import ProviderError

    agent = build_agent(
        tmp_path,
        [
            ProviderError(
                "empty provider response",
                provider="anthropic",
                model="deepseek-v4-pro",
                base_url="https://api.deepseek.com/anthropic/v1",
                code="empty_response",
                retryable=False,
            ),
            "<final>Recovered.</final>",
        ],
    )
    seen = []

    def capture(context):
        seen.append(
            (
                context["hook_name"],
                bool(context.get("will_retry")),
                context.get("error_metadata", {}).get("code"),
            )
        )

    agent.register_hook("on_error", capture)

    answer = agent.ask("recover from provider empty response")

    assert answer == "Recovered."
    assert seen == [("on_error", True, "empty_response")]
