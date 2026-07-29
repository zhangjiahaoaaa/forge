import hashlib
import json
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..features import memory as memorylib
from ..testing import ScriptedModelClient
from ..core.runtime import Pico, SessionStore
from ..core.run_store import RunStore
from ..core.task_state import STOP_REASON_FINAL_ANSWER_RETURNED
from ..paths import WORKSPACE_STATE_DIRNAME
from ..core.context_usage import estimate_tokens
from ..core.workspace import WorkspaceContext

BENCHMARK_SCHEMA_VERSION = 1
DEFAULT_BENCHMARK_PATH = Path("benchmarks/coding_tasks.json")
DEFAULT_ARTIFACT_PATH = Path("benchmarks/benchmark-v1.json")
DEFAULT_HARNESS_REGRESSION_V2_ARTIFACT_PATH = Path("artifacts/harness-regression-v2.json")
DEFAULT_MODEL_NAME = "ScriptedModelClient"
DEFAULT_MODEL_VERSION = "scripted-deterministic"
DEFAULT_TEMPERATURE = 0.0
DEFAULT_TOP_P = 1.0
DEFAULT_MAX_NEW_TOKENS = 64
DEFAULT_TIMEZONE = "Asia/Shanghai"

REQUIRED_BENCHMARK_KEYS = ("schema_version", "tasks")
REQUIRED_TASK_KEYS = (
    "id",
    "prompt",
    "fixture_repo",
    "allowed_tools",
    "step_budget",
    "expected_artifact",
    "verifier",
    "category",
)

TASK_FIXTURE_ARTIFACTS = {
    "bench_repo_readme": "README.md",
    "bench_repo_patch": "sample.txt",
    "bench_repo_bugfix_py": "calculator.py",
}

SCRIPTED_MODEL_OUTPUTS = {
    "readme_intro_locked": [
        '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":20}}</tool>',
        '<tool name="patch_file" path="README.md"><old_text>This is a placeholder benchmark fixture.</old_text><new_text>This fixture is a locked benchmark workspace.</new_text></tool>',
        "<final>Done.</final>",
    ],
    "readme_schema_note": [
        '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":20}}</tool>',
        '<tool name="patch_file" path="README.md"><old_text>- Placeholder note about the repo.</old_text><new_text>- The benchmark schema and baseline are fixed.</new_text></tool>',
        "<final>Done.</final>",
    ],
    "readme_ordering_note": [
        '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":20}}</tool>',
        '<tool name="patch_file" path="README.md"><old_text>- Placeholder note about the file layout.</old_text><new_text>- Deterministic file ordering keeps benchmark diffs stable.</new_text></tool>',
        "<final>Done.</final>",
    ],
    "sample_beta_locked": [
        '<tool>{"name":"read_file","args":{"path":"sample.txt","start":1,"end":20}}</tool>',
        '<tool name="patch_file" path="sample.txt"><old_text>beta</old_text><new_text>beta-locked</new_text></tool>',
        "<final>Done.</final>",
    ],
    "sample_gamma_locked": [
        '<tool>{"name":"read_file","args":{"path":"sample.txt","start":1,"end":20}}</tool>',
        '<tool name="patch_file" path="sample.txt"><old_text>gamma</old_text><new_text>gamma-locked</new_text></tool>',
        "<final>Done.</final>",
    ],
    "sample_placeholder_delta": [
        '<tool>{"name":"read_file","args":{"path":"sample.txt","start":1,"end":20}}</tool>',
        '<tool name="patch_file" path="sample.txt"><old_text>placeholder</old_text><new_text>delta</new_text></tool>',
        "<final>Done.</final>",
    ],
    "invalid_patch_recovery": [
        '<tool>{"name":"patch_file","args":{"path":"README.md","old_text":"This is a placeholder benchmark fixture."}}</tool>',
        '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":20}}</tool>',
        '<tool name="patch_file" path="README.md"><old_text>This is a placeholder benchmark fixture.</old_text><new_text>This fixture recovered after invalid patch args.</new_text></tool>',
        "<final>Done.</final>",
    ],
    "path_escape_recovery": [
        '<tool>{"name":"read_file","args":{"path":"../outside.txt","start":1,"end":1}}</tool>',
        '<tool>{"name":"read_file","args":{"path":"sample.txt","start":1,"end":20}}</tool>',
        '<tool name="patch_file" path="sample.txt"><old_text>alpha</old_text><new_text>alpha-guarded</new_text></tool>',
        "<final>Done.</final>",
    ],
    "repeated_read_recovery": [
        '<tool>{"name":"read_file","args":{"path":"sample.txt","start":1,"end":4}}</tool>',
        '<tool>{"name":"read_file","args":{"path":"sample.txt","start":1,"end":4}}</tool>',
        '<tool>{"name":"read_file","args":{"path":"sample.txt","start":1,"end":4}}</tool>',
        '<tool name="patch_file" path="sample.txt"><old_text>placeholder</old_text><new_text>repeat-guarded</new_text></tool>',
        "<final>Done.</final>",
    ],
    "context_reduction_checkpoint": [
        "<final>Done.</final>",
    ],
    "freshness_reanchor_resume": [
        "<final>Done.</final>",
    ],
    "workspace_mismatch_resume": [
        "<final>Done.</final>",
    ],
    "durable_promotion_accept": [
        "<final>Project convention: Preserve benchmark regression artifacts under artifacts/.\nDecision: Keep harness regression deterministic and reproducible.</final>",
    ],
    "durable_promotion_reject": [
        "<final>Project convention: Keep verifier outcomes stable across reruns.\nDependency: API key is sk-benchmark-secret.\nDecision: Current goal is debug the harness.</final>",
    ],
    "context_stale_summary_reanchor": [
        "<final>Done.</final>",
    ],
    "bugfix_requires_verification_after_patch": [
        '<tool>{"name":"read_file","args":{"path":"calculator.py","start":1,"end":40}}</tool>',
        '<tool name="patch_file" path="calculator.py"><old_text>def add(a, b):\n    return a - b</old_text><new_text>def add(a, b):\n    return a + b</new_text></tool>',
        "<final>Fixed.</final>",
        '<tool>{"name":"run_shell","args":{"command":"python verify_calculator.py","timeout":60}}</tool>',
        "<final>Fixed and verified.</final>",
    ],
    "bugfix_failed_verification_keeps_guard": [
        '<tool>{"name":"read_file","args":{"path":"calculator.py","start":1,"end":40}}</tool>',
        '<tool name="patch_file" path="calculator.py"><old_text>def add(a, b):\n    return a - b</old_text><new_text>def add(a, b):\n    return a * b</new_text></tool>',
        '<tool>{"name":"run_shell","args":{"command":"python verify_calculator.py","timeout":60}}</tool>',
        "<final>Fixed.</final>",
        '<tool name="patch_file" path="calculator.py"><old_text>def add(a, b):\n    return a * b</old_text><new_text>def add(a, b):\n    return a + b</new_text></tool>',
        '<tool>{"name":"run_shell","args":{"command":"python verify_calculator.py","timeout":60}}</tool>',
        "<final>Fixed and verified.</final>",
    ],
    "bugfix_records_reproduction_before_patch": [
        '<tool>{"name":"run_shell","args":{"command":"python verify_calculator.py","timeout":60}}</tool>',
        '<tool>{"name":"read_file","args":{"path":"calculator.py","start":1,"end":40}}</tool>',
        '<tool name="patch_file" path="calculator.py"><old_text>def add(a, b):\n    return a - b</old_text><new_text>def add(a, b):\n    return a + b</new_text></tool>',
        '<tool>{"name":"run_shell","args":{"command":"python verify_calculator.py","timeout":60}}</tool>',
        "<final>Reproduced, fixed, and verified.</final>",
    ],
    "docs_fix_does_not_require_test": [
        '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":20}}</tool>',
        '<tool name="patch_file" path="README.md"><old_text>typoo for the docs-only benchmark</old_text><new_text>typo for the docs-only benchmark</new_text></tool>',
        "<final>README typo fixed.</final>",
    ],
    "non_bug_feature_edit_not_guarded": [
        '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":20}}</tool>',
        '<tool name="patch_file" path="README.md"><old_text>The calculator module is intentionally tiny so bugfix verification tasks can focus on the agent runtime guardrails.</old_text><new_text>The calculator module is intentionally tiny so bugfix verification tasks can focus on the agent runtime guardrails.\n\nUsage note: run tests before release.</new_text></tool>',
        "<final>Usage note added.</final>",
    ],
    "echo_test_does_not_satisfy_verification": [
        '<tool>{"name":"read_file","args":{"path":"calculator.py","start":1,"end":40}}</tool>',
        '<tool name="patch_file" path="calculator.py"><old_text>def add(a, b):\n    return a - b</old_text><new_text>def add(a, b):\n    return a + b</new_text></tool>',
        '<tool>{"name":"run_shell","args":{"command":"echo test","timeout":20}}</tool>',
        "<final>Fixed.</final>",
        '<tool>{"name":"run_shell","args":{"command":"python verify_calculator.py","timeout":60}}</tool>',
        "<final>Fixed and verified.</final>",
    ],
    "py_compile_counts_as_verification": [
        '<tool>{"name":"read_file","args":{"path":"calculator.py","start":1,"end":40}}</tool>',
        '<tool name="patch_file" path="calculator.py"><old_text>def add(a, b):\n    return a - b</old_text><new_text>def add(a, b):\n    return a + b</new_text></tool>',
        '<tool>{"name":"run_shell","args":{"command":"python -m py_compile calculator.py","timeout":60}}</tool>',
        "<final>Fixed and compile verified.</final>",
    ],
    "long_test_output_saved_and_ledger_links_artifact": [
        '<tool>{"name":"read_file","args":{"path":"calculator.py","start":1,"end":40}}</tool>',
        '<tool name="patch_file" path="calculator.py"><old_text>def add(a, b):\n    return a - b</old_text><new_text>def add(a, b):\n    return a + b</new_text></tool>',
        '<tool>{"name":"run_shell","args":{"command":"python verify_calculator.py --verbose","timeout":60}}</tool>',
        "<final>Fixed and verbose verification saved.</final>",
    ],
    "long_chain_action_fix_after_failure": [
        '<tool>{"name":"read_file","args":{"path":"sample.txt","start":1,"end":20}}</tool>',
        '<tool name="patch_file" path="sample.txt"><old_text>beta</old_text><new_text>beta-locked</new_text></tool>',
        '<tool>{"name":"run_shell","args":{"command":"python -c \\\"from pathlib import Path; text = Path(\'sample.txt\').read_text(encoding=\'utf-8\'); assert \'beta-locked\' in text; print(\'PASS long-chain sample repair\')\\\"","timeout":60}}</tool>',
        "<final>Long-chain sample repair verified.</final>",
    ],
    "long_chain_action_stale_reanchor_then_patch": [
        '<tool>{"name":"read_file","args":{"path":"sample.txt","start":1,"end":20}}</tool>',
        '<tool name="patch_file" path="sample.txt"><old_text>stale-target</old_text><new_text>reanchored-target</new_text></tool>',
        "<final>Re-anchored and patched current file state.</final>",
    ],
    "long_chain_action_constraint_no_forbidden_edit": [
        '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":40}}</tool>',
        '<tool name="patch_file" path="README.md"><old_text>- Placeholder note about the repo.</old_text><new_text>- Long-chain constrained note is allowed.</new_text></tool>',
        "<final>Constrained README note updated.</final>",
    ],
    "long_chain_action_failed_verification_then_repair": [
        '<tool>{"name":"read_file","args":{"path":"calculator.py","start":1,"end":40}}</tool>',
        '<tool name="patch_file" path="calculator.py"><old_text>def add(a, b):\n    return a - b</old_text><new_text>def add(a, b):\n    return a * b</new_text></tool>',
        '<tool>{"name":"run_shell","args":{"command":"python verify_calculator.py","timeout":60}}</tool>',
        "<final>Fixed.</final>",
        '<tool name="patch_file" path="calculator.py"><old_text>def add(a, b):\n    return a * b</old_text><new_text>def add(a, b):\n    return a + b</new_text></tool>',
        '<tool>{"name":"run_shell","args":{"command":"python verify_calculator.py","timeout":60}}</tool>',
        "<final>Fixed and verified.</final>",
    ],
}

