import json
from pathlib import Path
from collections import Counter

import pytest

from forge.evaluation.evaluator import (
    BenchmarkEvaluator,
    load_benchmark,
    _python_c_verifier_args,
    run_harness_regression_v2,
    run_fixed_benchmark,
    summarize_bugfix_metrics,
    summarize_context_metrics,
    summarize_rows,
)


def test_load_benchmark_validates_fixed_schema():
    benchmark = load_benchmark(Path("benchmarks/coding_tasks.json"))

    assert benchmark["schema_version"] == 1
    assert len(benchmark["tasks"]) == 18
    assert Counter(task["category"] for task in benchmark["tasks"]) == {
        "documentation": 2,
        "text-edit": 2,
        "tool-boundary": 3,
        "recovery": 3,
        "durable-contract": 2,
        "context": 6,
    }
    for task in benchmark["tasks"]:
        assert {"id", "prompt", "fixture_repo", "allowed_tools", "step_budget", "expected_artifact", "verifier", "category"} <= set(task)
        assert isinstance(task["allowed_tools"], list)
        assert task["step_budget"] > 0


def test_load_bugfix_benchmark_validates_schema():
    benchmark = load_benchmark(Path("benchmarks/bugfix_tasks.json"))

    assert benchmark["schema_version"] == 1
    assert len(benchmark["tasks"]) == 8
    assert Counter(task["category"] for task in benchmark["tasks"]) == {
        "bugfix-verification": 8,
    }
    for task in benchmark["tasks"]:
        assert {"id", "prompt", "fixture_repo", "allowed_tools", "step_budget", "expected_artifact", "verifier", "category"} <= set(task)
        assert task["fixture_repo"] == "tests/fixtures/bench_repo_bugfix_py"

