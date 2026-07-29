"""Turn-level runtime engine.

The runtime owns state and persistence. Engine owns the control loop that turns
one user request into model calls, tool executions, and user-visible events.
"""

import time

from ..providers.base import complete_model
from . import bug_fix_ledger
from .model_errors import finish_model_error
from .engine_helpers import (
    execute_tool_payload,
    finish_limited_run,
    finish_stopped_run,
    maintain_memory_safely,
    request_step_limit_summary,
    should_retry_model_error,
)
from .run_result import RunResult
from .task_state import TaskState
from .workspace import clip, now

CHECKPOINT_NONE_STATUS = "no-checkpoint"
CHECKPOINT_PARTIAL_STALE_STATUS = "partial-stale"
CHECKPOINT_WORKSPACE_MISMATCH_STATUS = "workspace-mismatch"


class Engine:
    def __init__(self, runtime):
        self.runtime = runtime

    def ask(self, user_message):
        final_answer = ""
        for event in self.run_turn(user_message):
            if event["type"] in {"final", "stop"}:
                final_answer = event["content"]
        return final_answer

    def ask_run(self, user_message, on_run_started=None) -> RunResult:
        """运行单个 turn，并返回供外层持久化使用的结构化结果。

        ``on_run_started`` 在 Run 身份创建后、任何 Harness 工作开始前调用。
        Durable Task 调用方应在该回调中落盘 active Run；若回调失败，Run
        不会继续执行，从而避免出现无法恢复的未关联 artifact。
        """
        result = RunResult()
        for event in self.run_turn(user_message, on_run_started=on_run_started):
            if event["type"] == "turn_started":
                result.run_id = event.get("run_id", "")
            if event["type"] in {"final", "stop"}:
                result.final_answer = event.get("content", "")
        ts = self.runtime.current_task_state
        if ts:
            result.legacy_engine_task_id = getattr(ts, "task_id", "")
            result.outcome = self._outcome(ts)
            result.metadata["tool_steps"] = getattr(ts, "tool_steps", 0)
            result.metadata["attempts"] = getattr(ts, "attempts", 0)
            result.metadata["stop_reason"] = getattr(ts, "stop_reason", "")
        return result

    @staticmethod
    def _outcome(task_state) -> str:
        status = getattr(task_state, "status", "") or ""
        stop = getattr(task_state, "stop_reason", "") or ""
        if status == "completed":
            return "completed"
        if stop in ("step_limit_reached", "retry_limit_reached"):
            return "stopped"
        if status == "failed":
            return "failed"
        return "completed"

    def drain_worker_notifications(self):
        agent = self.runtime
        notifications = agent.worker_manager.drain_notifications()
        for notification in notifications:
            agent.record({"role": "user", "content": notification, "created_at": now()})
            agent.session_event_bus.emit(
                "worker_notification_drained",
                {
                    "run_id": getattr(agent, "current_run_id", ""),
                    "content": clip(notification, 500),
                },
            )
        return notifications

    def _drain_worker_notification_events(self):
        for notification in self.drain_worker_notifications():
            yield {
                "type": "worker_notification",
                "run_id": getattr(self.runtime, "current_run_id", ""),
                "content": notification,
            }

    def run_turn(self, user_message, on_run_started=None):
        """执行一个 Run；可选回调在任何执行副作用前取得 Run 身份。"""
        agent = self.runtime
        run_started_at = time.monotonic()
        task_state = TaskState.create(
            run_id=agent.new_run_id(),
            task_id=agent.new_task_id(),
            user_request=user_message,
        )
        task_state.bug_fix_ledger = bug_fix_ledger.create_ledger(user_message)
        task_state.resume_status = agent.resume_state.get(
            "status", CHECKPOINT_NONE_STATUS
        )
        agent.current_task_state = task_state
        agent.current_turn_id = task_state.task_id
        agent.current_run_id = task_state.run_id
        if on_run_started is not None:
            on_run_started(task_state.run_id, task_state.task_id)
        agent.current_run_dir = agent.run_store.start_run(task_state)
        agent.session_event_bus.emit(
            "turn_started",
            {
                "run_id": task_state.run_id,
                "task_id": task_state.task_id,
                "runtime_mode": agent.runtime_mode,
            },
        )
        yield {
            "type": "turn_started",
            "run_id": task_state.run_id,
            "task_id": task_state.task_id,
        }

        agent.memory.set_task_summary(user_message)
        agent.record({"role": "user", "content": user_message, "created_at": now()})
        agent.session_event_bus.emit(
            "user_message",
            {"run_id": task_state.run_id, "content": clip(user_message, 300)},
        )
        agent.emit_trace(
            task_state,
            "run_started",
            {
                "task_id": task_state.task_id,
                "user_request": clip(user_message, 300),
            },
        )

        tool_steps = 0
        attempts = 0
        provider_retries = {}
        # 不放大 attempts，避免出现"看不见的隐形重试"——失败必须被用户察觉。
        max_attempts = agent.max_steps + 2

        while tool_steps < agent.max_steps and attempts < max_attempts:
            if agent.abort_requested:
                yield from finish_stopped_run(
                    self,
                    task_state,
                    user_message,
                    "Stopped after abort request.",
                    "aborted",
                    run_started_at,
                )
                return
            yield from self._drain_worker_notification_events()
            attempts += 1
            task_state.record_attempt()
            agent.run_store.write_task_state(task_state)
            prompt_started_at = time.monotonic()
            prompt, prompt_metadata = agent._build_prompt_and_metadata(user_message)
            agent.emit_trace(
                task_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                },
            )
            if prompt_metadata.get("resume_status") == CHECKPOINT_PARTIAL_STALE_STATUS:
                checkpoint = agent.create_checkpoint(
                    task_state, user_message, trigger="freshness_mismatch"
                )
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "freshness_mismatch",
                    },
                )
            elif (
                prompt_metadata.get("resume_status")
                == CHECKPOINT_WORKSPACE_MISMATCH_STATUS
            ):
                agent.emit_trace(
                    task_state,
                    "runtime_identity_mismatch",
                    {
                        "fields": list(
                            prompt_metadata.get("runtime_identity_mismatch_fields", [])
                        ),
                    },
                )
                checkpoint = agent.create_checkpoint(
                    task_state, user_message, trigger="workspace_mismatch"
                )
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "workspace_mismatch",
                    },
                )
            if prompt_metadata.get("budget_reductions"):
                checkpoint = agent.create_checkpoint(
                    task_state, user_message, trigger="context_reduction"
                )
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "context_reduction",
                    },
                )
            agent.emit_trace(
                task_state,
                "model_requested",
                {
                    "attempts": task_state.attempts,
                    "tool_steps": task_state.tool_steps,
                    "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                },
            )
            agent.session_event_bus.emit(
                "model_requested",
                {
                    "run_id": task_state.run_id,
                    "attempts": task_state.attempts,
                    "tool_steps": task_state.tool_steps,
                },
            )
            yield {
                "type": "model_requested",
                "run_id": task_state.run_id,
                "attempts": task_state.attempts,
                "tool_steps": task_state.tool_steps,
            }

            prompt_cache_key = None
            prompt_cache_retention = None
            if getattr(agent.model_client, "supports_prompt_cache", False):
                prompt_cache_key = prompt_metadata.get("prompt_cache_key")
                prompt_cache_retention = "in_memory"

            model_started_at = time.monotonic()
            agent.run_hooks(
                "before_model",
                task_state=task_state,
                user_message=user_message,
                prompt=prompt,
                prompt_metadata=dict(prompt_metadata),
                attempts=task_state.attempts,
                tool_steps=task_state.tool_steps,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
            )
            try:
                result = complete_model(
                    agent.model_client,
                    prompt,
                    agent.max_new_tokens,
                    prompt_cache_key=prompt_cache_key,
                    prompt_cache_retention=prompt_cache_retention,
                )
            except Exception as exc:
                if agent.abort_requested:
                    yield from finish_stopped_run(
                        self,
                        task_state,
                        user_message,
                        "Stopped after abort request.",
                        "aborted",
                        run_started_at,
                    )
                    return
                if should_retry_model_error(exc, provider_retries):
                    code = getattr(exc, "code", type(exc).__name__)
                    provider_retries[code] = provider_retries.get(code, 0) + 1
                    agent.session_event_bus.emit(
                        "model_retry_scheduled",
                        {
                            "run_id": task_state.run_id,
                            "code": code,
                            "attempts": task_state.attempts,
                            "retry_count": provider_retries[code],
                        },
                    )
                    agent.emit_trace(
                        task_state,
                        "model_retry_scheduled",
                        {
                            "code": code,
                            "duration_ms": int(
                                (time.monotonic() - model_started_at) * 1000
                            ),
                            "retry_count": provider_retries[code],
                        },
                    )
                    agent.run_hooks(
                        "on_error",
                        task_state=task_state,
                        error=exc,
                        error_metadata={"code": code, "retry_count": provider_retries[code]},
                        prompt_metadata=dict(prompt_metadata),
                        user_message=user_message,
                        duration_ms=int(
                            (time.monotonic() - model_started_at) * 1000
                        ),
                        will_retry=True,
                    )
                    continue
                yield from finish_model_error(
                    self,
                    task_state,
                    user_message,
                    prompt_metadata,
                    exc,
                    int((time.monotonic() - model_started_at) * 1000),
                    int((time.monotonic() - run_started_at) * 1000),
                )
                return
            if agent.abort_requested:
                yield from finish_stopped_run(
                    self,
                    task_state,
                    user_message,
                    "Stopped after abort request.",
                    "aborted",
                    run_started_at,
                )
                return
            raw = result.text
            completion_metadata = dict(
                result.metadata
                or getattr(agent.model_client, "last_completion_metadata", {})
                or {}
            )
            if completion_metadata:
                prompt_metadata.update(completion_metadata)
            agent.last_completion_metadata = completion_metadata
            agent.last_prompt_metadata = prompt_metadata
            kind, payload = agent.parse(raw)
            duration_ms = int((time.monotonic() - model_started_at) * 1000)
            agent.run_hooks(
                "after_model",
                task_state=task_state,
                user_message=user_message,
                prompt_metadata=dict(prompt_metadata),
                completion_metadata=dict(completion_metadata),
                raw_output=raw,
                parsed_kind=kind,
                parsed_payload=payload,
                duration_ms=duration_ms,
            )
            agent.emit_trace(
                task_state,
                "model_parsed",
                {
                    "kind": kind,
                    "completion_metadata": completion_metadata,
                    "duration_ms": duration_ms,
                },
            )
            agent.session_event_bus.emit(
                "model_parsed",
                {"run_id": task_state.run_id, "kind": kind, "duration_ms": duration_ms},
            )
            yield {
                "type": "model_parsed",
                "run_id": task_state.run_id,
                "kind": kind,
                "duration_ms": duration_ms,
            }

            if kind in {"tool", "tools"}:
                tools = [payload] if kind == "tool" else list(payload)
                for tool_payload in tools:
                    if tool_steps >= agent.max_steps:
                        break
                    yield from execute_tool_payload(
                        self, task_state, user_message, tool_payload
                    )
                    tool_steps += 1
                    if agent.abort_requested:
                        break
                if agent.abort_requested:
                    yield from finish_stopped_run(
                        self,
                        task_state,
                        user_message,
                        "Stopped after abort request.",
                        "aborted",
                        run_started_at,
                    )
                    return
                continue

            if kind == "retry":
                agent.record(
                    {"role": "assistant", "content": payload, "created_at": now()}
                )
                agent.session_event_bus.emit(
                    "assistant_message",
                    {
                        "run_id": task_state.run_id,
                        "kind": "retry",
                        "content": clip(payload, 500),
                    },
                )
                agent.run_store.write_task_state(task_state)
                yield {"type": "retry", "run_id": task_state.run_id, "content": payload}
                continue

            final = (payload or raw).strip()
            yield from self._drain_worker_notification_events()
            guard_notice = bug_fix_ledger.final_guard_notice(task_state.bug_fix_ledger)
            if guard_notice:
                bug_fix_ledger.mark_guard_triggered(task_state.bug_fix_ledger)
                agent.emit_trace(
                    task_state,
                    "bug_fix_guard_triggered",
                    {
                        "changed_paths": list(task_state.bug_fix_ledger.get("changed_paths", [])),
                        "verify_required": bool(task_state.bug_fix_ledger.get("verify_required")),
                        "notice": guard_notice,
                        "bug_fix_ledger": dict(task_state.bug_fix_ledger),
                    },
                )
                agent.record(
                    {"role": "assistant", "content": guard_notice, "created_at": now()}
                )
                agent.session_event_bus.emit(
                    "assistant_message",
                    {
                        "run_id": task_state.run_id,
                        "kind": "runtime_notice",
                        "content": guard_notice,
                    },
                )
                agent.run_store.write_task_state(task_state)
                yield {
                    "type": "runtime_notice",
                    "run_id": task_state.run_id,
                    "content": guard_notice,
                }
                continue
            if agent.runtime_mode == "plan" and not agent.plan_mode.can_finish():
                notice = agent.plan_mode.final_notice()
                agent.record(
                    {"role": "assistant", "content": notice, "created_at": now()}
                )
                agent.session_event_bus.emit(
                    "assistant_message",
                    {
                        "run_id": task_state.run_id,
                        "kind": "runtime_notice",
                        "content": notice,
                    },
                )
                agent.run_store.write_task_state(task_state)
                yield {
                    "type": "runtime_notice",
                    "run_id": task_state.run_id,
                    "content": notice,
                }
                continue

            agent.record({"role": "assistant", "content": final, "created_at": now()})
            if agent.runtime_mode == "plan":
                agent.exit_plan_mode()
            agent.session_event_bus.emit(
                "assistant_message",
                {
                    "run_id": task_state.run_id,
                    "kind": "final",
                    "content": clip(final, 500),
                },
            )
            task_state.finish_success(final)
            agent.promote_durable_memory(user_message, final)
            maintain_memory_safely(agent, task_state, final)
            checkpoint = agent.create_checkpoint(
                task_state, user_message, trigger="run_finished"
            )
            agent.run_store.write_task_state(task_state)
            agent.emit_trace(
                task_state,
                "checkpoint_created",
                {
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "trigger": "run_finished",
                },
            )
            agent.emit_trace(
                task_state,
                "run_finished",
                {
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "final_answer": final,
                    "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
                },
            )
            agent.session_event_bus.emit(
                "turn_finished",
                {
                    "run_id": task_state.run_id,
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "duration_ms": int((time.monotonic() - run_started_at) * 1000),
                },
            )
            agent.run_hooks(
                "on_final",
                task_state=task_state,
                final_answer=final,
                stop_reason=task_state.stop_reason,
                user_message=user_message,
                status=task_state.status,
                duration_ms=int((time.monotonic() - run_started_at) * 1000),
            )
            agent.run_store.write_report(
                task_state, agent.redact_artifact(agent.build_report(task_state))
            )
            yield from self._drain_worker_notification_events()
            agent.current_turn_id = ""
            agent.current_run_id = ""
            yield {"type": "final", "run_id": task_state.run_id, "content": final}
            yield {
                "type": "turn_finished",
                "run_id": task_state.run_id,
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
            }
            return

        if attempts >= max_attempts and tool_steps < agent.max_steps:
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
            task_state.stop_retry_limit(final)
        else:
            summary = None
            if tool_steps > 0:
                summary = request_step_limit_summary(self, task_state, user_message)
            if summary:
                final = (
                    summary
                    + "\n\n— 已达本轮 step 预算上限（max_steps）。以上是当前进展总结。"
                    "继续工作：在 REPL 输入 /resume 续接本会话，或直接说"
                    "「继续」让我接着干。"
                )
            else:
                final = "Stopped after reaching the step limit without a final answer."
            task_state.stop_step_limit(final)
        yield from finish_limited_run(
            self, task_state, user_message, final, run_started_at
        )
