"""Lightweight structured result from a single Harness Run.

RunResult bridges the Engine (which generates run_id internally)
and the TaskStore (which needs run_id to link Runs to Tasks).

Engine.current_task_state remains the authoritative per-Run state;
RunResult is a minimal public projection for the outer CLI / TaskStore layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RunResult:
    """Public projection of a single Harness Run's outcome."""

    final_answer: str = ""
    run_id: str = ""
    legacy_engine_task_id: str = ""
    outcome: str = "completed"
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_task_state(cls, task_state) -> RunResult:
        """Construct a RunResult from a TaskState instance (engine.py)."""
        status = getattr(task_state, "status", "") or ""
        stop = getattr(task_state, "stop_reason", "") or ""
        if status == "completed":
            outcome = "completed"
        elif stop in ("step_limit_reached", "retry_limit_reached"):
            outcome = "stopped"
        elif status == "failed":
            outcome = "failed"
        else:
            outcome = "completed"

        return cls(
            final_answer=getattr(task_state, "final_answer", None) or "",
            run_id=getattr(task_state, "run_id", "") or "",
            legacy_engine_task_id=getattr(task_state, "task_id", "") or "",
            outcome=outcome,
        )
