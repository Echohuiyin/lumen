from langchain_core.messages import HumanMessage, SystemMessage

from agents.parsers import parse_reviewer_response
from config import get_llm, load_prompt
from graph.state import WorkflowState


def reviewer_node(state: WorkflowState) -> dict:
    llm = get_llm()
    system_prompt = load_prompt("reviewer")

    user_content = (
        f"原始用户需求:\n{state['user_request']}\n\n"
        f"任务计划:\n{state.get('task_plan', '')}\n\n"
        f"执行结果:\n{state.get('execution_result', '')}\n\n"
        "请审核执行结果是否符合预期。"
    )

    print(f"\n[Reviewer] 开始审核执行结果...")
    response = llm.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=user_content)]
    )
    parsed = parse_reviewer_response(response.content)

    print(f"[Reviewer] 审核结果: {parsed['status']}")

    result: dict = {
        "messages": [response],
        "review_status": parsed["status"],
        "review_feedback": parsed["feedback"],
    }

    if parsed["status"] == "rejected":
        result["retry_count"] = state.get("retry_count", 0) + 1

    return result
