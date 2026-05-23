from langgraph.graph import END

from config import MAX_RETRIES
from graph.state import WorkflowState


def route_after_coordinator(state: WorkflowState) -> str:
    next_node = state.get("next_node", "executor")
    if next_node == "end":
        return END
    if next_node == "clarify":
        return "coordinator_clarify"
    return "executor"


def route_after_coordinator_clarify(state: WorkflowState) -> str:
    return "coordinator"


def route_after_executor(state: WorkflowState) -> str:
    if state.get("executor_status") == "failed":
        return "coordinator"
    return "reviewer"


def route_after_reviewer(state: WorkflowState) -> str:
    if (
        state.get("review_status") == "rejected"
        and state.get("retry_count", 0) < MAX_RETRIES
    ):
        return "executor"
    return "coordinator"