CONTEXT_PROBE_TASK_IDS = {
    "context_history_only_recall",
    "context_old_history_compressed_recall",
    "context_large_tool_output_reduction",
    "context_relevant_memory_selection",
    "context_current_request_preserved_under_pressure",
}

LONG_CHAIN_PROBE_TASK_IDS = {
    "long_chain_next_step_after_failure",
    "long_chain_stale_summary_reanchor",
    "long_chain_user_constraint_preserved",
}


def _prompt_section(prompt, header, next_header):
    text = str(prompt)
    if header not in text:
        return ""
    section = text.split(header, 1)[1]
    if next_header in section:
        section = section.split(next_header, 1)[0]
    return section


class _ContextProbeModelClient(ScriptedModelClient):
    def __init__(self, task):
        super().__init__([])
        self.task = dict(task)

    def complete(self, prompt, max_new_tokens, **kwargs):
        del max_new_tokens, kwargs
        self.prompts.append(prompt)
        task_id = self.task["id"]
        prompt_lower = prompt.lower()
        transcript = _prompt_section(prompt_lower, "transcript:", "\n\ncurrent user request:")
        relevant = _prompt_section(prompt_lower, "relevant memory:", "\n\ntranscript:")
        final = "Done."
        probe = {"task_id": task_id}

        if task_id == "context_history_only_recall":
            fact = "history-only fact is amber"
            probe["history_fact_seen"] = fact in transcript
            probe["memory_disabled"] = "memory:\n- disabled" in prompt_lower
            final = f"{fact}." if probe["history_fact_seen"] else "missing history fact."
        elif task_id == "context_old_history_compressed_recall":
            fact = "old-compressed fact is cedar"
            probe["old_history_fact_seen"] = fact in transcript
            final = f"{fact}." if probe["old_history_fact_seen"] else "missing old history fact."
        elif task_id == "context_large_tool_output_reduction":
            probe["large_raw_payload_absent"] = "large_raw_payload" not in transcript
            probe["large_summary_seen"] = "compact-safe summary marker" in transcript
            final = "large output reduced." if probe["large_raw_payload_absent"] and probe["large_summary_seen"] else "large output leaked."
        elif task_id == "context_relevant_memory_selection":
            probe["relevant_fact_seen"] = "selected context fact is silver" in relevant
            probe["irrelevant_fact_absent"] = "irrelevant context fact is orange" not in relevant
            final = "relevant memory selected." if probe["relevant_fact_seen"] and probe["irrelevant_fact_absent"] else "relevant memory mismatch."
        elif task_id == "context_current_request_preserved_under_pressure":
            expected_tail = f"current user request:\n{str(self.task['prompt']).lower()}"
            probe["current_request_preserved"] = prompt_lower.endswith(expected_tail)
            probe["pressure_sentinel_seen"] = "pressure-request-sentinel-omega" in prompt_lower
            final = "current request preserved." if probe["current_request_preserved"] else "current request clipped."

        self.last_completion_metadata = {"context_probe": probe}
        return f"<final>{final}</final>"


