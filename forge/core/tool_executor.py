"""Tool execution guardrail used by Pico runtime."""

import re

from .protected_paths import ProtectedTaskStateModified, execute_with_task_state_guard, reject_protected_task_state_modification
from .tool_policy import ToolPolicyChecker
from .tool_repetition import repeated_tool_call_metadata
from .workspace import clip
INLINE_TOOL_OUTPUT_LIMIT = 1000
INLINE_TOOL_OUTPUT_BUDGETS = {
    "run_shell": 1000,
    "read_file": 2000,
    "search": 1500,
    "list_files": 1200,
    "_default": 800,
}
TOOL_PREVIEW_LINES = {
    "run_shell": 40,
    "read_file": 48,
    "search": 40,
    "list_files": 60,
    "_default": 30,
}
def run_tool(agent, name, args):
    tool = agent.tools.get(name)
    if tool is None:
        agent._last_tool_result_metadata = {
            "tool_status": "rejected",
            "tool_error_code": "unknown_tool",
            "security_event_type": "",
            "risk_level": "high",
            "read_only": False,
            "affected_paths": [],
            "workspace_changed": False,
            "diff_summary": [],
        }
        return f"error: unknown tool '{name}'"
    try:
        agent.validate_tool(name, args)
    except Exception as exc:
        example = agent.tool_example(name)
        message = f"error: invalid arguments for {name}: {exc}"
        if example:
            message += f"\nexample: {example}"
        security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
        agent._last_tool_result_metadata = {
            "tool_status": "rejected",
            "tool_error_code": "invalid_arguments",
            "security_event_type": security_event_type,
            "risk_level": "high" if tool.risky else "low",
            "read_only": tool.read_only,
            "affected_paths": [],
            "workspace_changed": False,
            "diff_summary": [],
        }
        return message
    if agent.repeated_tool_call(name, args):
        agent._last_tool_result_metadata = repeated_tool_call_metadata(tool)
        return f"error: repeated identical tool call for {name}; choose a different tool or return a final answer"
    decision = agent.permission_checker.check(tool, args)
    _emit_permission_decision(agent, tool, args, decision)
    if not decision.allowed:
        agent._last_tool_result_metadata = {
            "tool_status": "rejected",
            "tool_error_code": decision.reason,
            "security_event_type": decision.security_event_type,
            "risk_level": "high" if tool.risky else "low",
            "read_only": tool.read_only,
            "affected_paths": [],
            "workspace_changed": False,
            "diff_summary": [],
            "full_output_artifact": "",
        }
        return _permission_error(agent, tool, decision)
    policy = ToolPolicyChecker(agent).check(tool, args)
    _emit_tool_policy_decision(agent, tool, args, policy)
    if not policy.allowed:
        agent._last_tool_result_metadata = {
            "tool_status": "rejected",
            "tool_error_code": policy.reason,
            "security_event_type": "tool_policy",
            "risk_level": "high" if tool.risky else "low",
            "read_only": tool.read_only,
            "affected_paths": [],
            "workspace_changed": False,
            "diff_summary": [],
        }
        agent.record_process_note_for_tool(name, agent._last_tool_result_metadata)
        return policy.message
    before_snapshot = agent.capture_workspace_snapshot() if tool.risky else {}
    after_snapshot = before_snapshot
    try:
        full_result = (
            execute_with_task_state_guard(tool, args, agent.root)
            if name == "run_shell"
            else tool.execute(args).content
        )
        result, full_output_artifact = _render_tool_result(agent, name, full_result)
        after_snapshot = agent.capture_workspace_snapshot() if tool.risky else before_snapshot
        affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
        workspace_changed = bool(affected_paths)
        tool_status = "ok"
        tool_error_code = ""
        if name == "run_shell":
            match = re.search(r"exit_code:\s*(-?\d+)", result)
            exit_code = int(match.group(1)) if match else 0
            if exit_code != 0 and workspace_changed:
                tool_status = "partial_success"
                tool_error_code = "tool_partial_success"
            elif exit_code != 0:
                tool_status = "error"
                tool_error_code = "tool_failed"
        agent.update_memory_after_tool(name, args, result)
        agent._last_tool_result_metadata = {
            "tool_status": tool_status,
            "tool_error_code": tool_error_code,
            "security_event_type": "",
            "risk_level": "high" if tool.risky else "low",
            "read_only": tool.read_only,
            "affected_paths": affected_paths,
            "workspace_changed": workspace_changed,
            "workspace_fingerprint": agent.workspace.fingerprint(),
            "diff_summary": diff_summary,
            "full_output_artifact": full_output_artifact,
        }
        agent.record_process_note_for_tool(name, agent._last_tool_result_metadata)
        return result
    except ProtectedTaskStateModified as exc:
        return reject_protected_task_state_modification(agent, tool, exc.paths)
    except Exception as exc:
        after_snapshot = agent.capture_workspace_snapshot() if tool.risky else before_snapshot
        affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
        workspace_changed = bool(affected_paths)
        security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
        agent._last_tool_result_metadata = {
            "tool_status": "partial_success" if workspace_changed else "error",
            "tool_error_code": "tool_partial_success" if workspace_changed else "tool_failed",
            "security_event_type": security_event_type,
            "risk_level": "high" if tool.risky else "low",
            "read_only": tool.read_only,
            "affected_paths": affected_paths,
            "workspace_changed": workspace_changed,
            "workspace_fingerprint": agent.workspace.fingerprint(),
            "diff_summary": diff_summary,
        }
        agent.record_process_note_for_tool(name, agent._last_tool_result_metadata)
        return f"error: tool {name} failed: {exc}"


