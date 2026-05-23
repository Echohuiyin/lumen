from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from agents.llm_display import call_llm_with_display, print_tool_call, print_tool_result
from agents.parsers import parse_executor_response
from config import get_llm, load_prompt
from graph.state import WorkflowState
from tools.project_tools import PROJECT_TOOLS

MAX_TOOL_ITERATIONS = 10


def _run_tool_loop(llm, messages: list) -> str:
    llm_with_tools = llm.bind_tools(PROJECT_TOOLS)
    tool_map = {tool.name: tool for tool in PROJECT_TOOLS}

    for step in range(1, MAX_TOOL_ITERATIONS + 1):
        response = call_llm_with_display(
            "Executor",
            f"思考并执行 (第 {step} 轮)",
            llm_with_tools,
            messages,
        )
        messages.append(response)

        if not response.tool_calls:
            return response.content

        for tool_call in response.tool_calls:
            tool_fn = tool_map.get(tool_call["name"])
            print_tool_call(tool_call["name"], tool_call["args"])
            if tool_fn is None:
                result = f"错误: 未知工具 {tool_call['name']}"
            else:
                try:
                    result = tool_fn.invoke(tool_call["args"])
                except Exception as exc:
                    result = f"工具执行异常: {exc}"
            print_tool_result(str(result))
            messages.append(
                ToolMessage(content=str(result), tool_call_id=tool_call["id"])
            )

    return messages[-1].content if messages else ""


def executor_node(state: WorkflowState) -> dict:
    llm = get_llm()
    system_prompt = load_prompt("executor")

    task_context = state.get("task_plan", "")
    if state.get("review_status") == "rejected" and state.get("review_feedback"):
        task_context = (
            f"{task_context}\n\n"
            f"审核反馈（请根据以下建议重新执行）:\n{state['review_feedback']}"
        )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"任务计划:\n{task_context}"),
    ]

    raw_response = _run_tool_loop(llm, messages)
    parsed = parse_executor_response(raw_response)

    return {
        "messages": [HumanMessage(content=f"[Executor] {raw_response}")],
        "execution_result": parsed["content"],
        "executor_status": parsed["status"],
    }
