import operator
from typing import Annotated, Literal, Optional

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class WorkflowState(TypedDict):
    messages: Annotated[list, add_messages]
    user_request: str
    task_plan: str
    task_items: list[str]
    current_task: str
    task_index: int
    task_batch_offset: int
    execution_results: Annotated[list[dict], operator.add]
    execution_result: str
    review_feedback: str
    review_status: Optional[Literal["approved", "rejected"]]
    executor_status: Optional[Literal["success", "failed"]]
    retry_count: int
    final_response: str
    next_node: str
    coordinator_mode: str
    clarify_question: str