def _render_tool_result(agent, name, full_result):
    full_result = str(full_result)
    budget = int(INLINE_TOOL_OUTPUT_BUDGETS.get(name, INLINE_TOOL_OUTPUT_BUDGETS["_default"]))
    if len(full_result) <= budget:
        return full_result, ""
    if not getattr(agent, "current_task_state", None):
        return clip(full_result, budget), ""
    path = agent.run_store.write_text_artifact(agent.current_task_state, f"{name}-output", full_result)
    relative = path.resolve().relative_to(agent.root.resolve()).as_posix()
    return _summarize_tool_result(name, full_result, relative, budget), relative


def _summarize_tool_result(name, full_result, relative_artifact, budget):
    lines = str(full_result).splitlines()
    header = f"[{name} output: {len(lines)} lines, {len(full_result)} chars; full output saved: {relative_artifact}]"
    if name == "run_shell":
        header = f"full output saved: {relative_artifact}\n{header}"
    footer = f"... (truncated; read {relative_artifact} for complete output)"
    preview_budget = max(120, int(budget) - len(header) - len(footer) - 4)
    preview = _semantic_preview(name, lines, preview_budget)
    summary = "\n".join([header, preview, footer]).strip()
    return clip(summary, budget)

def _semantic_preview(name, lines, budget):
    if name == "read_file":
        return _head_tail_preview(lines, TOOL_PREVIEW_LINES["read_file"], budget)
    if name in {"search", "list_files", "run_shell"}:
        return _head_tail_preview(lines, TOOL_PREVIEW_LINES.get(name, TOOL_PREVIEW_LINES["_default"]), budget)
    return _head_tail_preview(lines, TOOL_PREVIEW_LINES["_default"], budget)


def _head_tail_preview(lines, max_lines, budget):
    lines = list(lines)
    if not lines:
        return "(empty output)"
    budget = max(40, int(budget))
    max_lines = max(2, int(max_lines))
    if len(lines) <= max_lines and len("\n".join(lines)) <= budget:
        return "\n".join(lines)

    head = []
    tail = []
    omitted = max(0, len(lines) - 2)
    marker = f"... ({omitted} lines omitted)"
    used = len(marker)
    head_index = 0
    tail_index = len(lines) - 1
    take_head = True
    while (
        head_index <= tail_index
        and len(head) + len(tail) < max_lines
    ):
        candidate = lines[head_index] if take_head else lines[tail_index]
        extra = len(candidate) + 1
        if used + extra > budget:
            if take_head:
                take_head = False
                continue
            break
        if take_head:
            head.append(candidate)
            head_index += 1
        else:
            tail.insert(0, candidate)
            tail_index -= 1
        used += extra
        take_head = not take_head

    omitted = max(0, len(lines) - len(head) - len(tail))
    marker = f"... ({omitted} lines omitted)" if omitted else ""
    parts = [*head]
    if marker:
        parts.append(marker)
    parts.extend(tail)
    return "\n".join(parts) if parts else clip("\n".join(lines), budget)


def _emit_permission_decision(agent, tool, args, decision):
    agent.session_event_bus.emit(
        "permission_decision",
        {
            "tool_name": tool.name,
            "decision": decision.decision,
            "reason": decision.reason,
            "security_event_type": decision.security_event_type,
            "tool_profile": agent.active_tool_profile.name,
            "args": args or {},
        },
    )


def _emit_tool_policy_decision(agent, tool, args, decision):
    agent.session_event_bus.emit(
        "tool_policy_decision",
        {"tool_name": tool.name, "decision": decision.decision, "reason": decision.reason, "args": args or {}},
    )


def _permission_error(agent, tool, decision):
    if decision.reason == "plan_mode_path_mismatch":
        return f"error: plan mode can only write the active plan artifact ({agent.plan_mode.plan_path})"
    if decision.reason == "plan_mode_tool_not_allowed":
        return f"error: plan mode only allows read-only tools or writing the active plan artifact ({agent.plan_mode.plan_path})"
    if decision.reason == "write_scope_mismatch":
        return f"error: worker write_scope does not allow {tool.name} on this path"
    if decision.reason == "contract_path_protected":
        return f"error: Durable Task state, Contract, and evidence are runtime-managed and cannot be modified by {tool.name}"
    if decision.reason in {"approval_denied", "tool_not_allowed"}:
        return f"error: approval denied for {tool.name}"
    return f"error: permission denied for {tool.name}: {decision.reason}"