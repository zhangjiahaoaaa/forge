"""命令行入口。

这个模块负责把“用户怎么启动 forge”翻译成 runtime 能理解的对象：
解析参数、挑模型后端、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

import argparse
import json
import os
import shutil
import sys
import textwrap
from pathlib import Path
from urllib.parse import urlparse

from .commands.slash import command_help_text, parse_subagent_args, resolve_command
from .config import (
    DEFAULT_PROVIDER,
    PROVIDER_DEFAULTS,
    default_max_tokens_for_provider,
    load_project_env,
    resolve_project_sandbox_config,
    resolve_provider_config,
)
from .features import skills as skillslib
from .features.skills_runtime import invoke_skill
from .features.loop import LoopController, TaskContextBuilder
from .features.task_record import TaskStore, TaskStatus
from .features.verification import (
    AcceptanceContract,
    ContractStore,
    ReproductionVerdict,
    TaskVerificationService,
    VerifierVerdict,
)
from .paths import migrate_workspace_layout, workspace_state_path
from .providers import AnthropicCompatibleModelClient, OpenAICompatibleModelClient
from .core.runtime import Pico, SessionStore
from .core.workspace import WorkspaceContext, middle

DEFAULT_SECRET_ENV_NAMES = (
    "FORGE_API_KEY",
    "FORGE_OPENAI_API_KEY",
    "PICO_API_KEY",
    "PICO_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "FORGE_ANTHROPIC_API_KEY",
    "PICO_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "FORGE_DEEPSEEK_API_KEY",
    "PICO_DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "FORGE_RIGHT_CODES_API_KEY",
    "PICO_RIGHT_CODES_API_KEY",
    "RIGHT_CODES_API_KEY",
    "GITHUB_PAT",
    "GH_PAT",
)

WELCOME_ART = (
    "        )  (        ",
    "       ( () )       ",
    "     ___\\__/___     ",
    "    /  FORGE  \\    ",
    "   /______/\\__\\   ",
    "      /_**_\\       ",
)
WELCOME_NAME = "forge"
WELCOME_SUBTITLE = "local coding agent"
WELCOME_STATUS = "calm shell, ready for work"
HELP_DETAILS = (
    command_help_text()
    + "\n\n"
    + textwrap.dedent(
        """\
    Skill workflows:
    /skill <name> [args] Run a user-invocable skill.
    """
    ).strip()
)


DEFAULT_OPENAI_MODEL = PROVIDER_DEFAULTS["openai"]["model"]
DEFAULT_OPENAI_BASE_URL = PROVIDER_DEFAULTS["openai"]["base_url"]
SECRET_ENV_NAMES_VAR = "FORGE_SECRET_ENV_NAMES"
LEGACY_SECRET_ENV_NAMES_VAR = "PICO_SECRET_ENV_NAMES"


def _configured_secret_names(args):
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    configured_secret_names.update(str(name).upper() for name in args.secret_env_names)
    for env_var in (SECRET_ENV_NAMES_VAR, LEGACY_SECRET_ENV_NAMES_VAR):
        extra_names = os.environ.get(env_var, "")
        if extra_names.strip():
            configured_secret_names.update(
                item.strip().upper() for item in extra_names.split(",") if item.strip()
            )
    return sorted(configured_secret_names)


def _build_model_client(args):
    config = resolve_provider_config(
        getattr(args, "provider", None),
        start=getattr(args, "cwd", "."),
        config_path=getattr(args, "config", None),
        model=getattr(args, "model", None),
        base_url=getattr(args, "base_url", None),
        api_key=getattr(args, "api_key", None),
    )
    # CLI 只负责把 provider profile 翻译成具体协议 client。
    # 例如 deepseek 是 profile，protocol=anthropic 才决定走 Messages API。
    if config.protocol == "openai":
        return OpenAICompatibleModelClient(
            model=config.model,
            base_url=config.base_url,
            api_key=config.api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", 300),
        )
    if config.protocol == "anthropic":
        return AnthropicCompatibleModelClient(
            model=config.model,
            base_url=config.base_url,
            api_key=config.api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", 300),
        )

    raise ValueError(f"unknown provider protocol: {config.protocol}")


def build_welcome(agent, model, host):
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width

    def row(text):
        body = middle(text, width - 4)
        return f"| {body.ljust(width - 4)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(text):
        body = middle(text, inner)
        return f"| {body.center(inner)} |"

    def cell(label, value, size):
        body = middle(f"{label:<9} {value}", size)
        return body.ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    line = divider("=")
    rows = [center(text) for text in WELCOME_ART]
    rows.extend(
        [
            center(WELCOME_NAME),
            center(WELCOME_SUBTITLE),
            center(WELCOME_STATUS),
            divider("-"),
            row(""),
            row("WORKSPACE  " + middle(agent.workspace.cwd, inner - 11)),
            pair("MODEL", model, "BRANCH", agent.workspace.branch),
            pair("APPROVAL", agent.approval_policy, "SESSION", agent.session["id"]),
            row(""),
        ]
    )
    return "\n".join([line, *rows, line])


def build_agent(args):
    """根据 CLI 参数装配出一个可运行的 Forge 实例。

    为什么存在：
    命令行参数只是字符串和开关，runtime 需要的是已经装配好的对象图：
    model client、workspace snapshot、session store、secret 配置等。
    这个函数负责把“启动参数”翻译成“agent 运行现场”。

    输入 / 输出：
    - 输入：`argparse` 解析后的 `args`
    - 输出：一个新的 `Pico`，或一个从旧 session 恢复出来的 `Pico`

    在 agent 链路里的位置：
    它是整个程序启动链路里最靠近 runtime 的装配点。`main()` 先调它，
    得到 agent 后，后面无论是 one-shot 还是 REPL 模式，都会落到 `ask()`。
    """
    # 这里是 CLI 到 runtime 的装配点：
    # 先采集工作区快照，再整理 secret 名单、模型后端和 session。
    workspace = WorkspaceContext.build(args.cwd)
    migrate_workspace_layout(workspace.repo_root)
    store = SessionStore(workspace_state_path(workspace.repo_root, "sessions"))
    provider_config = resolve_provider_config(
        getattr(args, "provider", None),
        start=getattr(args, "cwd", "."),
        config_path=getattr(args, "config", None),
        model=getattr(args, "model", None),
        base_url=getattr(args, "base_url", None),
        api_key=getattr(args, "api_key", None),
    )
    model = _build_model_client(args)

    def model_client_factory():
        return _build_model_client(args)

    if args.max_new_tokens is None:
        args.max_new_tokens = default_max_tokens_for_provider(provider_config.name)

    sandbox_config = resolve_project_sandbox_config(
        start=workspace.repo_root,
        config_path=getattr(args, "config", None),
        mode=getattr(args, "sandbox", None),
        backend=getattr(args, "sandbox_backend", None),
    )
    load_project_env(workspace.repo_root, override=False)
    configured_secret_names = _configured_secret_names(args)
    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest()
    memory_dir = getattr(args, "memory_dir", None)
    auto_dream = not getattr(args, "no_auto_dream", False)
    dream_interval = getattr(args, "dream_interval", 24.0)
    dream_min_sessions = getattr(args, "dream_min_sessions", 5)
    ask_user_callback = None if getattr(args, "prompt", None) else _cli_ask_user
    if session_id:
        return Pico.from_session(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            secret_env_names=configured_secret_names,
            memory_dir=memory_dir,
            auto_dream=auto_dream,
            dream_interval_hours=dream_interval,
            dream_min_sessions=dream_min_sessions,
            model_client_factory=model_client_factory,
            sandbox_config=sandbox_config,
            ask_user_callback=ask_user_callback,
        )
    return Pico(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=configured_secret_names,
        memory_dir=memory_dir,
        auto_dream=auto_dream,
        dream_interval_hours=dream_interval,
        dream_min_sessions=dream_min_sessions,
        model_client_factory=model_client_factory,
        sandbox_config=sandbox_config,
        ask_user_callback=ask_user_callback,
    )


def build_arg_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Minimal coding agent for provider profiles backed by OpenAI-compatible or Anthropic-compatible APIs.",
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument(
        "--config", default=None, help="Path to a Forge TOML config file."
    )
    parser.add_argument(
        "--provider",
        default=None,
        help=f"Provider profile to use. Defaults to config provider or {DEFAULT_PROVIDER}.",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key override for the selected provider profile.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name override for the selected provider profile.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="API base URL override for the selected provider profile.",
    )
    parser.add_argument(
        "--openai-timeout",
        type=int,
        default=300,
        help="Provider request timeout in seconds.",
    )
    parser.add_argument(
        "--resume", default=None, help="Session id to resume or 'latest'."
    )
    parser.add_argument(
        "--memory-dir",
        default=None,
        help="Memory directory. Defaults to .forge/memory in the workspace.",
    )
    parser.add_argument(
        "--no-auto-dream",
        action="store_true",
        help="Disable automatic memory consolidation.",
    )
    parser.add_argument(
        "--dream-interval",
        type=float,
        default=24.0,
        help="Hours between automatic dream runs.",
    )
    parser.add_argument(
        "--dream-min-sessions",
        type=int,
        default=5,
        help="Minimum new sessions before automatic dream runs.",
    )
    parser.add_argument(
        "--approval",
        choices=("ask", "auto", "never"),
        default="ask",
        help="Approval policy for risky tools.",
    )
    parser.add_argument(
        "--sandbox",
        choices=("off", "best_effort", "required"),
        default=None,
        help="Sandbox mode for run_shell.",
    )
    parser.add_argument(
        "--sandbox-backend",
        choices=("auto", "bubblewrap", "none"),
        default=None,
        help="Sandbox backend for run_shell.",
    )
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to treat as secrets for trace/report redaction.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=50,
        help="Maximum tool/model iterations per request.",
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=None,
        help="Maximum model output tokens per step. Defaults to a provider-aware value (anthropic 32000, openai/deepseek 8192).",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.2,
        help="Sampling temperature sent to the provider.",
    )
    parser.add_argument(
        "--tui", action="store_true", help="Start the Textual terminal UI."
    )
    parser.add_argument(
        "--repl",
        action="store_true",
        help="Use the plain line-oriented REPL instead of the TUI.",
    )

    # ``main()`` 会在 argparse 之前分派 ``forge task ...``，让普通 one-shot
    # prompt（例如 ``forge inspect tests``）继续可以自由包含多个位置参数。
    # 因此此处不能添加 argparse subparser，否则第一个 prompt 之后的词会被
    # 错误解释为子命令并破坏现有 CLI API。
    return parser


def handle_repl_command(agent, user_input):
    raw_command = ""
    command_args = ""
    command_name = ""
    if str(user_input).startswith("/"):
        raw_command, _, command_args = str(user_input)[1:].partition(" ")
        resolved = resolve_command(raw_command)
        command_name = resolved.name if resolved else raw_command.strip().lower()
        command_args = command_args.strip()

    if user_input in {"/exit", "/quit"}:
        return True, True, ""
    if user_input == "/help":
        return True, False, HELP_DETAILS
    if user_input == "/memory":
        return True, False, agent.memory_command_text()
    if user_input == "/working-memory":
        return True, False, agent.memory_text()
    if user_input.startswith("/remember"):
        _, _, note = user_input.partition(" ")
        if not note.strip():
            return True, False, "Usage: /remember <text>"
        agent.remember_durable_note(note)
        return True, False, "Saved to daily log."
    if user_input == "/dream":
        return True, False, agent.run_dream()
    if user_input == "/skills":
        return True, False, skillslib.render_skills_list(agent.skills)
    if user_input == "/plan" or user_input.startswith("/plan "):
        _, _, raw_topic = user_input.partition(" ")
        topic = raw_topic.strip()
        if not topic:
            return True, False, _format_mode_status(agent)
        path = None
        if " " in topic:
            topic, _, path = topic.partition(" ")
            path = path.strip() or None
        try:
            plan_path = agent.enter_plan_mode(topic, path=path)
        except ValueError as exc:
            return True, False, f"error: {exc}"
        return True, False, f"mode: plan\nplan path: {plan_path}"
    if user_input == "/plan-exit":
        agent.exit_plan_mode()
        return True, False, "mode: default"
    if user_input == "/mode":
        return True, False, _format_mode_status(agent)
    if user_input == "/session":
        return True, False, _format_session_status(agent)
    if command_name == "agents":
        return True, False, _format_subagent_status(agent)
    if command_name == "subagent":
        payload, error = parse_subagent_args(command_args)
        if error:
            return True, False, error
        return True, False, agent.run_tool("agent", payload)
    if user_input == "/context":
        return (
            True,
            False,
            json.dumps(
                agent.prompt_metadata("", "")["context_usage"], indent=2, sort_keys=True
            ),
        )
    if user_input == "/usage":
        return True, False, _format_usage(agent)
    if user_input == "/model" or user_input.startswith("/model "):
        _, _, model = user_input.partition(" ")
        model = model.strip()
        if not model:
            return True, False, _format_model(agent)
        setattr(agent.model_client, "model", model)
        agent.session_event_bus.emit("model_changed", {"model": model})
        agent.refresh_prefix(force=True)
        return True, False, f"model: {model}"
    if user_input == "/history":
        return True, False, _format_history(agent)
    if user_input.startswith("/resume "):
        _, _, target = user_input.partition(" ")
        session_id = _resolve_session_id(agent, target.strip())
        if not session_id:
            return True, False, "error: session not found"
        agent.resume_session(session_id)
        return True, False, f"resumed session {session_id}"
    if user_input == "/clear":
        session_id = agent.clear_session()
        return True, False, f"new session {session_id}"
    if user_input == "/compact":
        return (
            True,
            False,
            json.dumps(
                agent.compact_history(trigger="manual"), indent=2, sort_keys=True
            ),
        )
    if user_input == "/reset":
        agent.reset()
        return True, False, "session reset"
    command, arguments = skillslib.parse_slash_command(user_input)
    if command and command in agent.skills:
        return True, False, invoke_skill(agent, command, arguments)
    return False, False, ""


def _format_mode_status(agent):
    lines = [f"runtime mode: {agent.runtime_mode}"]
    plan_path = getattr(agent.plan_mode, "plan_path", "")
    if plan_path:
        lines.append(f"plan path: {plan_path}")
    return "\n".join(lines)


def _format_session_status(agent):
    task_state = getattr(agent, "current_task_state", None)
    run_id = getattr(task_state, "run_id", "") or ""
    run_dir = str(agent.run_store.run_dir(run_id)) if run_id else "-"
    workers = agent.worker_manager.to_dict()
    items = workers.get("items", [])
    worker_summary = "none"
    if items:
        counts = {}
        for item in items:
            status = str(item.get("status", "unknown") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        worker_summary = ", ".join(
            f"{status}={count}" for status, count in sorted(counts.items())
        )
    return "\n".join(
        [
            f"session id: {agent.session.get('id', '')}",
            f"session path: {agent.session_path}",
            f"events path: {agent.session_event_bus.path}",
            f"runtime mode: {agent.runtime_mode}",
            f"plan path: {getattr(agent.plan_mode, 'plan_path', '') or '-'}",
            f"last run id: {run_id or '-'}",
            f"last run dir: {run_dir}",
            f"resume status: {agent.resume_state.get('status', '-')}",
            f"worker summary: {worker_summary}",
        ]
    )


def _format_subagent_status(agent):
    return "\n".join(
        [
            "subagent tools: agent(description, prompt, subagent_type='Explore|worker', write_scope=[]), send_message(to, message), task_stop(task_id)",
            f"worker summary: {_worker_summary(agent)}",
        ]
    )


def _worker_summary(agent):
    items = agent.worker_manager.to_dict().get("items", [])
    if not items:
        return "none"
    return ", ".join(f"{item.get('id')}:{item.get('status')}" for item in items)


def _format_usage(agent):
    metadata = dict(getattr(agent, "last_completion_metadata", {}) or {})
    context_usage = dict(
        (getattr(agent, "last_prompt_metadata", {}) or {}).get("context_usage", {})
        or {}
    )
    base_url = str(getattr(agent.model_client, "base_url", "") or "")
    host = urlparse(base_url).netloc or "-"
    lines = [
        f"provider profile: {getattr(agent.model_client, 'provider', '-') or '-'}",
        f"provider protocol: {getattr(agent.model_client, 'protocol', '-') or '-'}",
        f"model: {getattr(agent.model_client, 'model', '-') or '-'}",
        f"base url host: {host}",
        f"prompt cache supported: {bool(getattr(agent.model_client, 'supports_prompt_cache', False))}",
        f"last input tokens: {metadata.get('input_tokens', 'unavailable')}",
        f"last output tokens: {metadata.get('output_tokens', 'unavailable')}",
        f"last cached tokens: {metadata.get('cached_tokens', 'unavailable')}",
        f"last provider attempts: {metadata.get('provider_attempts', 'unavailable')}",
        f"last provider retry count: {metadata.get('provider_retry_count', 'unavailable')}",
        f"last provider error: {metadata.get('provider_error', 'unavailable')}",
        f"context usage: {context_usage.get('total_estimated_tokens', '-')}/{context_usage.get('context_window', '-')}",
    ]
    return "\n".join(lines)


def _format_model(agent):
    return f"model: {getattr(agent.model_client, 'model', '-') or '-'}"


def _format_history(agent):
    rows = agent.session_store.list_sessions()
    if not rows:
        return "(no sessions)"
    lines = []
    for row in rows:
        lines.append(
            f"{row['index']}. {row['id']} mode={row['runtime_mode']} turns={row['history_count']} "
            f"updated={row['updated_at']} {row['last_final_answer']}"
        )
    return "\n".join(lines)


def _resolve_session_id(agent, target):
    if target == "latest":
        return agent.session_store.latest()
    rows = agent.session_store.list_sessions()
    if target.isdigit():
        index = int(target)
        for row in rows:
            if row["index"] == index:
                return row["id"]
    for row in rows:
        if row["id"] == target:
            return row["id"]
    return ""


def _cli_ask_user(question, choices):
    if choices:
        print(question)
        for index, choice in enumerate(choices, start=1):
            print(f"{index}. {choice}")
        answer = input("> ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        return answer
    return input(question + " ").strip()


def _task_store_for_workspace(cwd: Path) -> TaskStore:
    """返回 .forge 下的 TaskStore，并迁移早期 CLI 错放的任务目录。"""
    from .paths import WORKSPACE_STATE_DIRNAME

    workspace = Path(cwd).resolve()
    forge_dir = workspace / WORKSPACE_STATE_DIRNAME
    tasks_dir = forge_dir / "tasks"
    legacy_dir = workspace / "tasks"
    if legacy_dir.is_dir():
        legacy_tasks = [
            entry for entry in legacy_dir.iterdir()
            if entry.is_dir() and (entry / "task.json").is_file()
        ]
        if legacy_tasks:
            tasks_dir.mkdir(parents=True, exist_ok=True)
            collisions = [entry.name for entry in legacy_tasks if (tasks_dir / entry.name).exists()]
            if collisions:
                raise RuntimeError(
                    "cannot migrate legacy task directories with conflicting ids: "
                    + ", ".join(sorted(collisions))
                )
            for entry in legacy_tasks:
                shutil.move(str(entry), str(tasks_dir / entry.name))
    forge_dir.mkdir(parents=True, exist_ok=True)
    return TaskStore(forge_dir)


def _build_controller():
    """从当前工作区构建一个 LoopController。"""
    cwd = Path.cwd().resolve()
    from .paths import WORKSPACE_STATE_DIRNAME
    forge_dir = cwd / WORKSPACE_STATE_DIRNAME
    forge_dir.mkdir(parents=True, exist_ok=True)
    tasks_root = forge_dir / "tasks"
    store = TaskStore(forge_dir)
    from .features.verification import ContractStore, TaskVerificationService
    cs = ContractStore(tasks_root)
    vs = TaskVerificationService(store, cs, cwd, tasks_root)

    def _build_agent():
        """Build a Forge agent from CLI defaults (no provider override needed)."""
        parser = build_arg_parser()
        args = parser.parse_args([])
        return build_agent(args)

    def _build_resume_agent(session_id):
        """按 session_id 恢复同一会话的 Forge agent（会话历史 = 恢复现场）。"""
        parser = build_arg_parser()
        args = parser.parse_args([])
        args.resume = session_id
        return build_agent(args)

    return LoopController(
        task_store=store,
        contract_store=cs,
        verification_service=vs,
        context_builder=TaskContextBuilder(),
        build_agent_fn=_build_agent,
        build_resume_agent_fn=_build_resume_agent,
    )


def _main_loop(loop_argv):
    """Dispatch for 'forge loop <subcommand> [args...]'."""
    if not loop_argv:
        print("usage: forge loop advance|run|recover <task_id> [--force]", file=sys.stderr)
        return 1

    cmd = loop_argv[0].lower()
    args = loop_argv[1:]
    if not args:
        print("error: task_id is required", file=sys.stderr)
        return 1
    task_id = args[0]

    controller = _build_controller()

    if cmd == "advance":
        result = controller.advance(task_id)
        task = result.get("task")
        print(f"action:   {result.get('action', '?')}")
        print(f"reason:   {result.get('reason', '?')}")
        if task:
            print(f"status:   {getattr(task, 'status', '?')}")
            print(f"phase:    {getattr(task, 'phase', '?')}")
            print(f"cycle:    {getattr(task, 'cycle', 0)}")
        return 0

    if cmd == "recover":
        # 进程崩溃后遗留 running 任务：检测 active run 并尝试 resume 同一 Run。
        # --force：确认原进程已死后跳过 owner PID 存活检查。
        force = "--force" in args[1:]
        result = controller.recover(task_id, force=force)
        task = result.get("task")
        print(f"action:   {result.get('action', '?')}")
        print(f"reason:   {result.get('reason', '?')}")
        if task:
            print(f"status:   {getattr(task, 'status', '?')}")
            print(f"phase:    {getattr(task, 'phase', '?')}")
            print(f"cycle:    {getattr(task, 'cycle', 0)}")
        return 0

    if cmd == "run":
        prompt = " ".join(args[1:]).strip() if len(args) > 1 else None
        result = controller.advance_full(task_id, run_prompt=prompt)
        task = result.get("task")
        print(f"action:   {result.get('action', '?')}")
        print(f"reason:   {result.get('reason', '?')}")
        if task:
            print(f"status:   {getattr(task, 'status', '?')}")
            print(f"phase:    {getattr(task, 'phase', '?')}")
            print(f"cycle:    {getattr(task, 'cycle', 0)}")
        return 0 if result.get("action") == "completed" else 1

    print(f"error: unknown loop command: {cmd}", file=sys.stderr)
    return 1


def _main_task(task_argv):
    """Dispatch for 'forge task <subcommand> [args...]'."""
    if not task_argv:
        print("usage: forge task create|run|show|list|close ...", file=sys.stderr)
        return 1

    cmd = task_argv[0].lower()
    task_args = task_argv[1:]

    # Resolve task store root from CWD
    cwd = Path.cwd().resolve()
    store = _task_store_for_workspace(cwd)

    if cmd == "create":
        goal = " ".join(task_args).strip()
        if not goal:
            print("error: task goal is required", file=sys.stderr)
            return 1
        task = store.create_task(goal)
        print(f"created task: {task.task_id}")
        return 0

    if cmd == "list":
        tasks = store.list_tasks()
        if not tasks:
            print("no tasks found")
            return 0
        print(f"{'task_id':30s} {'status':15s} {'goal'}")
        print("-" * 80)
        for t in tasks:
            print(f"{t.task_id:30s} {t.status.value:15s} {t.goal[:60]}")
        return 0

    if cmd == "show":
        if not task_args:
            print("error: task_id is required", file=sys.stderr)
            return 1
        task_id = task_args[0]
        try:
            task = store.load_task(task_id)
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"task_id:        {task.task_id}")
        print(f"goal:           {task.goal}")
        print(f"status:         {task.status.value}")
        print(f"revision:       {task.revision}")
        print(f"created:        {task.created_at}")
        print(f"updated:        {task.updated_at}")
        print(f"current_run:    {task.current_run_id or '-'}")
        print(f"total_runs:     {len(task.run_ids)}")
        if task.last_run_summary:
            print(f"last_summary:   {task.last_run_summary}")
        attempts = store.list_attempts(task_id)
        if attempts:
            print("\nattempts:")
            for att in attempts:
                fin = att.finished_at or "(running)"
                print(f"  {att.sequence}. {att.run_id}  {att.outcome:12s}  started={att.started_at[:19]}  finished={fin[:19]}")
        return 0

    if cmd == "close":
        if not task_args:
            print("error: task_id is required", file=sys.stderr)
            return 1
        task_id = task_args[0]
        # Parse optional --status and --reason from remaining args
        status = TaskStatus.COMPLETED
        reason = ""
        i = 1
        while i < len(task_args):
            if task_args[i] == "--status" and i + 1 < len(task_args):
                i += 1
                status = TaskStatus(task_args[i])
            elif task_args[i] == "--reason" and i + 1 < len(task_args):
                i += 1
                reason = task_args[i]
            i += 1
        try:
            task = store.close_task(task_id, status, reason)
        except (KeyError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"closed {task.task_id} as {task.status.value}")
        return 0

    if cmd == "run":
        if not task_args:
            print("error: task_id is required", file=sys.stderr)
            return 1
        task_id = task_args[0]
        prompt = " ".join(task_args[1:]).strip()
        try:
            task = store.load_task(task_id)
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED):
            print(f"error: cannot run task in {task.status.value} state", file=sys.stderr)
            return 1
        if not task.baseline or task.baseline.get("reproduction_verdict") != ReproductionVerdict.REPRODUCED.value:
            print("error: run 'forge task reproduce <task_id>' and obtain reproduced first", file=sys.stderr)
            return 1
        if not task.contract_hash:
            print("error: task has no frozen Contract; initialize it before running", file=sys.stderr)
            return 1

        service = _build_verification_service()
        if not service.contract_is_intact(task_id, task.contract_hash):
            print("error: Contract integrity check failed; task is blocked", file=sys.stderr)
            _block_task(store, task, "contract_integrity_failed")
            return 1

        # Build agent from current environment
        parser = build_arg_parser()
        agent_args = parser.parse_args([])
        try:
            agent = build_agent(agent_args)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

        if not prompt:
            prompt = task.goal
        print(f"running task {task_id} ...")

        # Engine 创建 Run 身份后、开始写 artifact 或请求模型前立即持久化。
        trusted_baseline = dict(task.baseline or {})
        trusted_contract_hash = task.contract_hash
        trusted_goal = task.goal
        owner_token_holder: dict[str, str] = {"token": ""}

        def _on_run_started(run_id, legacy_task_id):
            store.record_run_started(
                task_id, run_id, legacy_engine_task_id=legacy_task_id
            )
            # 读取 fencing token：收尾写入必须持有（record_run_finished 校验）
            try:
                attempt = next(
                    (a for a in store.list_attempts(task_id) if a.run_id == run_id),
                    None,
                )
                if attempt is not None:
                    owner_token_holder["token"] = str(
                        getattr(attempt, "owner_token", "") or ""
                    )
            except Exception:
                pass

        result = agent.ask_run(prompt, on_run_started=_on_run_started)

        artifact_refs = []
        ts = getattr(agent, "current_task_state", None)
        if ts:
            run_dir = agent.run_store.run_dir(ts)
            for p in [run_dir / "report.json", run_dir / "trace.jsonl"]:
                if p.exists():
                    artifact_refs.append(str(p))
        store.record_run_finished(
            task_id,
            result.run_id,
            outcome=result.outcome,
            summary=(result.final_answer or "")[:200],
            artifact_refs=artifact_refs,
            owner_token=owner_token_holder["token"],
        )

        task = store.load_task(task_id)
        if (
            task.goal != trusted_goal
            or task.contract_hash != trusted_contract_hash
            or task.baseline != trusted_baseline
        ):
            _block_task(store, task, "durable_task_state_modified_during_run")
            print("error: Durable Task state changed during Agent Run; task is blocked", file=sys.stderr)
            return 1

        verdict, info = service.run_verification(
            task_id,
            run_id=result.run_id,
            expected_contract_hash=trusted_contract_hash,
            baseline_files=trusted_baseline.get("workspace_files"),
        )
        task = store.load_task(task_id)
        _record_verification_result(store, task, verdict, info, completion_reason="verifier_pass")

        print()
        print(result.final_answer)
        print(f"verify: {verdict.value}")
        for ref in info.evidence_refs:
            print(f"  evidence: {ref}")
        return 0

    # -- PR 2: reproduce / verify --

    if cmd == "reproduce":
        if not task_args:
            print("error: task_id is required", file=sys.stderr)
            return 1
        task_id = task_args[0]
        service = _build_verification_service()
        try:
            task = store.load_task(task_id)
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        if not task.contract_hash:
            print("error: task has no frozen Contract; run 'forge contract init <task_id>' first", file=sys.stderr)
            return 1
        repro_verdict, info = service.establish_baseline(
            task_id,
            expected_contract_hash=task.contract_hash,
        )
        task.baseline = {
            "commit": info.commit,
            "workspace_fingerprint": info.workspace_fingerprint,
            "reproduction_verdict": info.reproduction_verdict,
            "failure_fingerprint": info.failure_fingerprint,
            "evidence_ref": info.evidence_ref,
            "workspace_files": dict(info.workspace_files),
        }
        store.save_task(task)

        print(f"reproduce: {repro_verdict.value}")
        if info.evidence_ref:
            print(f"evidence:  {info.evidence_ref}")

        if repro_verdict == ReproductionVerdict.REPRODUCED:
            return 0
        if repro_verdict == ReproductionVerdict.NOT_REPRODUCED:
            verdict, verify_info = service.run_verification(
                task_id,
                run_id="baseline",
                expected_contract_hash=task.contract_hash,
                baseline_files=(task.baseline or {}).get("workspace_files"),
            )
            task = store.load_task(task_id)
            if verdict == VerifierVerdict.PASS:
                _record_verification_result(
                    store, task, verdict, verify_info, completion_reason="already_resolved"
                )
            else:
                _record_verification_result(
                    store, task, verdict, verify_info, completion_reason="baseline_mismatch"
                )
            print(f"verify: {verdict.value}")
            return 0

        task = store.load_task(task_id)
        _block_task(store, task, f"baseline_{repro_verdict.value}")
        return 0

    if cmd == "verify":
        if not task_args:
            print("error: task_id is required", file=sys.stderr)
            return 1
        task_id = task_args[0]
        run_id = task_args[1] if len(task_args) > 1 else ""
        service = _build_verification_service()
        try:
            task = store.load_task(task_id)
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED):
            print(f"error: cannot verify task in {task.status.value} state", file=sys.stderr)
            return 1
        if not task.baseline or task.baseline.get("reproduction_verdict") != ReproductionVerdict.REPRODUCED.value:
            print("error: reproduce must be stably reproduced before verification", file=sys.stderr)
            return 1
        if not task.contract_hash:
            print("error: task has no frozen Contract", file=sys.stderr)
            return 1
        verdict, info = service.run_verification(
            task_id,
            run_id=run_id,
            expected_contract_hash=task.contract_hash,
            baseline_files=(task.baseline or {}).get("workspace_files"),
        )
        _record_verification_result(store, task, verdict, info, completion_reason="verifier_pass")
        print(f"verify: {verdict.value}")
        if info.evidence_refs:
            for r in info.evidence_refs:
                print(f"  evidence: {r}")
        return 0

    print(f"error: unknown task command: {cmd}", file=sys.stderr)
    return 1


def _record_verification_result(store, task, verdict, info, completion_reason):
    """持久化外部验收结果；只有 PASS 可自动完成 Task。

    非 PASS verdict 显式将状态复位为 IN_PROGRESS（除非原本就是终端状态），
    防止 Agent 在 Run 期间篡改 task.json 的 status 字段后验收失败仍遗留 COMPLETED。
    """
    task.last_verdict = {
        "verdict": info.verdict or verdict.value,
        "checked_at": info.checked_at,
        "evidence_refs": list(info.evidence_refs),
    }
    if verdict == VerifierVerdict.PASS:
        task.status = TaskStatus.COMPLETED
        task.phase = ""
        task.completion_reason = completion_reason
    elif completion_reason == "baseline_mismatch":
        task.status = TaskStatus.BLOCKED
        task.phase = ""
        task.completion_reason = completion_reason
    elif verdict in (VerifierVerdict.BLOCKED, VerifierVerdict.INFRA_ERROR):
        task.status = TaskStatus.BLOCKED
        task.phase = ""
        task.completion_reason = info.reason or verdict.value
    else:
        # FAIL / FORBIDDEN → 显式复位为 IN_PROGRESS，归还重试可能性
        if task.status == TaskStatus.COMPLETED:
            task.status = TaskStatus.IN_PROGRESS
        task.completion_reason = f"verify_{verdict.value}" if hasattr(verdict, "value") else str(verdict)
    store.save_task(task)


def _block_task(store, task, reason):
    """记录无法继续自动验收的确定性阻塞原因。"""
    task.status = TaskStatus.BLOCKED
    task.completion_reason = reason
    store.save_task(task)


def _build_verification_service() -> TaskVerificationService:
    """Build a TaskVerificationService from the current workspace."""
    cwd = Path.cwd().resolve()
    store = _task_store_for_workspace(cwd)
    forge_dir = store.root
    contract_store = ContractStore(forge_dir / "tasks")
    return TaskVerificationService(
        task_store=store,
        contract_store=contract_store,
        workspace_root=cwd,
        tasks_root=forge_dir / "tasks",
    )


def _main_contract(contract_argv):
    """Dispatch for 'forge contract <subcommand> [args...]'."""
    if not contract_argv:
        print("usage: forge contract init <task_id> --from <contract.json> | validate <task_id>", file=sys.stderr)
        return 1

    cmd = contract_argv[0].lower()
    args = contract_argv[1:]

    if cmd == "init":
        if not args:
            print("error: task_id is required", file=sys.stderr)
            return 1
        task_id = args[0]
        source_path = ""
        index = 1
        while index < len(args):
            if args[index] == "--from" and index + 1 < len(args):
                index += 1
                source_path = args[index]
            index += 1
        if not source_path:
            print("error: --from <contract.json> is required", file=sys.stderr)
            return 1

        store_path = _resolve_tasks_root()
        store = _task_store_for_workspace(Path.cwd())
        try:
            task = store.load_task(task_id)
            source = Path(source_path).resolve()
            raw_text = source.read_text(encoding="utf-8")
            if source.suffix.lower() in (".yaml", ".yml"):
                try:
                    import yaml
                    raw_contract = yaml.safe_load(raw_text)
                except ImportError:
                    print("error: PyYAML is required for .yaml/.yml files; install with 'pip install pyyaml' or use JSON format", file=sys.stderr)
                    return 1
                except yaml.YAMLError as exc:
                    print(f"error: invalid YAML in {source_path}: {exc}", file=sys.stderr)
                    return 1
            else:
                raw_contract = json.loads(raw_text)
            contract = AcceptanceContract.from_dict(raw_contract)
        except (KeyError, OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if not contract.goal:
            contract.goal = task.goal
        if not contract.reproduce_command or not contract.verify_commands:
            print("error: Contract requires reproduce.command and verify.required", file=sys.stderr)
            return 1

        cs = ContractStore(store_path)
        try:
            cs.save_contract(task_id, contract)
        except FileExistsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        task.contract_hash = contract.content_hash
        store.save_task(task)
        print(f"contract initialized for {task_id}")
        print(f"content_hash: {contract.content_hash[:16]}")
        return 0

    if cmd == "validate":
        if not args:
            print("error: task_id is required", file=sys.stderr)
            return 1
        task_id = args[0]
        cs = ContractStore(_resolve_tasks_root())
        contract = cs.load_contract(task_id)
        if contract is None:
            print("no contract found")
            return 1
        print(f"goal:           {contract.goal}")
        print(f"reproduce:      {contract.reproduce_command or '(none)'}")
        print(f"verify cmds:    {len(contract.verify_commands)}")
        print(f"forbidden:      {contract.change_policy.forbidden_paths}")
        print(f"content_hash:   {contract.content_hash[:16]}")
        return 0

    print(f"error: unknown contract command: {cmd}", file=sys.stderr)
    return 1


def _main_benchmark(bench_argv):
    """Dispatch for 'forge benchmark <name> [args...]'."""
    if not bench_argv:
        print("usage: forge benchmark loop [--artifact PATH] [--fixtures DIR] [--workspace DIR]",
              file=sys.stderr)
        return 1

    name = bench_argv[0].lower()
    if name != "loop":
        print(f"error: unknown benchmark: {name}", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(prog="forge benchmark loop")
    parser.add_argument("--artifact", default=None, help="JSON artifact 输出路径")
    parser.add_argument("--fixtures", default=None, help="fixture 根目录（默认 <cwd>/tests/fixtures）")
    parser.add_argument("--workspace", default=None, help="benchmark 工作区目录（默认临时目录）")
    bargs = parser.parse_args(bench_argv[1:])

    try:
        from .evaluation.loop_benchmark import run_loop_benchmark
        report = run_loop_benchmark(
            fixtures_root=bargs.fixtures,
            workspace_root=bargs.workspace,
            artifact_path=bargs.artifact,
        )
    except Exception as exc:
        print(f"error: benchmark failed: {exc}", file=sys.stderr)
        return 1

    summary = {k: v for k, v in report.items() if k != "results"}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if bargs.artifact:
        print(f"artifact saved: {bargs.artifact}")
    return 0


def _resolve_tasks_root() -> Path:
    """返回当前工作区 Durable Task 文件根目录。"""
    store = _task_store_for_workspace(Path.cwd())
    tasks_root = store.root / "tasks"
    tasks_root.mkdir(parents=True, exist_ok=True)
    return tasks_root


def _drain_idle_worker_notifications(agent):
    notifications = agent.engine.drain_worker_notifications()
    for notification in notifications:
        print(f"\n[worker notification]\n{notification}")
    return notifications


def interaction_mode(args):
    if args.prompt:
        return "one_shot"
    if getattr(args, "repl", False):
        return "repl"
    if getattr(args, "tui", False) or sys.stdin.isatty():
        return "tui"
    return "repl"


def main(argv=None):
    # Detect "forge task ..." / "forge contract ..." / "forge loop ..." / "forge benchmark ..."
    # before argparse
    if argv is None:
        argv = sys.argv[1:]
    first = argv[0].lower() if argv else ""
    if first == "task":
        return _main_task(argv[1:])
    if first == "contract":
        return _main_contract(argv[1:])
    if first == "loop":
        return _main_loop(argv[1:])
    if first == "benchmark":
        return _main_benchmark(argv[1:])

    args = build_arg_parser().parse_args(argv)

    # -- Normal forge flow -----------------------------------------------
    try:
        agent = build_agent(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    mode = interaction_mode(args)
    if mode == "tui":
        from .tui.app import ForgeTuiApp

        ForgeTuiApp(agent).run()
        return 0

    model = getattr(
        agent.model_client, "model", getattr(args, "model", DEFAULT_OPENAI_MODEL)
    )
    host = getattr(
        agent.model_client,
        "base_url",
        getattr(args, "base_url", DEFAULT_OPENAI_BASE_URL),
    )
    print(build_welcome(agent, model=model, host=host))

    if mode == "one_shot":
        # one-shot 模式：只跑一次 ask，不进入 REPL 循环。
        prompt = " ".join(args.prompt).strip()
        if prompt:
            print()
            try:
                handled, _, output = handle_repl_command(agent, prompt)
                print(output if handled else agent.ask(prompt))
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        return 0

    while True:
        # 交互模式：每次读取一条用户输入，交给同一个 agent，
        # 因此 session history 和 working memory 会跨轮延续。
        _drain_idle_worker_notifications(agent)
        try:
            user_input = input("\nforge> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0

        if not user_input:
            continue
        handled, should_exit, output = handle_repl_command(agent, user_input)
        if should_exit:
            return 0
        if handled:
            print(output)
            continue

        print()
        try:
            print(agent.ask(user_input))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
