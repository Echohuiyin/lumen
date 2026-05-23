from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import interrupt

from agents.llm_display import call_llm_with_display
from agents.parsers import parse_coordinator_response
from config import MAX_RETRIES, get_llm, load_prompt
from graph.state import WorkflowState

PHASE_LABELS = {
    "plan": "思考并制定任务计划",
    "summarize": "思考并汇总最终结果",
    "summarize_force": "思考并汇总（审核未通过）",
    "replan": "思考并重新制定计划",
    "clarify_replan": "思考并根据用户补充重新规划",
}


def _determine_mode(state: WorkflowState) -> str:
    if state.get("review_status") == "approved":
        return "summarize"
    if state.get("retry_count", 0) >= MAX_RETRIES and state.get("review_status") == "rejected":
        return "summarize_force"
    if state.get("executor_status") == "failed":
        return "replan"
    if state.get("coordinator_mode") == "clarify_replan":
        return "clarify_replan"
    return "plan"


def _build_planning_prompt(state: WorkflowState, mode: str) -> str:
    if mode == "clarify_replan":
        return (
            f"用户原始指令:\n{state['user_request']}\n\n"
            "请根据目前已有的完整信息继续任务规划。"
            "若仍信息不足，使用 USER_QUESTION: 继续澄清。"
            "若任务无法完成，使用 FINAL_RESPONSE: 直接说明原因。"
            "仅当计划完整可执行时使用 TASK_PLAN: 标记。"
        )
    if mode == "replan":
        return (
            f"原始用户需求:\n{state['user_request']}\n\n"
            f"上次任务计划:\n{state.get('task_plan', '')}\n\n"
            f"执行失败原因:\n{state.get('execution_result', '')}\n\n"
            "请根据失败原因重新制定可自动执行的任务计划。"
            "执行阶段无法向用户提问，计划必须完整可执行。使用 TASK_PLAN: 标记。"
        )
    return (
        f"用户指令:\n{state['user_request']}\n\n"
        "请分析指令并制定详细、可执行的任务计划。"
        "若信息不足无法制定计划，使用 USER_QUESTION: 向用户澄清。"
        "若任务无法完成，使用 FINAL_RESPONSE: 直接说明原因。"
        "仅当计划完整可执行时使用 TASK_PLAN: 标记。"
    )


def _apply_planning_result(state: WorkflowState, response, parsed: dict) -> dict:
    if parsed["mode"] == "summarize":
        return {
            "messages": [response],
            "final_response": parsed["final_response"],
            "next_node": "end",
            "coordinator_mode": "summarize",
            "clarify_question": "",
        }

    if parsed["mode"] == "ask_user":
        return {
            "messages": [response],
            "clarify_question": parsed.get("question") or "请补充更多信息以便制定任务计划。",
            "next_node": "clarify",
            "coordinator_mode": "plan",
        }

    if parsed["mode"] == "plan" and parsed.get("task_plan", "").strip():
        return {
            "messages": [response],
            "task_plan": parsed["task_plan"],
            "executor_status": None,
            "review_status": None,
            "next_node": "executor",
            "coordinator_mode": "plan",
            "clarify_question": "",
        }

    fallback = parsed.get("final_response") or parsed.get("question") or response.content
    return {
        "messages": [response],
        "final_response": fallback.strip() or "无法制定可执行的任务计划，请补充更明确的指令。",
        "next_node": "end",
        "coordinator_mode": "plan_failed",
        "clarify_question": "",
    }


def coordinator_clarify_node(state: WorkflowState) -> dict:
    question = state.get("clarify_question") or "请补充更多信息以便制定任务计划。"
    user_reply = interrupt({"question": question, "phase": "planning"})
    if isinstance(user_reply, dict):
        user_reply = user_reply.get("user_reply", str(user_reply))
    updated_request = f"{state['user_request']}\n\n用户补充: {user_reply}"
    return {
        "user_request": updated_request,
        "coordinator_mode": "clarify_replan",
        "next_node": "coordinator",
        "clarify_question": "",
    }


def coordinator_node(state: WorkflowState) -> dict:
    llm = get_llm()
    system_prompt = load_prompt("coordinator")
    mode = _determine_mode(state)

    if mode in ("plan", "replan", "clarify_replan"):
        allow_user_questions = mode in ("plan", "clarify_replan")
        response = call_llm_with_display(
            "Coordinator",
            PHASE_LABELS.get(mode, PHASE_LABELS["plan"]),
            llm,
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=_build_planning_prompt(state, mode)),
            ],
        )
        parsed = parse_coordinator_response(response.content)
        if not allow_user_questions and parsed["mode"] == "ask_user":
            parsed = {
                "mode": "summarize",
                "final_response": "执行阶段无法向用户澄清，且当前无法自动生成可执行计划。",
            }
        return _apply_planning_result(state, response, parsed)

    if mode == "summarize":
        user_content = (
            f"原始用户需求:\n{state['user_request']}\n\n"
            f"任务计划:\n{state.get('task_plan', '')}\n\n"
            f"执行结果:\n{state.get('execution_result', '')}\n\n"
            f"审核摘要:\n{state.get('review_feedback', '')}\n\n"
            "请汇总以上信息，生成给用户的最终回复。使用 FINAL_RESPONSE: 标记。"
        )
    else:
        user_content = (
            f"原始用户需求:\n{state['user_request']}\n\n"
            f"执行结果:\n{state.get('execution_result', '')}\n\n"
            f"审核反馈（已重试 {state.get('retry_count', 0)} 次仍未通过）:\n"
            f"{state.get('review_feedback', '')}\n\n"
            "请汇总当前最佳结果并告知用户审核未完全通过的情况。使用 FINAL_RESPONSE: 标记。"
        )

    response = call_llm_with_display(
        "Coordinator",
        PHASE_LABELS.get(mode, PHASE_LABELS["summarize"]),
        llm,
        [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
    )
    parsed = parse_coordinator_response(response.content)

    return {
        "messages": [response],
        "final_response": parsed.get("final_response", response.content),
        "next_node": "end",
        "coordinator_mode": "summarize",
        "clarify_question": "",
    }