class _LongChainProbeModelClient(ScriptedModelClient):
    def __init__(self, task):
        super().__init__([])
        self.task = dict(task)

    def complete(self, prompt, max_new_tokens, **kwargs):
        del max_new_tokens, kwargs
        self.prompts.append(prompt)
        task_id = self.task["id"]
        prompt_lower = prompt.lower()
        current = _prompt_section(prompt_lower, "current user request:", "__never__")
        probe = {"task_id": task_id}
        final = "long-chain probe missing state."

        if task_id == "long_chain_next_step_after_failure":
            probe["last_failure_seen"] = "long-chain failure sentinel expected beta-locked but saw beta" in prompt_lower
            probe["changed_file_seen"] = "sample.txt" in prompt_lower and "changed" in prompt_lower
            probe["next_step_seen"] = "next step is inspect sample.txt before final" in prompt_lower
            probe["verification_constraint_seen"] = "must run verification before final" in prompt_lower
            if probe["last_failure_seen"] and probe["changed_file_seen"] and probe["next_step_seen"] and probe["verification_constraint_seen"]:
                final = "long-chain failure state preserved."
        elif task_id == "long_chain_stale_summary_reanchor":
            probe["current_request_seen"] = "checking freshness" in current
            probe["stale_summary_seen"] = "stale long-chain summary" in prompt_lower
            if probe["current_request_seen"]:
                final = "long-chain stale summary reanchored."
        elif task_id == "long_chain_user_constraint_preserved":
            probe["current_constraint_seen"] = "do not modify readme public api wording" in current
            probe["history_constraint_seen"] = "do not modify readme public api wording" in prompt_lower
            if probe["current_constraint_seen"] and probe["history_constraint_seen"]:
                final = "long-chain user constraint preserved."

        self.last_completion_metadata = {"long_chain_probe": probe}
        return f"<final>{final}</final>"

def _git_value(args, fallback="", cwd=None):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or Path.cwd(),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip() or fallback
    except Exception:
        return fallback


def _runtime_provenance(repo_root):
    """收集可复现 benchmark 所需的运行时和代码来源信息。"""
    from .._version import __version__

    commit_sha = _git_value(["rev-parse", "HEAD"], cwd=repo_root)
    branch = _git_value(["branch", "--show-current"], cwd=repo_root)
    # 没有 Git 元数据时明确说明不可追溯，避免空字符串被误认为有效提交。
    source_revision = commit_sha or "untracked-worktree"
    return {
        "commit_sha": commit_sha or None,
        "source_revision": source_revision,
        "source_tracked": bool(commit_sha),
        "branch": branch or None,
        "forge_version": __version__,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
    }


def _current_locale():
    return "C.UTF-8"


def _now_in_timezone(timezone_name):
    return datetime.now(ZoneInfo(timezone_name)).strftime("%Y-%m-%dT%H:%M:%S%z")


def _artifact_path_for_task(task):
    fixture_repo_name = Path(str(task["fixture_repo"])).name
    if fixture_repo_name not in TASK_FIXTURE_ARTIFACTS:
        raise ValueError(f"unsupported fixture repo for artifact lookup: {fixture_repo_name}")
    return TASK_FIXTURE_ARTIFACTS[fixture_repo_name]


def _workspace_relative(path, workspace_root):
    return str(Path(path).resolve().relative_to(Path(workspace_root).resolve()))


def _bugfix_command(command, workspace_root=None):
    command = str(command)
    python_exe = sys.executable.replace("\\", "/")
    if workspace_root is not None and "python verify_calculator.py" in command:
        root_path = Path(workspace_root)
        if not root_path.is_absolute():
            root_path = Path.cwd() / root_path
        verify_path = (root_path / "verify_calculator.py").as_posix()
        return command.replace("python verify_calculator.py", f'{python_exe} {verify_path}')
    if workspace_root is not None and "python -m pytest" in command:
        root = Path(workspace_root).resolve().as_posix()
        verbose = " -s" in command
        long_output = "" if not verbose else "; [print(f'LONG-VERIFICATION-LINE-{index:03d}') for index in range(180)]"
        code = (
            "import sys; "
            f"sys.path.insert(0, r'{root}'); "
            "from calculator import add, subtract; "
            "assert add(2, 3) == 5; "
            "assert subtract(5, 3) == 2"
            f"{long_output}; "
            "print('PASS calculator regression')"
        )
        return f'{python_exe} -c "{code}"'
    command = command.replace("python -m", f"{python_exe} -m")
    if workspace_root is not None:
        root = Path(workspace_root).resolve()
        test_path = (root / "test_calculator.py").as_posix()
        calc_path = (root / "calculator.py").as_posix()
        command = command.replace("test_calculator.py", "__FORGE_TEST_CALCULATOR__")
        command = command.replace("calculator.py", calc_path)
        command = command.replace("__FORGE_TEST_CALCULATOR__", test_path)
    return command


class _BugfixScriptedModelClient(ScriptedModelClient):
    def __init__(self, outputs, workspace_root=None):
        super().__init__(outputs)
        self.workspace_root = workspace_root

    def complete(self, prompt, max_new_tokens, **kwargs):
        result = super().complete(prompt, max_new_tokens, **kwargs)
        if isinstance(result, str):
            result = _bugfix_command(result, self.workspace_root)
        return result


def _scripted_outputs_for_task(task):
    outputs = SCRIPTED_MODEL_OUTPUTS.get(task["id"])
    if outputs is None:
        raise ValueError(f"no scripted model outputs for benchmark task: {task['id']}")
    return list(outputs)


def _model_client_for_task(task, workspace_root=None):
    if task["id"] in CONTEXT_PROBE_TASK_IDS:
        return _ContextProbeModelClient(task)
    if task["id"] in LONG_CHAIN_PROBE_TASK_IDS:
        return _LongChainProbeModelClient(task)
    if str(task.get("category", "")) == "bugfix-verification":
        return _BugfixScriptedModelClient(_scripted_outputs_for_task(task), workspace_root=workspace_root)
    return ScriptedModelClient(_scripted_outputs_for_task(task))


def _fixture_snapshot_id(fixture_paths):
    sha = hashlib.sha256()
    for fixture_path in sorted({Path(path).resolve() for path in fixture_paths}, key=lambda path: str(path)):
        for path in sorted((item for item in fixture_path.rglob("*") if item.is_file()), key=lambda item: str(item.relative_to(fixture_path))):
            sha.update(str(fixture_path.name).encode("utf-8"))
            sha.update(b"\0")
            sha.update(str(path.relative_to(fixture_path)).encode("utf-8"))
            sha.update(b"\0")
            sha.update(path.read_bytes())
            sha.update(b"\0")
    return "sha256:" + sha.hexdigest()


def validate_benchmark(data, repo_root=None):
    if not isinstance(data, dict):
        raise ValueError("benchmark must be a mapping")

    missing = [key for key in REQUIRED_BENCHMARK_KEYS if key not in data]
    if missing:
        raise ValueError(f"benchmark is missing required keys: {', '.join(missing)}")

    if int(data.get("schema_version", 0)) != BENCHMARK_SCHEMA_VERSION:
        raise ValueError("unsupported benchmark schema_version")

    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("benchmark tasks must be a non-empty list")

    repo_root = Path(repo_root or Path.cwd()).resolve()
    seen_ids = set()
    normalized_tasks = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"benchmark task at index {index} must be a mapping")

        missing_task_keys = [key for key in REQUIRED_TASK_KEYS if key not in task]
        if missing_task_keys:
            raise ValueError(
                f"benchmark task {task.get('id', index)!r} is missing required keys: {', '.join(missing_task_keys)}"
            )

        task_id = str(task["id"]).strip()
        if not task_id:
            raise ValueError(f"benchmark task at index {index} has an empty id")
        if task_id in seen_ids:
            raise ValueError(f"duplicate benchmark task id: {task_id}")
        seen_ids.add(task_id)

        fixture_repo = repo_root / str(task["fixture_repo"])
        if not fixture_repo.is_dir():
            raise ValueError(f"benchmark task {task_id} fixture repo does not exist: {task['fixture_repo']}")

        allowed_tools = task["allowed_tools"]
        if not isinstance(allowed_tools, list) or not allowed_tools:
            raise ValueError(f"benchmark task {task_id} allowed_tools must be a non-empty list")
        normalized_allowed_tools = []
        for tool in allowed_tools:
            tool_name = str(tool).strip()
            if not tool_name:
                raise ValueError(f"benchmark task {task_id} has an empty allowed_tools entry")
            normalized_allowed_tools.append(tool_name)

        step_budget = int(task["step_budget"])
        if step_budget < 1:
            raise ValueError(f"benchmark task {task_id} step_budget must be positive")

        normalized_task = dict(task)
        normalized_task["id"] = task_id
        normalized_task["prompt"] = str(task["prompt"]).strip()
        normalized_task["fixture_repo"] = str(task["fixture_repo"]).strip()
        normalized_task["allowed_tools"] = normalized_allowed_tools
        normalized_task["step_budget"] = step_budget
        normalized_task["expected_artifact"] = str(task["expected_artifact"]).strip()
        normalized_task["verifier"] = str(task["verifier"]).strip()
        normalized_task["category"] = str(task["category"]).strip()
        normalized_tasks.append(normalized_task)

    normalized = dict(data)
    normalized["schema_version"] = BENCHMARK_SCHEMA_VERSION
    normalized["tasks"] = normalized_tasks
    return normalized


