"""Coordinator subagent tool definitions."""

from ..core.worker_manager import dumps_payload

AGENT_TOOL_NAMES = {"agent", "send_message", "task_stop"}

AGENT_TOOL_SPECS = {
    "agent": {
        "schema": {
            "description": "str",
            "prompt": "str",
            "subagent_type": "str='worker'",
            "write_scope": "list[str]=[]",
        },
        "risky": False,
        "description": "Launch a bounded worker or read-only Explore subagent.",
    },
    "send_message": {
        "schema": {"to": "str", "message": "str"},
        "risky": False,
        "description": "Continue an existing idle worker by id.",
    },
    "task_stop": {
        "schema": {"task_id": "str"},
        "risky": False,
        "description": "Stop a worker by id.",
    },
}

AGENT_TOOL_EXAMPLES = {
    "agent": '<tool>{"name":"agent","args":{"description":"Inspect auth","prompt":"Find auth entry points","subagent_type":"Explore"}}</tool>',
    "send_message": '<tool>{"name":"send_message","args":{"to":"agent_1","message":"Now patch the bug in src/auth.py"}}</tool>',
    "task_stop": '<tool>{"name":"task_stop","args":{"task_id":"agent_1"}}</tool>',
}


def validate_agent_tool(agent, name, args):
    if name == "agent":
        if not str(args.get("description", "")).strip():
            raise ValueError("description must not be empty")
        if not str(args.get("prompt", "")).strip():
            raise ValueError("prompt must not be empty")
        subagent_type = str(args.get("subagent_type", "worker")).strip()
        if subagent_type not in {"worker", "Explore"}:
            raise ValueError("subagent_type must be worker or Explore")
        if agent.runtime_mode == "plan" and subagent_type != "Explore":
            raise ValueError("plan mode only allows Explore agents")
        write_scope = args.get("write_scope", [])
        if write_scope is not None and not isinstance(write_scope, (list, str)):
            raise ValueError("write_scope must be a list of workspace paths")
        return
    if name == "send_message":
        if not str(args.get("to", "")).strip():
            raise ValueError("to must not be empty")
        if not str(args.get("message", "")).strip():
            raise ValueError("message must not be empty")
        return
    if name == "task_stop" and not str(args.get("task_id", "")).strip():
        raise ValueError("task_id must not be empty")


def tool_agent(agent, args):
    return dumps_payload(
        agent.worker_manager.spawn(
            args["description"],
            args["prompt"],
            subagent_type=args.get("subagent_type", "worker"),
            write_scope=args.get("write_scope", []),
        )
    )


def tool_send_message(agent, args):
    return dumps_payload(agent.worker_manager.continue_task(args["to"], args["message"]))


def tool_task_stop(agent, args):
    return dumps_payload(agent.worker_manager.stop_task(args["task_id"]))
