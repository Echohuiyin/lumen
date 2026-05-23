from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import interrupt

from agents.parsers import parse_coordinator_response
from config import MAX_RETRIES, get_llm, load_prompt
from graph.state import WorkflowState


def _determine_mode(state: WorkflowState) -> str:
    if state.get("review_status") == "approved":
        return "summarize"
    if state.get("retry_count", 0) >= MAX_RETRIES and state.get("review_status") == "rejected":
        return "summarize_force"
    if state.get("executor_status") == "need_user_input":
        return "ask_user"
    if state.get("executor_status") == "failed":
        return "replan"
    return "plan"


def coordinator_node(state: WorkflowState) -> dict:
    llm = get_llm()
    system_prompt = load_prompt("coordinator")
    mode = _determine_mode(state)

    if mode == "ask_user":
        question = state.get("execution_result", "")
        user_reply = interrupt({"question": question})
        if isinstance(user_reply, dict):
            user_reply = user_reply.get("user_reply", str(user_reply))
        updated_request = f"{state['user_request']}\n\n用户补充: {user_reply}"
        replan_response = llm.invoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(
                    content=(
                        f"用户原始指令:\n{state['user_request']}\n\n"
                        f"用户补充信息:\n{user_reply}\n\n"
                        "请根据完整信息重新制定任务计划。使用 TASK_PLAN: 标记。"
                    )
                ),
            ]
        )
        replan_parsed = parse_coordinator_response(replan_response.content)
        return {
            "messages": [replan_response],
            "user_request": updated_request,
            "task_plan": replan_parsed.get("task_plan", replan_response.content),
            "executor_status": None,
            "review_status": None,
            "next_node": "executor",
            "coordinator_mode": "plan",
        }

    if mode == "summarize":
        user_content = (
            f"原始用户需求:\n{state['user_request']}\n\n"
            f"任务计划:\n{state.get('task_plan', '')}\n\n"
            f"执行结果:\n{state.get('execution_result', '')}\n\n"
            f"审核摘要:\n{state.get('review_feedback', '')}\n\n"
            "请汇总以上信息，生成给用户的最终回复。使用 FINAL_RESPONSE: 标记。"
        )
    elif mode == "summarize_force":
        user_content = (
            f"原始用户需求:\n{state['user_request']}\n\n"
            f"执行结果:\n{state.get('execution_result', '')}\n\n"
            f"审核反馈（已重试 {state.get('retry_count', 0)} 次仍未通过）:\n"
            f"{state.get('review_feedback', '')}\n\n"
            "请汇总当前最佳结果并告知用户审核未完全通过的情况。使用 FINAL_RESPONSE: 标记。"
        )
    elif mode == "replan":
        user_content = (
            f"原始用户需求:\n{state['user_request']}\n\n"
            f"上次任务计划:\n{state.get('task_plan', '')}\n\n"
            f"执行失败原因:\n{state.get('execution_result', '')}\n\n"
            "请根据失败原因重新制定任务计划。使用 TASK_PLAN: 标记。"
        )
    else:
        user_content = (
            f"用户指令:\n{state['user_request']}\n\n"
            "请分析指令并制定详细的任务计划。使用 TASK_PLAN: 标记。"
        )

    response = llm.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=user_content)]
    )
    parsed = parse_coordinator_response(response.content)

    if parsed["mode"] == "summarize" or mode in ("summarize", "summarize_force"):
        return {
            "messages": [response],
            "final_response": parsed["final_response"],
            "next_node": "end",
            "coordinator_mode": "summarize",
        }

    return {
        "messages": [response],
        "task_plan": parsed.get("task_plan", response.content),
        "executor_status": None,
        "next_node": "executor",
        "coordinator_mode": "plan",
    }
