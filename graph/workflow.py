from langgraph.graph import END, START, StateGraph

from agents.coordinator import coordinator_clarify_node, coordinator_node
from agents.executor import executor_node
from agents.reviewer import reviewer_node
from graph.router import (
    route_after_coordinator,
    route_after_coordinator_clarify,
    route_after_executor,
    route_after_reviewer,
)
from graph.state import WorkflowState


def build_workflow(*, checkpointer=None):
    builder = StateGraph(WorkflowState)

    builder.add_node("coordinator", coordinator_node)
    builder.add_node("coordinator_clarify", coordinator_clarify_node)
    builder.add_node("executor", executor_node)
    builder.add_node("reviewer", reviewer_node)

    builder.add_edge(START, "coordinator")
    builder.add_conditional_edges("coordinator", route_after_coordinator)
    builder.add_conditional_edges("coordinator_clarify", route_after_coordinator_clarify)
    builder.add_conditional_edges("executor", route_after_executor)
    builder.add_conditional_edges("reviewer", route_after_reviewer)

    if checkpointer is not None:
        return builder.compile(checkpointer=checkpointer)
    return builder.compile()


# LangGraph Studio / `langgraph dev` 入口（持久化由平台自动处理）
graph = build_workflow()
