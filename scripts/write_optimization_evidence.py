#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forge.evaluation.metrics import (  # noqa: E402
    run_context_ablation_v2,
    run_memory_ablation_v2,
    run_recovery_ablation_v2,
    write_benchmark_core_report,
    write_context_baseline_artifacts,
)


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Generate Forge optimization evidence artifacts for context, memory, and recovery baselines."
    )
    parser.add_argument(
        "--artifact-dir",
        default="artifacts",
        help="Directory for generated JSON/Markdown artifacts.",
    )
    parser.add_argument(
        "--report-path",
        default="docs/metrics/forge-benchmark-core-report.md",
        help="Path to the generated benchmark core Markdown report.",
    )
    parser.add_argument(
        "--context-repetitions",
        type=int,
        default=1,
        help="Repetitions for context ablation. Use higher values for final evidence.",
    )
    parser.add_argument(
        "--memory-repetitions",
        type=int,
        default=1,
        help="Repetitions for memory ablation. Use higher values for final evidence.",
    )
    parser.add_argument(
        "--recovery-repetitions",
        type=int,
        default=1,
        help="Repetitions for recovery ablation. Use higher values for final evidence.",
    )
    parser.add_argument(
        "--workspace-root",
        default=None,
        help="Optional workspace root for harness fixture copies.",
    )
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    artifact_dir = Path(args.artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    harness_path = artifact_dir / "harness-regression-v2.json"
    context_baseline_json = artifact_dir / "context-baseline.json"
    context_baseline_md = artifact_dir / "context-baseline.md"
    context_ablation_path = artifact_dir / "context-ablation-v2.json"
    memory_ablation_path = artifact_dir / "memory-ablation-v2.json"
    recovery_ablation_path = artifact_dir / "recovery-ablation-v2.json"

    baseline = write_context_baseline_artifacts(
        json_path=context_baseline_json,
        markdown_path=context_baseline_md,
        harness_artifact_path=harness_path,
        workspace_root=Path(args.workspace_root) if args.workspace_root else None,
    )
    context = run_context_ablation_v2(
        artifact_path=context_ablation_path,
        repetitions=args.context_repetitions,
    )
    memory = run_memory_ablation_v2(
        artifact_path=memory_ablation_path,
        repetitions=args.memory_repetitions,
    )
    recovery = run_recovery_ablation_v2(
        artifact_path=recovery_ablation_path,
        repetitions=args.recovery_repetitions,
    )
    report = write_benchmark_core_report(
        report_path=Path(args.report_path),
        harness_artifact_path=harness_path,
        context_artifact_path=context_ablation_path,
        memory_artifact_path=memory_ablation_path,
        recovery_artifact_path=recovery_ablation_path,
    )

    summary = baseline.get("summary", {})
    context_summary = baseline.get("context_summary", {})
    print("optimization evidence written:")
    print(f"- harness: {harness_path}")
    print(f"- context baseline json: {context_baseline_json}")
    print(f"- context baseline markdown: {context_baseline_md}")
    print(f"- context ablation: {context_ablation_path} configs={context.get('config_count', 0)}")
    print(f"- memory ablation: {memory_ablation_path} tasks={memory.get('task_count', 0)}")
    print(f"- recovery ablation: {recovery_ablation_path} tasks={recovery.get('task_count', 0)}")
    print(f"- core report: {args.report_path} lines={len(report.splitlines())}")
    print(
        "- summary: "
        f"tasks={summary.get('total_tasks', 0)} "
        f"pass_rate={float(summary.get('pass_rate', 0.0)):.2%} "
        f"average_prompt_tokens={float(context_summary.get('average_prompt_tokens', 0.0)):.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