def test_load_benchmark_rejects_missing_required_task_fields(tmp_path):
    benchmark_path = tmp_path / "bad-benchmark.json"
    benchmark_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tasks": [
                    {
                        "id": "broken",
                        "prompt": "Missing required task keys.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="required"):
        load_benchmark(benchmark_path)


def test_run_fixed_benchmark_uses_fresh_fixture_copy_and_fresh_run_directory(tmp_path):
    artifact_path = tmp_path / "benchmark-v1.json"
    evaluator = BenchmarkEvaluator(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=artifact_path,
        workspace_root=tmp_path / "workspaces",
    )

    original_fixture = Path("tests/fixtures/bench_repo_patch/sample.txt").read_text(encoding="utf-8")
    artifact = evaluator.run()

    row = next(item for item in artifact["rows"] if item["id"] == "sample_beta_locked")
    copied_fixture = (tmp_path / "workspaces" / row["fixture_copy_relpath"]).resolve()
    run_dir = (tmp_path / "workspaces" / row["run_dir_relpath"]).resolve()

    assert artifact_path.exists()
    assert copied_fixture.exists()
    assert run_dir.exists()
    assert not row["fixture_copy_relpath"].startswith("/")
    assert not row["run_dir_relpath"].startswith("/")
    assert row["initial_history_empty"] is True
    assert row["initial_memory_empty"] is True
    assert row["initial_task_summary_empty"] is True
    assert Path("tests/fixtures/bench_repo_patch/sample.txt").read_text(encoding="utf-8") == original_fixture
    assert "beta-locked" in (copied_fixture / "sample.txt").read_text(encoding="utf-8")


def test_run_fixed_benchmark_reports_metadata_and_success_definition(tmp_path):
    artifact_path = tmp_path / "benchmark-v1.json"
    artifact = run_fixed_benchmark(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=artifact_path,
        workspace_root=tmp_path / "workspaces",
    )

    assert artifact_path.exists()
    persisted = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert persisted == artifact

    assert artifact["schema_version"] == 1
    runtime = artifact["runtime"]
    assert runtime["source_revision"]
    assert isinstance(runtime["source_tracked"], bool)
    assert runtime["forge_version"] == "0.3.0"
    assert runtime["python_version"]
    assert runtime["python_implementation"]
    assert runtime["platform"]
    if runtime["source_tracked"]:
        assert runtime["commit_sha"]
    else:
        assert runtime["commit_sha"] is None
        assert runtime["source_revision"] == "untracked-worktree"
    assert artifact["summary"] == {
        "total_tasks": 18,
        "passed": 18,
        "failed": 0,
        "pass_rate": 1.0,
        "within_budget": 18,
        "verifier_passes": 18,
        "within_budget_rate": 1.0,
        "verifier_pass_rate": 1.0,
        "failure_category_counts": {},
    }
    assert artifact["context_summary"]["row_count"] == 18
    assert artifact["context_summary"]["prompt_over_budget_count"] == 2
    assert artifact["context_summary"]["average_prompt_tokens"] > 0
    assert artifact["context_summary"]["max_prompt_tokens"] >= artifact["context_summary"]["average_prompt_tokens"]
    assert artifact["context_summary"]["average_history_tokens"] >= 0
    assert artifact["context_summary"]["average_memory_tokens"] >= 0
    assert artifact["context_summary"]["average_tool_output_tokens"] >= 0
    assert artifact["context_summary"]["max_total_estimated_tokens"] > 0
    assert artifact["context_summary"]["total_checkpoint_count"] >= 18
    assert artifact["context_summary"]["total_recovery_checkpoint_count"] >= 8
    assert artifact["context_summary"]["total_routine_checkpoint_count"] >= 18
    assert artifact["context_summary"]["total_context_reduction_checkpoint_count"] == 2
    assert artifact["failure_category_counts"] == {}

    reproducibility = artifact["reproducibility"]
    assert reproducibility["model_name"] == "ScriptedModelClient"
    assert reproducibility["model_version"] == "scripted-deterministic"
    assert reproducibility["fixture_snapshot_id"].startswith("sha256:")
    assert reproducibility["decoding"] == {
        "temperature": 0.0,
        "top_p": 1.0,
        "max_new_tokens": 64,
    }
    assert reproducibility["timezone"] == "Asia/Shanghai"
    assert reproducibility["locale"] == "C.UTF-8"

    for row in artifact["rows"]:
        assert not row["fixture_copy_relpath"].startswith("/")
        assert not row["run_dir_relpath"].startswith("/")
        assert not row["task_state_relpath"].startswith("/")
        assert not row["report_relpath"].startswith("/")
        assert row["status"] == "pass"
        assert row["passed"] is True
        assert row["within_budget"] is True
        assert row["verifier_passed"] is True
        assert row["expected_artifact_exists"] is True
        assert row["non_failure_stop_reason"] is True
        assert row["stop_reason"] == "final_answer_returned"
        assert row["context_metrics"]["prompt_chars"] > 0
        assert row["context_metrics"]["prompt_tokens"] > 0
        assert row["context_metrics"]["total_estimated_tokens"] > 0
        assert row["context_metrics"]["history_tokens"] >= 0
        assert row["context_metrics"]["memory_tokens"] >= 0
        assert row["context_metrics"]["tool_output_tokens"] >= 0
        assert row["context_metrics"]["compact_count"] == row["context_metrics"]["compaction_count"]
        assert row["context_metrics"]["repeated_tool_guard_count"] == row["context_metrics"]["repeated_tool_rejection_count"]
        assert row["context_metrics"]["checkpoint_count"] >= 1
        assert row["context_metrics"]["routine_checkpoint_count"] >= 0
        assert row["context_metrics"]["recovery_checkpoint_count"] >= 0


def test_run_fixed_benchmark_covers_recovery_and_durable_contract_rows(tmp_path):
    artifact = run_fixed_benchmark(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=tmp_path / "benchmark-v1.json",
        workspace_root=tmp_path / "workspaces",
    )

    context_row = next(item for item in artifact["rows"] if item["id"] == "context_reduction_checkpoint")
    durable_row = next(item for item in artifact["rows"] if item["id"] == "durable_promotion_reject")
    history_only_row = next(item for item in artifact["rows"] if item["id"] == "context_history_only_recall")
    old_history_row = next(item for item in artifact["rows"] if item["id"] == "context_old_history_compressed_recall")
    large_output_row = next(item for item in artifact["rows"] if item["id"] == "context_large_tool_output_reduction")
    relevant_memory_row = next(item for item in artifact["rows"] if item["id"] == "context_relevant_memory_selection")
    current_request_row = next(
        item for item in artifact["rows"] if item["id"] == "context_current_request_preserved_under_pressure"
    )
    stale_context_row = next(item for item in artifact["rows"] if item["id"] == "context_stale_summary_reanchor")

    trace_path = (tmp_path / "workspaces" / context_row["run_dir_relpath"] / "trace.jsonl").resolve()
    trace_events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]

    assert any(
        event.get("event") == "checkpoint_created" and event.get("trigger") == "context_reduction"
        for event in trace_events
    )
    assert context_row["context_metrics"]["budget_reduction_count"] > 0
    assert context_row["context_metrics"]["history_chars_saved"] > 0
    assert context_row["context_metrics"]["context_reduction_checkpoint_count"] == 1
    assert durable_row["report"]["durable_rejections"] == [
        "dependency-facts:secret_shaped",
        "key-decisions:transient_task_state",
    ]
    assert history_only_row["report"]["prompt_metadata"]["context_probe"]["history_fact_seen"] is True
    assert history_only_row["report"]["prompt_metadata"]["context_probe"]["memory_disabled"] is True
    assert old_history_row["report"]["prompt_metadata"]["context_probe"]["old_history_fact_seen"] is True
    assert old_history_row["report"]["prompt_metadata"]["history"]["older_entries_count"] > 0
    assert large_output_row["report"]["prompt_metadata"]["context_probe"]["large_raw_payload_absent"] is True
    assert large_output_row["report"]["prompt_metadata"]["history"]["reused_file_summary_count"] >= 1
    assert relevant_memory_row["report"]["prompt_metadata"]["context_probe"]["relevant_fact_seen"] is True
    assert relevant_memory_row["report"]["prompt_metadata"]["context_probe"]["irrelevant_fact_absent"] is True
    assert relevant_memory_row["report"]["prompt_metadata"]["relevant_memory"]["selected_notes"] == [
        "selected context fact is silver"
    ]
    assert current_request_row["report"]["prompt_metadata"]["context_probe"]["current_request_preserved"] is True
    assert stale_context_row["report"]["prompt_metadata"]["resume_status"] == "partial-stale"


