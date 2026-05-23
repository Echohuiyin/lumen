"""验证工作流各组件（无需 LLM API Key）。"""

import sys

from agents.parsers import (
    parse_coordinator_response,
    parse_executor_response,
    parse_reviewer_response,
)
from graph.router import (
    route_after_coordinator,
    route_after_executor,
    route_after_reviewer,
)
from langgraph.checkpoint.memory import MemorySaver
from graph.workflow import build_workflow
from tools.project_tools import read_file, run_shell_command, search_code, write_file


def test_parsers():
    coord_plan = parse_coordinator_response("分析如下\nTASK_PLAN:\n读取 main.py 并统计行数")
    assert coord_plan["mode"] == "plan"
    assert "main.py" in coord_plan["task_plan"]

    coord_ask = parse_coordinator_response("请补充\nUSER_QUESTION:\n请问要读取哪个文件？")
    assert coord_ask["mode"] == "ask_user"

    coord_final = parse_coordinator_response("完成\nFINAL_RESPONSE:\n共 100 行")
    assert coord_final["mode"] == "summarize"
    assert "100" in coord_final["final_response"]

    exec_ok = parse_executor_response("完成\nSTATUS: success\nRESULT:\n文件共 85 行")
    assert exec_ok["status"] == "success"

    exec_fail = parse_executor_response("失败\nSTATUS: failed\nERROR:\n权限不足")
    assert exec_fail["status"] == "failed"

    exec_fail_over_ask = parse_executor_response(
        "需要澄清\nSTATUS: need_user_input\nQUESTION:\n哪个目录？\nSTATUS: failed\nERROR:\nx"
    )
    assert exec_fail_over_ask["status"] == "failed"

    rev_ok = parse_reviewer_response("通过\nREVIEW: approved\nSUMMARY:\n结果正确")
    assert rev_ok["status"] == "approved"

    rev_no = parse_reviewer_response("不通过\nREVIEW: rejected\nFEEDBACK:\n缺少行数统计")
    assert rev_no["status"] == "rejected"
    print("[OK] parsers")


def test_tools():
    content = read_file.invoke({"path": "main.py"})
    assert "run_workflow" in content

    result = run_shell_command.invoke({"command": "wc -l main.py"})
    assert "main.py" in result

    matches = search_code.invoke({"pattern": "build_workflow"})
    assert "build_workflow" in matches

    write_file.invoke({"path": "_test_tmp.txt", "content": "hello"})
    written = read_file.invoke({"path": "_test_tmp.txt"})
    assert written == "hello"
    print("[OK] tools")


def test_routing():
    assert route_after_coordinator({"next_node": "executor"}) == "executor"
    assert route_after_coordinator({"next_node": "end"}) == "__end__"
    assert route_after_coordinator({"next_node": "clarify"}) == "coordinator_clarify"

    assert route_after_executor({"executor_status": "failed"}) == "coordinator"
    assert route_after_executor({"executor_status": "success"}) == "reviewer"

    assert route_after_reviewer({"review_status": "rejected", "retry_count": 1}) == "executor"
    assert route_after_reviewer({"review_status": "rejected", "retry_count": 3}) == "coordinator"
    assert route_after_reviewer({"review_status": "approved", "retry_count": 0}) == "coordinator"
    print("[OK] routing")


def test_graph_compile():
    graph = build_workflow(checkpointer=MemorySaver())
    nodes = set(graph.get_graph().nodes.keys())
    assert "coordinator" in nodes
    assert "coordinator_clarify" in nodes
    assert "executor" in nodes
    assert "reviewer" in nodes
    print("[OK] graph compile")


if __name__ == "__main__":
    test_parsers()
    test_tools()
    test_routing()
    test_graph_compile()
    print("\n所有组件验证通过。")
    sys.exit(0)
