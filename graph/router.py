from langgraph.graph import END
from langgraph.types import Send

from config import MAX_PARALLEL_TASKS, MAX_RETRIES
from graph.state import WorkflowState


def _resolve_task_items(state: WorkflowState) -> list[str]:
    task_items = state.get("task_items") or []
    if task_items:
        return task_items
    task_plan = state.get("task_plan", "").strip()
    return [task_plan] if task_plan else [""]


def has_pending_batches(state: WorkflowState) -> bool:
    task_items = _resolve_task_items(state)
    offset = state.get("task_batch_offset", 0)
    return offset + MAX_PARALLEL_TASKS < len(task_items)


def dispatch_executors(state: WorkflowState) -> list[Send]:
    task_items = _resolve_task_items(state)
    offset = state.get("task_batch_offset", 0)
    batch = task_items[offset : offset + MAX_PARALLEL_TASKS]
    return [
        Send("executor", {"current_task": task, "task_index": offset + index})
        for index, task in enumerate(batch)
        if task.strip()
    ]


def route_after_coordinator(state: WorkflowState):
    next_node = state.get("next_node", "executor")
    if next_node == "end":
        return END
    if next_node == "clarify":
        return "coordinator_clarify"
    if next_node == "coordinator":
        return "coordinator"
    return dispatch_executors(state)


def route_after_coordinator_clarify(state: WorkflowState) -> str:
    return "coordinator"


def route_after_executor_aggregate(state: WorkflowState) -> str:
    if state.get("executor_status") == "failed":
        return "coordinator"
    if has_pending_batches(state):
        return "batch_advance"
    return "reviewer"


def route_after_batch_advance(state: WorkflowState):
    return dispatch_executors(state)


def route_after_reviewer(state: WorkflowState):
    if (
        state.get("review_status") == "rejected"
        and state.get("retry_count", 0) < MAX_RETRIES
    ):
        return dispatch_executors(state)
    return "summarizer"
