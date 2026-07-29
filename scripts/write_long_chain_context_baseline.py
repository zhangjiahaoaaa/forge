#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forge.evaluation.metrics import write_long_chain_context_baseline_artifacts  # noqa: E402


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Generate Forge long-chain context baseline artifacts.")
    parser.add_argument(
        "--output-json",
        default="artifacts/long-chain-context-baseline.json",
        help="Path to output long-chain context baseline JSON.",
    )
    parser.add_argument(
        "--output-markdown",
        default="artifacts/long-chain-context-baseline.md",
        help="Path to output long-chain context baseline Markdown.",
    )
    parser.add_argument(
        "--benchmark",
        default="benchmarks/long_chain_context_tasks.json",
        help="Path to long-chain benchmark task JSON.",
    )
    parser.add_argument(
        "--workspace-root",
        default=None,
        help="Optional workspace root for temporary benchmark fixture copies.",
    )
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    baseline = write_long_chain_context_baseline_artifacts(
        json_path=Path(args.output_json),
        markdown_path=Path(args.output_markdown),
        benchmark_path=Path(args.benchmark),
        workspace_root=Path(args.workspace_root) if args.workspace_root else None,
    )
    summary = baseline.get("summary", {})
    context = baseline.get("context_summary", {})
    print(
        "long-chain context baseline written: "
        f"tasks={summary.get('total_tasks', 0)} "
        f"pass_rate={float(summary.get('pass_rate', 0.0)):.2%} "
        f"average_prompt_tokens={float(context.get('average_prompt_tokens', 0.0)):.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
