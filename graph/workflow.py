from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from agents.coordinator import coordinator_node
from agents.executor import executor_node
from agents.reviewer import reviewer_node
from graph.router import (
    route_after_coordinator,
    route_after_executor,
    route_after_reviewer,
)
from graph.state import WorkflowState


def build_workflow():
    builder = StateGraph(WorkflowState)

    builder.add_node("coordinator", coordinator_node)
    builder.add_node("executor", executor_node)
    builder.add_node("reviewer", reviewer_node)

    builder.add_edge(START, "coordinator")
    builder.add_conditional_edges("coordinator", route_after_coordinator)
    builder.add_conditional_edges("executor", route_after_executor)
    builder.add_conditional_edges("reviewer", route_after_reviewer)

    checkpointer = MemorySaver()
    return builder.compile(checkpointer=checkpointer)
