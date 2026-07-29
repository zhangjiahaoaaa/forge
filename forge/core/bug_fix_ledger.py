"""Lightweight bug-fix state and verification guardrails."""

import re

from .workspace import clip, now

ENGLISH_STRICT_BUG_KEYWORDS = (
    "bug",
    "fail",
    "failing",
    "failure",
    "error",
    "exception",
    "regression",
    "broken",
)
ENGLISH_SOFT_FIX_KEYWORDS = (
    "fix",
)
ZH_STRICT_BUG_KEYWORDS = (
    "报错",
    "失败",
    "异常",
    "回归",
)
ZH_SOFT_FIX_KEYWORDS = (
    "修复",
)
DOC_ONLY_HINTS = (
    "typo",
    "readme",
    "docs",
    "doc",
    "documentation",
    "comment",
    "comments",
    "wording",
    "formatting",
    "文档",
    "错别字",
    "注释",
)
VERIFY_KEYWORDS = (
    "python ",
    "python3 ",
    "python -c",
    "python3 -c",
    "pytest",
    "unittest",
    "compileall",
    "py_compile",
    "ruff",
    "mypy",
    "tsc",
    "typecheck",
    "npm test",
    "npm run build",
    "npm run typecheck",
    "pnpm test",
    "pnpm build",
    "pnpm typecheck",
    "yarn test",
    "yarn build",
    "cargo test",
    "cargo check",
    "go test",
)
NON_VERIFY_PREFIXES = (
    "echo ",
    "printf ",
    "grep ",
    "cat ",
    "type ",
)


def classify_request(text):
    lowered = str(text or "").lower()
    has_doc_hint = any(keyword in lowered for keyword in DOC_ONLY_HINTS)
    has_strict = any(keyword in lowered for keyword in ZH_STRICT_BUG_KEYWORDS) or any(
        re.search(rf"\b{re.escape(keyword)}\b", lowered)
        for keyword in ENGLISH_STRICT_BUG_KEYWORDS
    )
    if has_strict:
        return "strict"
    has_soft = any(keyword in lowered for keyword in ZH_SOFT_FIX_KEYWORDS) or any(
        re.search(rf"\b{re.escape(keyword)}\b", lowered)
        for keyword in ENGLISH_SOFT_FIX_KEYWORDS
    )
    if has_soft and has_doc_hint:
        return "soft"
    if has_soft:
        return "strict"
    return "none"


def is_bug_fix_request(text):
    return classify_request(text) != "none"


def create_ledger(user_request):
    mode = classify_request(user_request)
    return {
        "active": mode != "none",
        "mode": mode,
        "symptom": clip(str(user_request or "").strip(), 240),
        "reproduction_command": "",
        "failing_evidence": "",
        "failing_artifact": "",
        "hypothesis": "",
        "changed_paths": [],
        "verification_command": "",
        "passing_evidence": "",
        "passing_artifact": "",
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
                if metadata.get("full_output_artifact"):
                    ledger["passing_artifact"] = str(metadata.get("full_output_artifact"))
                checks = ledger.setdefault("regression_checks", [])
                if command not in checks:
                    checks.append(command)
                ledger["verify_required"] = False
            else:
                ledger["reproduction_command"] = ledger.get("reproduction_command") or command
                ledger["failing_evidence"] = _shell_evidence(result)
                if metadata.get("full_output_artifact"):
                    ledger["failing_artifact"] = str(metadata.get("full_output_artifact"))
                ledger["verify_required"] = True
        elif _exit_code(result) not in {None, 0}:
            ledger["failing_evidence"] = _shell_evidence(result)
            if metadata.get("full_output_artifact"):
                ledger["failing_artifact"] = str(metadata.get("full_output_artifact"))
            ledger["verify_required"] = True

    changed_paths = [str(path) for path in metadata.get("affected_paths", []) if str(path).strip()]
    if changed_paths:
        existing = list(ledger.get("changed_paths", []))
        for path in changed_paths:
            if path not in existing:
                existing.append(path)
        ledger["changed_paths"] = existing
        if ledger.get("mode") == "strict":
            ledger["verify_required"] = True
    ledger["updated_at"] = now()
    return ledger


def final_guard_notice(ledger):
    if not ledger or not ledger.get("active"):
        return ""
    if ledger.get("mode") != "strict":
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
        f"- Mode: {ledger.get('mode') or 'none'}",
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
    lowered = " ".join(str(command or "").lower().split())
    if not lowered:
        return False
    if any(lowered.startswith(prefix) for prefix in NON_VERIFY_PREFIXES):
        return False
    if "python" in lowered:
        return True
    return any(keyword in lowered for keyword in VERIFY_KEYWORDS)


def _exit_code(result):
    match = re.search(r"exit_code:\s*(-?\d+)", str(result or ""))
    if not match:
        return None
    return int(match.group(1))


def _shell_evidence(result):
    lines = [line.strip() for line in str(result or "").splitlines() if line.strip()]
    return clip(" | ".join(lines[:8]), 360)
