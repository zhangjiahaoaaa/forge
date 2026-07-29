import os
import subprocess
import sys
from unittest.mock import patch

from forge.evaluation.metrics import (
    _provider_profile,
    run_context_ablation_v2,
    run_memory_ablation_v2,
    run_recovery_ablation_v2,
    write_benchmark_core_report,
    write_context_baseline_artifacts,
)


def test_write_context_baseline_artifacts_writes_json_and_markdown(tmp_path):
    json_path = tmp_path / "artifacts" / "context-baseline.json"
    md_path = tmp_path / "artifacts" / "context-baseline.md"

    baseline = write_context_baseline_artifacts(json_path=json_path, markdown_path=md_path)

    assert json_path.exists()
    assert md_path.exists()
    assert baseline["artifact_type"] == "context-baseline-v1"
    assert baseline["summary"]["total_tasks"] == 18
    assert baseline["context_summary"]["row_count"] == 18
    assert baseline["context_summary"]["average_prompt_tokens"] > 0
    text = md_path.read_text(encoding="utf-8")
    assert "# Forge Context Baseline" in text
    assert "average_prompt_tokens" in text
    assert "current_request_preserved" in text


def test_write_context_baseline_script_generates_artifacts(tmp_path):
    json_path = tmp_path / "artifacts" / "context-baseline.json"
    md_path = tmp_path / "artifacts" / "context-baseline.md"
    harness_path = tmp_path / "artifacts" / "harness-regression-v2.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/write_context_baseline.py",
            "--output-json",
            str(json_path),
            "--output-markdown",
            str(md_path),
            "--harness-artifact",
            str(harness_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert json_path.exists()
    assert md_path.exists()
    assert harness_path.exists()
    assert "context baseline written" in result.stdout


def test_write_optimization_evidence_script_generates_core_artifacts(tmp_path):
    artifact_dir = tmp_path / "artifacts"
    report_path = tmp_path / "docs" / "metrics" / "forge-benchmark-core-report.md"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/write_optimization_evidence.py",
            "--artifact-dir",
            str(artifact_dir),
            "--report-path",
            str(report_path),
            "--context-repetitions",
            "1",
            "--memory-repetitions",
            "1",
            "--recovery-repetitions",
            "1",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert (artifact_dir / "harness-regression-v2.json").exists()
    assert (artifact_dir / "context-baseline.json").exists()
    assert (artifact_dir / "context-baseline.md").exists()
    assert (artifact_dir / "context-ablation-v2.json").exists()
    assert (artifact_dir / "memory-ablation-v2.json").exists()
    assert (artifact_dir / "recovery-ablation-v2.json").exists()
    assert report_path.exists()
    assert "optimization evidence written" in result.stdout


def test_provider_profile_uses_project_toml_before_legacy_pico_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".pico.toml").write_text(
        "\n".join(
            [
                "[providers.deepseek]",
                'protocol = "anthropic"',
                'api_key = "sk-project-deepseek"',
                'model = "deepseek-v4-pro"',
                'base_url = "https://api.deepseek.com/anthropic"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with patch.dict(
        os.environ,
        {
            "PICO_DEEPSEEK_API_KEY": "sk-legacy-deepseek",
            "PICO_DEEPSEEK_MODEL": "legacy-deepseek-model",
            "PICO_DEEPSEEK_API_BASE": "https://legacy.deepseek.example/anthropic",
        },
        clear=True,
    ):
        profile = _provider_profile("deepseek")

    assert profile["status"] == "ready"
    assert profile["api_key"] == "sk-project-deepseek"
    assert profile["model"] == "deepseek-v4-pro"
    assert profile["base_url"] == "https://api.deepseek.com/anthropic"


def test_run_memory_ablation_v2_writes_expected_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "memory-ablation-v2.json"

    artifact = run_memory_ablation_v2(
        artifact_path=artifact_path,
        repetitions=1,
    )

    assert artifact_path.exists()
    assert artifact["artifact_type"] == "memory-ablation-v2"
    assert artifact["task_count"] == 12
    assert set(artifact["variants"]) == {"memory_on", "memory_off", "memory_irrelevant"}
    assert "memory_hit_rate" in artifact["variants"]["memory_on"]


def test_run_recovery_ablation_v2_writes_expected_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "recovery-ablation-v2.json"

    artifact = run_recovery_ablation_v2(
        artifact_path=artifact_path,
        repetitions=1,
    )

    assert artifact_path.exists()
    assert artifact["artifact_type"] == "recovery-ablation-v2"
    assert artifact["task_count"] == 10
    assert set(artifact["variants"]) == {"resume_enabled", "resume_disabled"}
    assert set(artifact["variants"]["resume_enabled"]["summary"]) >= {
        "resume_success_rate",
        "stale_reanchor_rate",
        "workspace_drift_detection_rate",
        "resume_false_accept_rate",
    }


def test_write_benchmark_core_report_marks_resume_safe_metrics(tmp_path):
    run_context_ablation_v2(tmp_path / "artifacts" / "context-ablation-v2.json", repetitions=1)
    run_memory_ablation_v2(tmp_path / "artifacts" / "memory-ablation-v2.json", repetitions=1)
    run_recovery_ablation_v2(tmp_path / "artifacts" / "recovery-ablation-v2.json", repetitions=1)
    harness_artifact_path = tmp_path / "artifacts" / "harness-regression-v2.json"
    harness_artifact_path.write_text(
        '{"summary":{"total_tasks":12,"pass_rate":1.0,"within_budget_rate":1.0,"verifier_pass_rate":1.0},"context_summary":{"average_prompt_tokens":1200.0,"average_memory_tokens":300.0,"total_recovery_checkpoint_count":2,"total_routine_checkpoint_count":10},"failure_category_counts":{}}',
        encoding="utf-8",
    )

    report_path = tmp_path / "docs" / "metrics" / "forge-benchmark-core-report.md"
    report_text = write_benchmark_core_report(
        report_path=report_path,
        harness_artifact_path=harness_artifact_path,
        context_artifact_path=tmp_path / "artifacts" / "context-ablation-v2.json",
        memory_artifact_path=tmp_path / "artifacts" / "memory-ablation-v2.json",
        recovery_artifact_path=tmp_path / "artifacts" / "recovery-ablation-v2.json",
    )

    assert report_path.exists()
    assert "可以安全写进简历的指标" in report_text
    assert "只适合放文档/面试展开的指标" in report_text
    assert "resume_success_rate" in report_text
    assert "memory_hit_rate" in report_text
    assert "average_memory_tokens" in report_text
    assert "recovery_checkpoint_count" in report_text
    assert "routine_checkpoint_count" in report_text