def test_run_bugfix_benchmark_reports_guardrail_metrics(tmp_path):
    artifact = run_fixed_benchmark(
        benchmark_path=Path("benchmarks/bugfix_tasks.json"),
        artifact_path=tmp_path / "bugfix-v1.json",
        workspace_root=tmp_path / "workspaces",
    )

    assert artifact["summary"]["total_tasks"] == 8
    assert artifact["summary"]["pass_rate"] == 1.0
    summary = artifact["bugfix_summary"]
    assert summary["task_count"] == 8
    assert summary["strict_count"] == 6
    assert summary["soft_count"] == 1
    assert summary["inactive_count"] == 1
    assert summary["guard_trigger_rate"] > 0
    assert summary["false_guard_rate"] == 0
    assert summary["false_verification_accept_rate"] == 0
    assert summary["evidence_record_rate"] == 1.0

    docs_row = next(row for row in artifact["rows"] if row["id"] == "docs_fix_does_not_require_test")
    assert docs_row["report"]["bug_fix_ledger"]["mode"] == "soft"
    assert docs_row["report"]["bug_fix_ledger"]["guard_triggered"] is False

    echo_row = next(row for row in artifact["rows"] if row["id"] == "echo_test_does_not_satisfy_verification")
    assert echo_row["report"]["bug_fix_ledger"]["verification_command"].startswith("python -m pytest")
    assert echo_row["report"]["bug_fix_ledger"]["guard_triggered"] is True

    long_row = next(row for row in artifact["rows"] if row["id"] == "long_test_output_saved_and_ledger_links_artifact")
    ledger = long_row["report"]["bug_fix_ledger"]
    assert ledger["passing_artifact"]
    assert any(item["path"] == ledger["passing_artifact"] for item in long_row["report"]["output_artifacts"])


def test_summarize_bugfix_metrics_empty_for_non_bugfix_rows():
    assert summarize_bugfix_metrics([{"category": "documentation", "report": {}}]) == {}


def test_run_harness_regression_v2_writes_named_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "harness-regression-v2.json"

    artifact = run_harness_regression_v2(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=artifact_path,
        workspace_root=tmp_path / "workspaces",
    )

    assert artifact_path.exists()
    assert artifact["summary"]["total_tasks"] == 18
    assert artifact["summary"]["pass_rate"] == 1.0
    assert artifact["summary"]["within_budget_rate"] == 1.0
    assert artifact["summary"]["verifier_pass_rate"] == 1.0


