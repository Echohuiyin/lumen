from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from agents.parsers import parse_executor_response
from config import get_llm, load_prompt
from graph.state import WorkflowState
from tools.project_tools import PROJECT_TOOLS

MAX_TOOL_ITERATIONS = 10


def _run_tool_loop(llm, messages: list) -> str:
    llm_with_tools = llm.bind_tools(PROJECT_TOOLS)
    tool_map = {tool.name: tool for tool in PROJECT_TOOLS}

    for _ in range(MAX_TOOL_ITERATIONS):
        response = llm_with_tools.invoke(messages)
        messages.append(response)

        if not response.tool_calls:
            return response.content

        for tool_call in response.tool_calls:
            tool_fn = tool_map.get(tool_call["name"])
            if tool_fn is None:
                result = f"错误: 未知工具 {tool_call['name']}"
            else:
                try:
                    result = tool_fn.invoke(tool_call["args"])
                except Exception as exc:
                    result = f"工具执行异常: {exc}"
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

    print(f"\n[Executor] 开始执行任务...")
    raw_response = _run_tool_loop(llm, messages)
    parsed = parse_executor_response(raw_response)

    print(f"[Executor] 状态: {parsed['status']}")

    return {
        "messages": [HumanMessage(content=f"[Executor] {raw_response}")],
        "execution_result": parsed["content"],
        "executor_status": parsed["status"],
    }