def load_benchmark(path=DEFAULT_BENCHMARK_PATH, repo_root=None):
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if repo_root is None:
        repo_root = path.resolve().parent.parent
    return validate_benchmark(data, repo_root=repo_root)


def summarize_rows(rows):
    rows = list(rows)
    passed = sum(1 for row in rows if row.get("passed") or row.get("status") == "pass")
    failed = len(rows) - passed
    failure_category_counts = {}
    for row in rows:
        if row.get("passed") or row.get("status") == "pass":
            continue
        category = str(row.get("failure_category") or "unknown")
        failure_category_counts[category] = failure_category_counts.get(category, 0) + 1

    total_tasks = len(rows)
    within_budget = sum(1 for row in rows if row.get("within_budget"))
    verifier_passes = sum(1 for row in rows if row.get("verifier_passed"))
    return {
        "total_tasks": total_tasks,
        "passed": passed,
        "failed": failed,
        "pass_rate": (passed / total_tasks) if total_tasks else 0.0,
        "within_budget": within_budget,
        "verifier_passes": verifier_passes,
        "within_budget_rate": (within_budget / total_tasks) if total_tasks else 0.0,
        "verifier_pass_rate": (verifier_passes / total_tasks) if total_tasks else 0.0,
        "failure_category_counts": failure_category_counts,
    }


def _ratio(numerator, denominator):
    if not denominator:
        return 0.0
    return numerator / denominator


def summarize_bugfix_metrics(rows):
    rows = list(rows)
    bugfix_rows = [row for row in rows if str(row.get("category", "")) == "bugfix-verification"]
    if not bugfix_rows:
        return {}

    ledgers = [dict((row.get("report") or {}).get("bug_fix_ledger", {}) or {}) for row in bugfix_rows]
    strict_ledgers = [ledger for ledger in ledgers if ledger.get("mode") == "strict"]
    soft_ledgers = [ledger for ledger in ledgers if ledger.get("mode") == "soft"]
    inactive_ledgers = [ledger for ledger in ledgers if not ledger.get("active")]
    guard_triggered = [ledger for ledger in ledgers if ledger.get("guard_triggered")]
    false_guard_rows = [
        row for row, ledger in zip(bugfix_rows, ledgers)
        if row["id"] in {"docs_fix_does_not_require_test", "non_bug_feature_edit_not_guarded"}
        and ledger.get("guard_triggered")
    ]
    fake_verify_rows = [
        row for row, ledger in zip(bugfix_rows, ledgers)
        if row["id"] == "echo_test_does_not_satisfy_verification"
        and str(ledger.get("verification_command", "")).startswith("echo")
    ]
    evidence_ledgers = [
        ledger for ledger in ledgers
        if ledger.get("failing_evidence") or ledger.get("passing_evidence")
    ]
    changed_path_ledgers = [ledger for ledger in ledgers if ledger.get("changed_paths")]
    return {
        "task_count": len(bugfix_rows),
        "strict_count": len(strict_ledgers),
        "soft_count": len(soft_ledgers),
        "inactive_count": len(inactive_ledgers),
        "guard_trigger_rate": _ratio(len(guard_triggered), len(strict_ledgers)),
        "false_guard_rate": _ratio(len(false_guard_rows), 2),
        "false_verification_accept_rate": _ratio(len(fake_verify_rows), 1),
        "evidence_record_rate": _ratio(len(evidence_ledgers), len([ledger for ledger in ledgers if ledger.get("active")])),
        "changed_paths_record_rate": _ratio(len(changed_path_ledgers), len(bugfix_rows)),
    }

def summarize_context_metrics(rows):
    rows = list(rows)
    metrics = [row.get("context_metrics", {}) for row in rows if row.get("context_metrics")]
    if not metrics:
        return {
            "row_count": 0,
            "average_prompt_tokens": 0,
            "max_prompt_tokens": 0,
            "average_history_tokens": 0,
            "average_memory_tokens": 0,
            "average_tool_output_tokens": 0,
            "prompt_over_budget_count": 0,
            "average_total_estimated_tokens": 0,
            "max_total_estimated_tokens": 0,
            "average_prompt_chars": 0,
            "total_budget_reduction_count": 0,
            "total_history_chars_saved": 0,
            "total_compact_count": 0,
            "total_compaction_count": 0,
            "total_checkpoint_count": 0,
            "total_recovery_checkpoint_count": 0,
            "total_routine_checkpoint_count": 0,
            "total_context_reduction_checkpoint_count": 0,
            "total_repeated_tool_guard_count": 0,
            "total_repeated_tool_rejection_count": 0,
        }

    row_count = len(metrics)
    total_tokens = sum(int(item.get("prompt_tokens", item.get("total_estimated_tokens", 0))) for item in metrics)
    total_prompt_chars = sum(int(item.get("prompt_chars", 0)) for item in metrics)
    return {
        "row_count": row_count,
        "average_prompt_tokens": round(total_tokens / row_count, 2),
        "max_prompt_tokens": max(int(item.get("prompt_tokens", item.get("total_estimated_tokens", 0))) for item in metrics),
        "average_history_tokens": round(
            sum(int(item.get("history_tokens", 0)) for item in metrics) / row_count, 2
        ),
        "average_memory_tokens": round(
            sum(int(item.get("memory_tokens", 0)) for item in metrics) / row_count, 2
        ),
        "average_tool_output_tokens": round(
            sum(int(item.get("tool_output_tokens", 0)) for item in metrics) / row_count, 2
        ),
        "prompt_over_budget_count": sum(1 for item in metrics if item.get("prompt_over_budget")),
        "average_total_estimated_tokens": round(total_tokens / row_count, 2),
        "max_total_estimated_tokens": max(int(item.get("prompt_tokens", item.get("total_estimated_tokens", 0))) for item in metrics),
        "average_prompt_chars": round(total_prompt_chars / row_count, 2),
        "total_budget_reduction_count": sum(int(item.get("budget_reduction_count", 0)) for item in metrics),
        "total_history_chars_saved": sum(int(item.get("history_chars_saved", 0)) for item in metrics),
        "total_compact_count": sum(int(item.get("compact_count", item.get("compaction_count", 0))) for item in metrics),
        "total_compaction_count": sum(int(item.get("compaction_count", 0)) for item in metrics),
        "total_checkpoint_count": sum(int(item.get("checkpoint_count", 0)) for item in metrics),
        "total_recovery_checkpoint_count": sum(int(item.get("recovery_checkpoint_count", 0)) for item in metrics),
        "total_routine_checkpoint_count": sum(int(item.get("routine_checkpoint_count", 0)) for item in metrics),
        "total_context_reduction_checkpoint_count": sum(
            int(item.get("context_reduction_checkpoint_count", 0)) for item in metrics
        ),
        "total_repeated_tool_guard_count": sum(
            int(item.get("repeated_tool_guard_count", item.get("repeated_tool_rejection_count", 0))) for item in metrics
        ),
        "total_repeated_tool_rejection_count": sum(
            int(item.get("repeated_tool_rejection_count", 0)) for item in metrics
        ),
    }