def test_python_c_verifier_args_uses_current_interpreter():
    args = _python_c_verifier_args("python3 -c \"print('ok')\"")

    assert args[1:] == ["-c", "print('ok')"]


def test_run_task_anchors_paths_to_fixture_copy_even_inside_repo_workspace():
    evaluator = BenchmarkEvaluator(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=Path("docs/review-pack/benchmark-v1.json"),
        workspace_root=Path("."),
    )

    task = next(item for item in evaluator.load()["tasks"] if item["id"] == "readme_intro_locked")
    row = evaluator.run_task(task)

    assert row["status"] == "pass"
    fixture_copy = Path(row["fixture_copy_relpath"])
    readme_path = fixture_copy / "README.md"
    assert "This fixture is a locked benchmark workspace." in readme_path.read_text(encoding="utf-8")


def test_summarize_context_metrics_aggregates_context_health_rows():
    summary = summarize_context_metrics(
        [
            {
                "context_metrics": {
                    "prompt_over_budget": False,
                    "prompt_tokens": 100,
                    "history_tokens": 20,
                    "memory_tokens": 10,
                    "tool_output_tokens": 5,
                    "total_estimated_tokens": 100,
                    "prompt_chars": 400,
                    "budget_reduction_count": 1,
                    "history_chars_saved": 200,
                    "compact_count": 0,
                    "compaction_count": 0,
                    "checkpoint_count": 1,
                    "recovery_checkpoint_count": 1,
                    "routine_checkpoint_count": 0,
                    "context_reduction_checkpoint_count": 1,
                    "repeated_tool_guard_count": 0,
                    "repeated_tool_rejection_count": 0,
                }
            },
            {
                "context_metrics": {
                    "prompt_over_budget": True,
                    "prompt_tokens": 300,
                    "history_tokens": 60,
                    "memory_tokens": 30,
                    "tool_output_tokens": 15,
                    "total_estimated_tokens": 300,
                    "prompt_chars": 800,
                    "budget_reduction_count": 2,
                    "history_chars_saved": 50,
                    "compact_count": 1,
                    "compaction_count": 1,
                    "checkpoint_count": 2,
                    "recovery_checkpoint_count": 0,
                    "routine_checkpoint_count": 2,
                    "context_reduction_checkpoint_count": 0,
                    "repeated_tool_guard_count": 1,
                    "repeated_tool_rejection_count": 1,
                }
            },
        ]
    )

    assert summary == {
        "row_count": 2,
        "average_prompt_tokens": 200.0,
        "max_prompt_tokens": 300,
        "average_history_tokens": 40.0,
        "average_memory_tokens": 20.0,
        "average_tool_output_tokens": 10.0,
        "prompt_over_budget_count": 1,
        "average_total_estimated_tokens": 200.0,
        "max_total_estimated_tokens": 300,
        "average_prompt_chars": 600.0,
        "total_budget_reduction_count": 3,
        "total_history_chars_saved": 250,
        "total_compact_count": 1,
        "total_compaction_count": 1,
        "total_checkpoint_count": 3,
        "total_recovery_checkpoint_count": 1,
        "total_routine_checkpoint_count": 2,
        "total_context_reduction_checkpoint_count": 1,
        "total_repeated_tool_guard_count": 1,
        "total_repeated_tool_rejection_count": 1,
    }


def test_summarize_rows_counts_failure_categories():
    summary = summarize_rows(
        [
            {
                "status": "pass",
                "within_budget": True,
                "verifier_passed": True,
                "expected_artifact_exists": True,
                "non_failure_stop_reason": True,
            },
            {
                "status": "fail",
                "within_budget": False,
                "verifier_passed": False,
                "expected_artifact_exists": False,
                "non_failure_stop_reason": False,
                "failure_category": "verifier_failed",
            },
            {
                "status": "fail",
                "within_budget": False,
                "verifier_passed": True,
                "expected_artifact_exists": True,
                "non_failure_stop_reason": False,
                "failure_category": "budget_exceeded",
            },
        ]
    )

    assert summary["total_tasks"] == 3
    assert summary["passed"] == 1
    assert summary["failed"] == 2
    assert summary["pass_rate"] == pytest.approx(1 / 3)
    assert summary["within_budget"] == 1
    assert summary["verifier_passes"] == 2
    assert summary["failure_category_counts"] == {
        "budget_exceeded": 1,
        "verifier_failed": 1,
    }
