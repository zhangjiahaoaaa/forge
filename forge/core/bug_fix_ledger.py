"""Lightweight bug-fix state and verification guardrails."""

import re

from .workspace import clip, now

BUG_KEYWORDS = (
    "bug",
    "fix",
    "fail",
    "failing",
    "failure",
    "error",
    "exception",
    "regression",
    "broken",
    "修复",
    "报错",
    "失败",
    "异常",
    "回归",
)
VERIFY_KEYWORDS = (
    "pytest",
    "test",
    "unittest",
    "compileall",
    "ruff",
    "mypy",
    "npm test",
    "npm run build",
    "pnpm test",
    "yarn test",
    "cargo test",
    "go test",
)


def is_bug_fix_request(text):
    lowered = str(text or "").lower()
    return any(keyword in lowered for keyword in BUG_KEYWORDS)


def create_ledger(user_request):
    return {
        "active": is_bug_fix_request(user_request),
        "symptom": clip(str(user_request or "").strip(), 240),
        "reproduction_command": "",
        "failing_evidence": "",
        "hypothesis": "",
        "changed_paths": [],
        "verification_command": "",
        "passing_evidence": "",
        "regression_checks": [],
        "verify_required": False,
        "guard_triggered": False,
        "updated_at": now(),
    }


def update_after_tool(ledger, name, args, result, metadata):
    if not ledger or not ledger.get("active"):
        return ledger
    result = str(result or "")
    metadata = metadata or {}
    if name == "run_shell":
        command = str((args or {}).get("command", "")).strip()
        if command and _looks_like_verification(command):
            ledger["verification_command"] = command
            if _exit_code(result) == 0:
                ledger["passing_evidence"] = _shell_evidence(result)
                checks = ledger.setdefault("regression_checks", [])
                if command not in checks:
                    checks.append(command)
                ledger["verify_required"] = False
            else:
                ledger["reproduction_command"] = ledger.get("reproduction_command") or command
                ledger["failing_evidence"] = _shell_evidence(result)
                ledger["verify_required"] = True
        elif _exit_code(result) not in {None, 0}:
            ledger["failing_evidence"] = _shell_evidence(result)
            ledger["verify_required"] = True

    changed_paths = [str(path) for path in metadata.get("affected_paths", []) if str(path).strip()]
    if changed_paths:
        existing = list(ledger.get("changed_paths", []))
        for path in changed_paths:
            if path not in existing:
                existing.append(path)
        ledger["changed_paths"] = existing
        ledger["verify_required"] = True
    ledger["updated_at"] = now()
    return ledger


def final_guard_notice(ledger):
    if not ledger or not ledger.get("active"):
        return ""
    if not ledger.get("changed_paths") or not ledger.get("verify_required"):
        return ""
    paths = ", ".join(ledger.get("changed_paths", []))
    suggestions = ledger.get("regression_checks", [])
    previous = f" Previous verification: {suggestions[-1]}." if suggestions else ""
    return (
        "Bug-fix verification is still required before final answer. "
        f"Changed paths: {paths}.{previous} "
        "Run the smallest relevant test or verification command, then return final."
    )


def mark_guard_triggered(ledger):
    if ledger:
        ledger["guard_triggered"] = True
        ledger["updated_at"] = now()
    return ledger


def render_prompt_section(ledger):
    if not ledger or not ledger.get("active"):
        return ""
    lines = [
        "Bug fix ledger:",
        f"- Symptom: {ledger.get('symptom') or '-'}",
        f"- Changed paths: {', '.join(ledger.get('changed_paths', [])) or '-'}",
        f"- Failing evidence: {ledger.get('failing_evidence') or '-'}",
        f"- Passing evidence: {ledger.get('passing_evidence') or '-'}",
        f"- Verify required: {bool(ledger.get('verify_required'))}",
    ]
    if ledger.get("verification_command"):
        lines.append(f"- Last verification: {ledger['verification_command']}")
    if ledger.get("regression_checks"):
        lines.append(f"- Regression checks: {', '.join(ledger['regression_checks'][-3:])}")
    return "\n".join(lines)


def _looks_like_verification(command):
    lowered = str(command or "").lower()
    return any(keyword in lowered for keyword in VERIFY_KEYWORDS)


def _exit_code(result):
    match = re.search(r"exit_code:\s*(-?\d+)", str(result or ""))
    if not match:
        return None
    return int(match.group(1))


def _shell_evidence(result):
    lines = [line.strip() for line in str(result or "").splitlines() if line.strip()]
    return clip(" | ".join(lines[:8]), 360)