def _checkpoint_payload(
    checkpoint_id,
    current_goal,
    next_step,
    runtime_identity,
    *,
    schema_version=BENCHMARK_SCHEMA_VERSION,
    current_blocker="",
    key_files=None,
    freshness=None,
    summary="",
):
    return {
        "checkpoint_id": checkpoint_id,
        "parent_checkpoint_id": "",
        "schema_version": "phase1-v1" if schema_version == BENCHMARK_SCHEMA_VERSION else str(schema_version),
        "created_at": "2026-04-15T08:00:00+00:00",
        "current_goal": current_goal,
        "completed": [],
        "excluded": [],
        "current_blocker": current_blocker,
        "next_step": next_step,
        "key_files": list(key_files or []),
        "freshness": dict(freshness or {}),
        "summary": summary or current_goal,
        "runtime_identity": dict(runtime_identity),
    }


def _apply_task_setup(agent, task, fixture_copy_root):
    setup = dict(task.get("setup", {}) or {})
    if not setup:
        return

    kind = str(setup.get("kind", "")).strip()
    if kind == "long_chain_action_failure_history":
        last_failure = str(setup.get("last_failure", "long-chain action failure expected beta-locked but saw beta"))
        changed_file = str(setup.get("changed_file", "sample.txt"))
        next_step = str(setup.get("next_step", "read sample.txt, patch beta to beta-locked, then run verification"))
        for index in range(int(setup.get("history_turns", 36))):
            agent.record(
                {
                    "role": "assistant" if index % 2 else "user",
                    "content": f"long-chain action history {index}: investigated candidate file {index % 6} " + ("A" * 120),
                    "turn_id": f"long_action_failure_{index:02d}",
                    "created_at": f"2026-04-15T06:{index:02d}:00+00:00",
                }
            )
        agent.record(
            {
                "role": "tool",
                "name": "run_shell",
                "args": {"command": "python verify_sample.py"},
                "content": f"exit_code: 1\nstderr: {last_failure}\nchanged file: {changed_file}\nnext step: {next_step}",
                "turn_id": "long_action_failure_verify",
                "created_at": "2026-04-15T06:45:00+00:00",
            }
        )
        agent.context_manager.total_budget = int(setup.get("total_budget", 5200))
        return

    if kind == "long_chain_action_stale_patch":
        for index in range(int(setup.get("history_turns", 30))):
            agent.record(
                {
                    "role": "assistant",
                    "content": f"long-chain stale patch history {index}: stale summaries must be checked before patching " + ("S" * 120),
                    "turn_id": f"long_action_stale_{index:02d}",
                    "created_at": f"2026-04-15T06:{index:02d}:00+00:00",
                }
            )
        path = str(setup.get("path", "sample.txt"))
        summary_text = str(setup.get("summary", "sample.txt stale summary says placeholder is the patch target"))
        agent.memory.set_file_summary(path, summary_text)
        agent.memory.remember_file(path)
        freshness = agent.memory.to_dict()["file_summaries"][path]["freshness"]
        agent.session["memory"] = agent.memory.to_dict()
        agent.session["checkpoints"] = {
            "current_id": "ckpt_long_action_stale",
            "items": {
                "ckpt_long_action_stale": _checkpoint_payload(
                    "ckpt_long_action_stale",
                    current_goal="Patch current file state after stale long-chain summary",
                    next_step=f"Re-read {path} and patch only current contents",
                    runtime_identity={"workspace_fingerprint": agent.workspace.fingerprint()},
                    key_files=[{"path": path, "freshness": freshness}],
                    freshness={path: freshness},
                    summary="long-chain action stale patch checkpoint",
                )
            },
        }
        agent.session_store.save(agent.session)
        (fixture_copy_root / path).write_text(str(setup.get("mutated_text", "alpha\nbeta\ngamma\nstale-target\n")), encoding="utf-8")
        return

    if kind == "long_chain_action_constraint_history":
        constraint = str(setup.get("constraint", "do not modify the opening sentence"))
        stale_plan = str(setup.get("stale_plan", "old plan incorrectly suggested replacing the opening sentence"))
        agent.record(
            {
                "role": "user",
                "content": f"Long-chain constraint: {constraint}. Ignore stale plan: {stale_plan}.",
                "turn_id": "long_action_constraint_user",
                "created_at": "2026-04-15T06:00:00+00:00",
            }
        )
        for index in range(int(setup.get("history_turns", 42))):
            agent.record(
                {
                    "role": "assistant" if index % 2 else "user",
                    "content": f"constraint action history {index}: {stale_plan}; current constraint remains {constraint} " + ("C" * 120),
                    "turn_id": f"long_action_constraint_{index:02d}",
                    "created_at": f"2026-04-15T06:{index + 1:02d}:00+00:00",
                }
            )
        agent.context_manager.total_budget = int(setup.get("total_budget", 5200))
        return

    if kind == "long_chain_action_bugfix_history":
        last_failure = str(setup.get("last_failure", "calculator add regression still failing after first attempted repair"))
        for index in range(int(setup.get("history_turns", 34))):
            agent.record(
                {
                    "role": "assistant" if index % 2 else "user",
                    "content": f"bugfix action history {index}: inspect calculator regression and preserve verification evidence " + ("B" * 120),
                    "turn_id": f"long_action_bugfix_{index:02d}",
                    "created_at": f"2026-04-15T06:{index:02d}:00+00:00",
                }
            )
        agent.record(
            {
                "role": "tool",
                "name": "run_shell",
                "args": {"command": "python verify_calculator.py"},
                "content": f"exit_code: 1\nstderr: {last_failure}",
                "turn_id": "long_action_bugfix_failed_verify",
                "created_at": "2026-04-15T06:45:00+00:00",
            }
        )
        agent.context_manager.total_budget = int(setup.get("total_budget", 5200))
        return

    if kind == "long_chain_failure_state":
        changed_file = str(setup.get("changed_file", "sample.txt"))
        last_failure = str(setup.get("last_failure", "long-chain failure sentinel expected beta-locked but saw beta"))
        next_step = str(setup.get("next_step", "next step is inspect sample.txt before final"))
        verification_constraint = str(setup.get("verification_constraint", "must run verification before final"))
        for index in range(int(setup.get("history_turns", 20))):
            agent.record(
                {
                    "role": "assistant" if index % 2 else "user",
                    "content": f"long-chain setup turn {index}: reviewed module-{index % 5}.py " + ("H" * 100),
                    "turn_id": f"long_failure_{index:02d}",
                    "created_at": f"2026-04-15T07:{index:02d}:00+00:00",
                }
            )
        agent.record(
            {
                "role": "tool",
                "name": "patch_file",
                "args": {"path": changed_file},
                "content": f"patched {changed_file}; changed file is {changed_file}",
                "turn_id": "long_failure_patch",
                "created_at": "2026-04-15T07:30:00+00:00",
            }
        )
        agent.record(
            {
                "role": "tool",
                "name": "run_shell",
                "args": {"command": "python verify_long_chain.py"},
                "content": f"exit_code: 1\nstderr: {last_failure}\n{next_step}\n{verification_constraint}",
                "turn_id": "long_failure_verify",
                "created_at": "2026-04-15T07:31:00+00:00",
            }
        )
        agent.context_manager.total_budget = int(setup.get("total_budget", 5000))
        return

    if kind == "long_chain_stale_summary":
        for index in range(int(setup.get("history_turns", 16))):
            agent.record(
                {
                    "role": "assistant",
                    "content": f"stale-summary filler turn {index}: keep checking freshness before trusting summaries " + ("S" * 100),
                    "turn_id": f"long_stale_{index:02d}",
                    "created_at": f"2026-04-15T07:{index:02d}:00+00:00",
                }
            )
        path = str(setup.get("path", "sample.txt"))
        summary_text = str(setup.get("summary", "sample.txt: stale long-chain summary"))
        agent.memory.set_file_summary(path, summary_text)
        agent.memory.remember_file(path)
        freshness = agent.memory.to_dict()["file_summaries"][path]["freshness"]
        agent.session["memory"] = agent.memory.to_dict()
        agent.session["checkpoints"] = {
            "current_id": "ckpt_long_stale",
            "items": {
                "ckpt_long_stale": _checkpoint_payload(
                    "ckpt_long_stale",
                    current_goal="Continue long-chain stale summary recovery",
                    next_step=f"Re-read {path} before trusting stale long-chain summary",
                    runtime_identity={"workspace_fingerprint": agent.workspace.fingerprint()},
                    key_files=[{"path": path, "freshness": freshness}],
                    freshness={path: freshness},
                    summary="stale long-chain summary checkpoint",
                )
            },
        }
        agent.session_store.save(agent.session)
        (fixture_copy_root / path).write_text(str(setup.get("mutated_text", "alpha\nbeta\nlong-chain-stale-updated\n")), encoding="utf-8")
        return

    if kind == "long_chain_user_constraint":
        constraint = str(setup.get("constraint", "do not modify README public API wording"))
        filler_chars = int(setup.get("filler_chars", 180))
        agent.record(
            {
                "role": "user",
                "content": f"Important long-chain user constraint: {constraint}.",
                "turn_id": "long_constraint_user",
                "created_at": "2026-04-15T07:00:00+00:00",
            }
        )
        for index in range(int(setup.get("history_turns", 24))):
            agent.record(
                {
                    "role": "assistant" if index % 2 else "user",
                    "content": f"constraint pressure turn {index}: preserve the user constraint while investigating README state " + ("C" * filler_chars),
                    "turn_id": f"long_constraint_{index:02d}",
                    "created_at": f"2026-04-15T07:{index + 1:02d}:00+00:00",
                }
            )
        agent.context_manager.total_budget = int(setup.get("total_budget", 3200))
        return

    if kind == "history_only_recall":
        agent.feature_flags["memory"] = False
        agent.feature_flags["relevant_memory"] = False
        agent.record(
            {
                "role": "assistant",
                "content": str(setup.get("fact", "history-only fact is amber")),
                "created_at": "2026-04-15T08:00:00+00:00",
            }
        )
        agent.session["memory"] = agent.memory.to_dict()
        return

    if kind == "old_history_compressed_recall":
        fact = str(setup.get("fact", "old-compressed fact is cedar"))
        agent.feature_flags["memory"] = False
        agent.feature_flags["relevant_memory"] = False
        agent.context_manager.total_budget = int(setup.get("total_budget", 2200))
        agent.context_manager.section_budgets = dict(
            setup.get(
                "section_budgets",
                {"prefix": 500, "memory": 120, "skills": 120, "relevant_memory": 120, "history": 1700},
            )
        )
        agent.record(
            {
                "role": "assistant",
                "content": fact,
                "turn_id": "old_fact_turn",
                "created_at": "2026-04-15T08:00:00+00:00",
            }
        )
        for index in range(int(setup.get("filler_turns", 7))):
            agent.record(
                {
                    "role": "assistant",
                    "content": f"old-history-filler-{index}-" + ("H" * 120),
                    "turn_id": f"old_filler_{index:02d}",
                    "created_at": f"2026-04-15T08:{index + 1:02d}:00+00:00",
                }
            )
        agent.session["memory"] = agent.memory.to_dict()
        return

    if kind == "large_tool_output_reduction":
        path = str(setup.get("path", "large.txt"))
        large_text = "large_raw_payload\n" + ("X" * int(setup.get("payload_chars", 5000)))
        agent.record(
            {
                "role": "tool",
                "name": "read_file",
                "args": {"path": path, "start": 1, "end": 500},
                "content": large_text,
                "turn_id": "large_tool_turn",
                "created_at": "2026-04-15T08:00:00+00:00",
            }
        )
        agent.memory.remember_file(path)
        agent.memory.set_file_summary(path, str(setup.get("summary", "compact-safe summary marker")))
        agent.session["memory"] = agent.memory.to_dict()
        agent.context_manager.total_budget = int(setup.get("total_budget", 2600))
        agent.context_manager.section_budgets = dict(
            setup.get(
                "section_budgets",
                {"prefix": 500, "memory": 600, "skills": 120, "relevant_memory": 120, "history": 1200},
            )
        )
        return

    if kind == "relevant_memory_selection":
        selected = str(setup.get("selected", "selected context fact is silver"))
        irrelevant = str(setup.get("irrelevant", "irrelevant context fact is orange"))
        agent.memory.append_note(
            selected,
            tags=("recall", "silver"),
            source="selected.md",
            created_at="2026-04-15T08:00:00+00:00",
        )
        for index in range(int(setup.get("irrelevant_count", 8))):
            agent.memory.append_note(
                f"{irrelevant} #{index}",
                tags=("unrelated",),
                source=f"irrelevant-{index}.md",
                created_at=f"2026-04-15T08:{index + 1:02d}:00+00:00",
            )
        agent.session["memory"] = agent.memory.to_dict()
        return

    if kind == "current_request_pressure":
        agent.context_manager.total_budget = int(setup.get("total_budget", 900))
        agent.context_manager.section_budgets = dict(
            setup.get(
                "section_budgets",
                {"prefix": 900, "memory": 900, "skills": 200, "relevant_memory": 900, "history": 1600},
            )
        )
        for index in range(int(setup.get("history_count", 10))):
            agent.record(
                {
                    "role": "assistant",
                    "content": f"pressure-history-{index}-" + ("P" * 220),
                    "turn_id": f"pressure_{index:02d}",
                    "created_at": f"2026-04-15T08:{index:02d}:00+00:00",
                }
            )
        for index in range(int(setup.get("note_count", 6))):
            agent.memory.append_note(
                f"pressure-note-{index}-" + ("N" * 180),
                tags=("pressure",),
                created_at=f"2026-04-15T09:{index:02d}:00+00:00",
            )
        agent.session["memory"] = agent.memory.to_dict()
        return

    if kind == "context_reduction":
        history_count = int(setup.get("history_count", 12))
        note_count = int(setup.get("note_count", 6))
        for index in range(history_count):
            agent.record(
                {
                    "role": "user" if index % 2 == 0 else "assistant",
                    "content": f"benchmark-history-{index}-" + ("A" * 220),
                    "created_at": f"2026-04-15T09:{index:02d}:00+00:00",
                }
            )
        for index in range(note_count):
            agent.memory.append_note(
                f"benchmark-note-{index}-" + ("B" * 180),
                tags=("recall",),
                created_at=f"2026-04-15T10:{index:02d}:00+00:00",
            )
        agent.session["memory"] = agent.memory.to_dict()
        agent.context_manager.total_budget = int(setup.get("total_budget", 900))
        agent.context_manager.section_budgets = dict(
            setup.get(
                "section_budgets",
                {"prefix": 120, "memory": 120, "relevant_memory": 120, "history": 160},
            )
        )
        return

    if kind == "freshness_mismatch":
        path = str(setup.get("path", "sample.txt"))
        summary_text = str(setup.get("summary", f"{path}: stale benchmark summary"))
        agent.memory.set_file_summary(path, summary_text)
        agent.memory.remember_file(path)
        freshness = agent.memory.to_dict()["file_summaries"][path]["freshness"]
        agent.session["memory"] = agent.memory.to_dict()
        agent.session["checkpoints"] = {
            "current_id": "ckpt_freshness",
            "items": {
                "ckpt_freshness": _checkpoint_payload(
                    "ckpt_freshness",
                    current_goal="Re-anchor stale benchmark file state",
                    next_step=f"Re-read {path}",
                    runtime_identity={"workspace_fingerprint": agent.workspace.fingerprint()},
                    key_files=[{"path": path, "freshness": freshness}],
                    freshness={path: freshness},
                    summary="stale benchmark checkpoint",
                )
            },
        }
        agent.session_store.save(agent.session)
        (fixture_copy_root / path).write_text(str(setup.get("mutated_text", "alpha\nbeta\nstale-updated\nplaceholder\n")), encoding="utf-8")
        return

    if kind == "workspace_mismatch":
        agent.session["checkpoints"] = {
            "current_id": "ckpt_workspace",
            "items": {
                "ckpt_workspace": _checkpoint_payload(
                    "ckpt_workspace",
                    current_goal="Recover after benchmark workspace drift",
                    next_step="Rebuild runtime state from a fresh checkpoint",
                    runtime_identity={"workspace_fingerprint": "outdated-benchmark-fingerprint"},
                    summary="workspace drift benchmark checkpoint",
                )
            },
        }
        agent.session_store.save(agent.session)
        return


