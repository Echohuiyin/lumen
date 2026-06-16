#!/usr/bin/env python3
"""自迭代验证入口程序。

运行自迭代验证工作流，测试 Maintenance Agent 的分析能力。

用法：
    python self_test_main.py --fault_type nullptr --max_iterations 5
    python self_test_main.py --fault_type deadlock --config self_test_config.json
"""

import argparse
import logging
import uuid

from langgraph.checkpoint.memory import MemorySaver

from config import load_config
from agents.self_test.self_test_workflow import make_self_test_initial_state
from agents.self_test.workflow import build_self_test_workflow

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("langchain").setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(description="自迭代验证工作流")
    parser.add_argument(
        "--fault_type",
        default="nullptr",
        choices=["nullptr", "softlockup", "deadlock", "panic", "stack_overflow"],
        help="故障类型"
    )
    parser.add_argument(
        "--max_iterations",
        type=int,
        default=5,
        help="最大迭代次数"
    )
    parser.add_argument(
        "--config",
        default="self_test_config.json",
        help="配置文件路径"
    )
    parser.add_argument(
        "--target_score",
        type=int,
        default=90,
        help="目标评分（达到后停止迭代）"
    )
    args = parser.parse_args()

    # 加载配置
    config = load_config(args.config)

    # 从配置中获取参数
    self_test_config = config.get("self_test", {})
    max_iterations = self_test_config.get("max_iterations", args.max_iterations)
    target_score = self_test_config.get("target_score", args.target_score)

    # 构建工作流
    graph = build_self_test_workflow(checkpointer=MemorySaver())
    thread_id = str(uuid.uuid4())
    run_config = {"configurable": {"thread_id": thread_id}}

    # 创建初始状态
    initial_state = make_self_test_initial_state(
        fault_type=args.fault_type,
        max_iterations=max_iterations,
        config_path=args.config,
    )
    initial_state["config"] = config
    initial_state["target_score"] = target_score

    print(f"\n{'=' * 60}")
    print(f"自迭代验证工作流")
    print(f"故障类型: {args.fault_type}")
    print(f"最大迭代: {max_iterations}")
    print(f"目标评分: {target_score}")
    print(f"{'=' * 60}")

    # 执行工作流
    result = graph.invoke(initial_state, run_config)

    print(f"\n{'=' * 60}")
    print("最终结果:")
    print(f"{'=' * 60}")

    if result.get("fault_generation_error"):
        print(f"故障生成失败: {result['fault_generation_error']}")
    else:
        iteration_count = result.get("iteration_count", 0)
        evaluation_score = result.get("evaluation_score", 0)
        gaps_found = result.get("gaps_found", [])
        improvement_report = result.get("improvement_report", "")

        print(f"总迭代次数: {iteration_count}")
        print(f"最终评分: {evaluation_score}/{target_score}")
        print(f"\n发现的差距:")
        for gap in gaps_found:
            print(f"  - {gap}")

        if improvement_report:
            print(f"\n改进报告: {improvement_report}")

        print(f"\n{result.get('final_response', '工作流已完成')}")


if __name__ == "__main__":
    main()