"""集成测试：模拟 LLM 响应，验证三条工作流路径。"""

import sys
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, AIMessageChunk
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from graph.workflow import build_workflow


def _build_test_graph():
    return build_workflow(checkpointer=MemorySaver())


def _make_llm(responses: list[str]):
    queue = list(responses)
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm

    def next_response() -> AIMessageChunk:
        content = queue.pop(0) if queue else ""
        return AIMessageChunk(content=content)

    mock_llm.stream.side_effect = lambda messages: iter([next_response()])
    mock_llm.invoke.side_effect = lambda messages: AIMessage(
        content=next_response().content
    )
    return mock_llm


def _base_state(user_request: str) -> dict:
    return {
        "messages": [],
        "user_request": user_request,
        "task_plan": "",
        "execution_result": "",
        "review_feedback": "",
        "review_status": None,
        "executor_status": None,
        "retry_count": 0,
        "final_response": "",
        "next_node": "executor",
        "coordinator_mode": "plan",
        "clarify_question": "",
    }


def test_happy_path():
    """正常执行：Coordinator → Executor → Reviewer(approved) → Coordinator(汇总) → END"""
    responses = [
        "TASK_PLAN:\n读取 main.py 并统计行数",
        "STATUS: success\nRESULT:\nmain.py 共 85 行",
        "REVIEW: approved\nSUMMARY:\n结果正确，已完成行数统计",
        "FINAL_RESPONSE:\n已完成 main.py 行数统计，共 85 行。",
    ]
    with patch("agents.coordinator.get_llm", return_value=_make_llm([responses[0], responses[3]])):
        with patch("agents.executor.get_llm", return_value=_make_llm([responses[1]])):
            with patch("agents.reviewer.get_llm", return_value=_make_llm([responses[2]])):
                graph = _build_test_graph()
                config = {"configurable": {"thread_id": "test-happy"}}
                result = graph.invoke(_base_state("读取 main.py 并统计行数"), config)
                assert "85" in result["final_response"]
                assert result["next_node"] == "end"
    print("[OK] happy path")


def test_coordinator_planning_clarification():
    """规划阶段澄清：Coordinator(USER_QUESTION) → interrupt → Coordinator(TASK_PLAN) → ..."""
    responses = [
        "USER_QUESTION:\n请问要读取哪个文件？",
        "TASK_PLAN:\n读取 config.py",
        "STATUS: success\nRESULT:\nconfig.py 共 26 行",
        "REVIEW: approved\nSUMMARY:\n完成",
        "FINAL_RESPONSE:\n已读取 config.py，共 26 行。",
    ]
    with patch("agents.coordinator.get_llm", return_value=_make_llm([responses[0], responses[1], responses[4]])):
        with patch("agents.executor.get_llm", return_value=_make_llm([responses[2]])):
            with patch("agents.reviewer.get_llm", return_value=_make_llm([responses[3]])):
                graph = _build_test_graph()
                config = {"configurable": {"thread_id": "test-plan-ask"}}
                result = graph.invoke(_base_state("读取文件"), config)

                snapshot = graph.get_state(config)
                assert snapshot.next, "应在任务规划阶段 interrupt 处暂停"
                result = graph.invoke(
                    Command(resume={"user_reply": "config.py"}),
                    config,
                )
                assert "26" in result["final_response"]
    print("[OK] coordinator planning clarification")


def test_coordinator_cannot_plan():
    """无法制定计划：Coordinator(FINAL_RESPONSE) → END，不进入 Executor"""
    responses = [
        "FINAL_RESPONSE:\n指令过于模糊，无法制定可执行计划。",
    ]
    with patch("agents.coordinator.get_llm", return_value=_make_llm(responses)):
        graph = _build_test_graph()
        config = {"configurable": {"thread_id": "test-no-plan"}}
        result = graph.invoke(_base_state("做点什么"), config)
        assert "无法" in result["final_response"]
        assert result["next_node"] == "end"
    print("[OK] coordinator cannot plan")


def test_reviewer_reject_retry():
    """Reviewer 拒绝重试：Reviewer(rejected) → Executor(重试) → Reviewer(approved) → END"""
    responses = [
        "TASK_PLAN:\n读取 main.py",
        "STATUS: success\nRESULT:\n文件内容已读取",
        "REVIEW: rejected\nFEEDBACK:\n缺少行数统计，请补充 wc -l 结果",
        "STATUS: success\nRESULT:\nmain.py 共 85 行",
        "REVIEW: approved\nSUMMARY:\n已包含行数",
        "FINAL_RESPONSE:\nmain.py 共 85 行，审核通过。",
    ]
    with patch("agents.coordinator.get_llm", return_value=_make_llm([responses[0], responses[5]])):
        with patch("agents.executor.get_llm", return_value=_make_llm([responses[1], responses[3]])):
            with patch("agents.reviewer.get_llm", return_value=_make_llm([responses[2], responses[4]])):
                graph = _build_test_graph()
                config = {"configurable": {"thread_id": "test-retry"}}
                result = graph.invoke(_base_state("读取 main.py 并统计行数"), config)
                assert result["retry_count"] == 1
                assert "85" in result["final_response"]
    print("[OK] reviewer reject retry")


if __name__ == "__main__":
    test_happy_path()
    test_coordinator_planning_clarification()
    test_coordinator_cannot_plan()
    test_reviewer_reject_retry()
    print("\n所有集成路径验证通过。")
    sys.exit(0)