class BenchmarkEvaluator:
    def __init__(
        self,
        benchmark_path=DEFAULT_BENCHMARK_PATH,
        artifact_path=DEFAULT_ARTIFACT_PATH,
        workspace_root=None,
        model_name=DEFAULT_MODEL_NAME,
        model_version=DEFAULT_MODEL_VERSION,
        temperature=DEFAULT_TEMPERATURE,
        top_p=DEFAULT_TOP_P,
        max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
        timezone_name=DEFAULT_TIMEZONE,
        model_client_factory=None,
    ):
        self.benchmark_path = Path(benchmark_path)
        self.artifact_path = Path(artifact_path)
        self.workspace_root = Path(workspace_root) if workspace_root is not None else Path(
            tempfile.mkdtemp(prefix="pico-benchmark-")
        )
        self.model_name = model_name
        self.model_version = model_version
        self.temperature = temperature
        self.top_p = top_p
        self.max_new_tokens = max_new_tokens
        self.timezone_name = timezone_name
        self.model_client_factory = model_client_factory
        self.repo_root = self.benchmark_path.resolve().parent.parent

    def load(self):
        return load_benchmark(self.benchmark_path, repo_root=self.repo_root)

    def run(self):
        benchmark = self.load()
        rows = [self.run_task(task) for task in benchmark["tasks"]]
        summary = summarize_rows(rows)
        artifact = {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "captured_at": _now_in_timezone(self.timezone_name),
            "runtime": _runtime_provenance(self.repo_root),
            "benchmark": {
                "source": str(self.benchmark_path.resolve().relative_to(self.repo_root)),
                "task_count": len(benchmark["tasks"]),
            },
            "reproducibility": {
                "fixture_snapshot_id": _fixture_snapshot_id(
                    self.repo_root / str(task["fixture_repo"]) for task in benchmark["tasks"]
                ),
                "model_name": self.model_name,
                "model_version": self.model_version,
                "decoding": {
                    "temperature": self.temperature,
                    "top_p": self.top_p,
                    "max_new_tokens": self.max_new_tokens,
                },
                "timezone": self.timezone_name,
                "locale": _current_locale(),
            },
            "summary": summary,
            "context_summary": summarize_context_metrics(rows),
            "bugfix_summary": summarize_bugfix_metrics(rows),
            "failure_category_counts": summary["failure_category_counts"],
            "rows": rows,
        }
        self._write_artifact(artifact)
        return artifact

    def run_task(self, task):
        task = dict(task)
        fixture_source = self.repo_root / task["fixture_repo"]
        fixture_copy_root = self.workspace_root / task["id"] / fixture_source.name
        if fixture_copy_root.exists():
            shutil.rmtree(fixture_copy_root, ignore_errors=True)
        fixture_copy_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(fixture_source, fixture_copy_root)

        workspace = WorkspaceContext.build(
            fixture_copy_root,
            repo_root_override=fixture_copy_root,
        )
        session_store = SessionStore(fixture_copy_root / WORKSPACE_STATE_DIRNAME / "sessions")
        run_store = RunStore(fixture_copy_root / WORKSPACE_STATE_DIRNAME / "runs")
        if self.model_client_factory is not None:
            model_client = self.model_client_factory(task=task, workspace=workspace)
        else:
            model_client = _model_client_for_task(task, workspace_root=fixture_copy_root)
        agent = Pico(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            run_store=run_store,
            approval_policy="auto",
            max_steps=int(task["step_budget"]),
            max_new_tokens=self.max_new_tokens,
        )
        _apply_task_setup(agent, task, fixture_copy_root)

        initial_history_empty = len(agent.session["history"]) == 0
        initial_memory_state = agent.memory.to_dict()
        initial_memory_empty = memorylib.is_effectively_empty(initial_memory_state)
        initial_task_summary_empty = not str(initial_memory_state["working"]["task_summary"]).strip()
        initial_episodic_notes_empty = not initial_memory_state["episodic_notes"]

        final_answer = agent.ask(task["prompt"])
        task_state = agent.current_task_state
        run_dir = Path(agent.current_run_dir)
        task_state_path = agent.run_store.task_state_path(task_state)
        report_path = agent.run_store.report_path(task_state)
        report = agent.run_store.load_report(task_state.run_id)

        artifact_path = _artifact_path_for_task(task)
        artifact_file = fixture_copy_root / artifact_path
        expected_artifact_exists = artifact_file.exists()
        artifact_digest = _digest_file(artifact_file) if expected_artifact_exists else ""

        verifier = _run_verifier(task["verifier"], cwd=fixture_copy_root)

        within_budget = task_state.tool_steps <= int(task["step_budget"])
        verifier_passed = verifier.returncode == 0
        non_failure_stop_reason = task_state.stop_reason == STOP_REASON_FINAL_ANSWER_RETURNED
        passed = within_budget and verifier_passed and expected_artifact_exists and non_failure_stop_reason
        failure_category = None if passed else self._failure_category(
            within_budget=within_budget,
            verifier_passed=verifier_passed,
            expected_artifact_exists=expected_artifact_exists,
            non_failure_stop_reason=non_failure_stop_reason,
        )

        return {
            "id": task["id"],
            "prompt": task["prompt"],
            "fixture_repo": task["fixture_repo"],
            "fixture_copy_relpath": _workspace_relative(fixture_copy_root, self.workspace_root),
            "run_id": task_state.run_id,
            "run_dir_relpath": _workspace_relative(run_dir, self.workspace_root),
            "task_state_relpath": _workspace_relative(task_state_path, self.workspace_root),
            "report_relpath": _workspace_relative(report_path, self.workspace_root),
            "allowed_tools": list(task["allowed_tools"]),
            "step_budget": int(task["step_budget"]),
            "expected_artifact": task["expected_artifact"],
            "artifact_path": artifact_path,
            "artifact_exists": expected_artifact_exists,
            "artifact_digest": artifact_digest,
            "verifier": task["verifier"],
            "verifier_exit_code": verifier.returncode,
            "verifier_stdout": verifier.stdout,
            "verifier_stderr": verifier.stderr,
            "category": task["category"],
            "status": "pass" if passed else "fail",
            "passed": passed,
            "failure_category": failure_category,
            "within_budget": within_budget,
            "verifier_passed": verifier_passed,
            "expected_artifact_exists": expected_artifact_exists,
            "non_failure_stop_reason": non_failure_stop_reason,
            "tool_steps": task_state.tool_steps,
            "attempts": task_state.attempts,
            "final_answer": final_answer,
            "stop_reason": task_state.stop_reason,
            "initial_history_empty": initial_history_empty,
            "initial_memory_empty": initial_memory_empty,
            "initial_task_summary_empty": initial_task_summary_empty,
            "initial_episodic_notes_empty": initial_episodic_notes_empty,
            "context_metrics": _context_metrics(report, run_dir),
            "task_state": task_state.to_dict(),
            "report": report,
        }

    def _failure_category(
        self,
        within_budget,
        verifier_passed,
        expected_artifact_exists,
        non_failure_stop_reason,
    ):
        if not expected_artifact_exists:
            return "missing_artifact"
        if not within_budget:
            return "budget_exceeded"
        if not verifier_passed:
            return "verifier_failed"
        if not non_failure_stop_reason:
            return "failure_stop_reason"
        return "unknown"

    def _write_artifact(self, artifact):
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _context_metrics(report, run_dir):
    prompt_metadata = dict((report or {}).get("prompt_metadata", {}) or {})
    context_usage = dict(prompt_metadata.get("context_usage", {}) or {})
    usage_sections = dict(context_usage.get("sections", {}) or {})
    sections = dict(prompt_metadata.get("sections", {}) or {})
    history_section = dict(sections.get("history", {}) or {})
    trace_events = _load_trace_events(Path(run_dir) / "trace.jsonl")

    history_raw_chars = int(history_section.get("raw_chars", 0) or 0)
    history_rendered_chars = int(history_section.get("rendered_chars", 0) or 0)
    tool_output_chars = sum(
        len(str(event.get("result", "")))
        for event in trace_events
        if event.get("event") == "tool_executed"
    )
    checkpoint_count = sum(1 for event in trace_events if event.get("event") == "checkpoint_created")
    recovery_checkpoint_triggers = {"context_reduction", "freshness_mismatch", "workspace_mismatch", "model_error"}
    recovery_checkpoint_count = sum(
        1
        for event in trace_events
        if event.get("event") == "checkpoint_created" and str(event.get("trigger", "")) in recovery_checkpoint_triggers
    )
    context_reduction_checkpoint_count = sum(
        1
        for event in trace_events
        if event.get("event") == "checkpoint_created" and event.get("trigger") == "context_reduction"
    )
    repeated_tool_rejection_count = sum(
        1
        for event in trace_events
        if event.get("tool_error_code") == "repeated_tool_call"
        or "repeated identical tool call" in str(event.get("result", ""))
    )
    compact_count = len((report or {}).get("compactions", []) or [])
    prompt_tokens = int(context_usage.get("total_estimated_tokens", 0) or 0)
    memory_tokens = _section_tokens(usage_sections, "memory")
    relevant_memory_tokens = _section_tokens(usage_sections, "relevant_memory")

    return {
        "prompt_chars": int(prompt_metadata.get("prompt_chars", 0) or 0),
        "prompt_tokens": prompt_tokens,
        "prompt_budget_chars": int(prompt_metadata.get("prompt_budget_chars", 0) or 0),
        "prompt_over_budget": bool(prompt_metadata.get("prompt_over_budget", False)),
        "total_estimated_tokens": int(context_usage.get("total_estimated_tokens", 0) or 0),
        "free_tokens": int(context_usage.get("free_tokens", 0) or 0),
        "context_window": int(context_usage.get("context_window", 0) or 0),
        "prefix_tokens": _section_tokens(usage_sections, "prefix"),
        "tools_tokens": _section_tokens(usage_sections, "tools"),
        "memory_tokens": memory_tokens,
        "relevant_memory_tokens": relevant_memory_tokens,
        "combined_memory_tokens": memory_tokens + relevant_memory_tokens,
        "history_tokens": _section_tokens(usage_sections, "history"),
        "current_request_tokens": _section_tokens(usage_sections, "current_request"),
        "tool_output_chars": tool_output_chars,
        "tool_output_tokens": estimate_tokens(tool_output_chars),
        "budget_reduction_count": len(prompt_metadata.get("budget_reductions", []) or []),
        "history_raw_chars": history_raw_chars,
        "history_rendered_chars": history_rendered_chars,
        "history_chars_saved": max(0, history_raw_chars - history_rendered_chars),
        "history_rendered_turns": int(prompt_metadata.get("history", {}).get("rendered_turns", 0) or 0),
        "history_summarized_tool_count": int(prompt_metadata.get("history", {}).get("summarized_tool_count", 0) or 0),
        "relevant_memory_selected_count": int(
            prompt_metadata.get("relevant_memory", {}).get("selected_count", 0) or 0
        ),
        "relevant_memory_rendered_count": int(
            prompt_metadata.get("relevant_memory", {}).get("rendered_count", 0) or 0
        ),
        "compact_count": compact_count,
        "compaction_count": compact_count,
        "checkpoint_count": checkpoint_count,
        "recovery_checkpoint_count": recovery_checkpoint_count,
        "routine_checkpoint_count": max(0, checkpoint_count - recovery_checkpoint_count),
        "context_reduction_checkpoint_count": context_reduction_checkpoint_count,
        "repeated_tool_guard_count": repeated_tool_rejection_count,
        "repeated_tool_rejection_count": repeated_tool_rejection_count,
    }


