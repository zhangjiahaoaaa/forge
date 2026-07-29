from pathlib import Path


def test_core_modules_stay_below_entropy_budget():
    root = Path(__file__).resolve().parents[1]
    budgets = {
        "forge/core/runtime.py": 1050,
        "forge/core/runtime_events.py": 90,
        "forge/core/runtime_consumers.py": 90,
        "forge/core/artifacts.py": 130,
        "forge/core/task_state.py": 140,
        "forge/core/todo_ledger.py": 120,
        "forge/core/worker_manager.py": 220,
        "forge/core/context_manager.py": 420,
        "forge/core/context_usage.py": 120,
        "forge/core/compact.py": 180,
        "forge/core/engine.py": 570,
        "forge/core/model_errors.py": 120,
        "forge/core/permissions.py": 140,
        "forge/core/tool_policy.py": 90,
        "forge/core/plan_mode.py": 140,
        "forge/core/tool_executor.py": 260,
        "forge/core/tool_profiles.py": 80,
        "forge/core/turn_history.py": 250,
        "forge/features/skills.py": 220,
        "forge/features/skills_bundled.py": 120,
        "forge/features/skills_runtime.py": 140,
        "forge/tools/registry.py": 360,
        "forge/tools/todos.py": 80,
        "forge/tools/agents.py": 90,
    }

    for relative_path, max_lines in budgets.items():
        line_count = len((root / relative_path).read_text(encoding="utf-8").splitlines())
        assert line_count <= max_lines, f"{relative_path} has {line_count} lines, budget is {max_lines}"