def _section_tokens(usage_sections, section):
    return int(dict(usage_sections.get(section, {}) or {}).get("tokens", 0) or 0)


def _load_trace_events(path):
    if not Path(path).exists():
        return []
    events = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _digest_file(path):
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _run_verifier(command, cwd):
    command = str(command)
    python_args = _python_c_verifier_args(command)
    if python_args:
        return subprocess.run(
            python_args,
            cwd=cwd,
            shell=False,
            capture_output=True,
            text=True,
        )
    return subprocess.run(
        _verifier_command(command),
        cwd=cwd,
        shell=True,
        capture_output=True,
        text=True,
    )


def _python_c_verifier_args(command):
    try:
        parts = shlex.split(str(command), posix=not sys.platform.startswith("win"))
    except ValueError:
        return []
    if len(parts) == 3 and parts[0] in {"python3", "python"} and parts[1] == "-c":
        return [sys.executable, "-c", _strip_wrapping_quotes(parts[2])]
    return []


def _strip_wrapping_quotes(value):
    value = str(value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _verifier_command(command):
    command = str(command)
    if sys.platform.startswith("win"):
        return command.replace("python3 -c", f"{sys.executable} -c")
    return command


def run_fixed_benchmark(
    benchmark_path=DEFAULT_BENCHMARK_PATH,
    artifact_path=DEFAULT_ARTIFACT_PATH,
    workspace_root=None,
    model_name=DEFAULT_MODEL_NAME,
    model_version=DEFAULT_MODEL_VERSION,
    temperature=DEFAULT_TEMPERATURE,
    top_p=DEFAULT_TOP_P,
    max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
    timezone_name=DEFAULT_TIMEZONE,
    model_client_factory=None,
):
    evaluator = BenchmarkEvaluator(
        benchmark_path=benchmark_path,
        artifact_path=artifact_path,
        workspace_root=workspace_root,
        model_name=model_name,
        model_version=model_version,
        temperature=temperature,
        top_p=top_p,
        max_new_tokens=max_new_tokens,
        timezone_name=timezone_name,
        model_client_factory=model_client_factory,
    )
    return evaluator.run()


def run_harness_regression_v2(
    benchmark_path=DEFAULT_BENCHMARK_PATH,
    artifact_path=DEFAULT_HARNESS_REGRESSION_V2_ARTIFACT_PATH,
    workspace_root=None,
    model_name=DEFAULT_MODEL_NAME,
    model_version=DEFAULT_MODEL_VERSION,
    temperature=DEFAULT_TEMPERATURE,
    top_p=DEFAULT_TOP_P,
    max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
    timezone_name=DEFAULT_TIMEZONE,
    model_client_factory=None,
):
    return run_fixed_benchmark(
        benchmark_path=benchmark_path,
        artifact_path=artifact_path,
        workspace_root=workspace_root,
        model_name=model_name,
        model_version=model_version,
        temperature=temperature,
        top_p=top_p,
        max_new_tokens=max_new_tokens,
        timezone_name=timezone_name,
        model_client_factory=model_client_factory,
    )
