import argparse
import logging
import uuid

from langgraph.checkpoint.memory import MemorySaver

from config import load_config
from graph.rn_state import make_initial_state
from graph.rn_workflow import build_maintenance_workflow

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("langchain").setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(description="维护接口人工作流")
    parser.add_argument("--input", required=True, help="用户输入的问题描述")
    parser.add_argument("--config", default="config.json", help="工作流配置文件路径")
    args = parser.parse_args()

    # 加载配置
    config = load_config(args.config)

    graph = build_maintenance_workflow(checkpointer=MemorySaver())
    thread_id = str(uuid.uuid4())
    run_config = {"configurable": {"thread_id": thread_id}}

    initial_state = make_initial_state(
        user_input=args.input,
        config_path=args.config,
    )

    print(f"\n{'=' * 60}")
    print(f"维护接口人工作流")
    print(f"用户输入: {args.input}")
    print(f"配置: {args.config}")
    print(f"{'=' * 60}")

    result = graph.invoke(initial_state, run_config)

    print(f"\n{'=' * 60}")
    print("最终结果:")
    print(f"{'=' * 60}")

    if result.get("validation_passed") is False and result.get("validation_feedback"):
        print("输入信息不完整，请补充以下信息：")
        print(result["validation_feedback"])
    elif result.get("final_response"):
        print(result["final_response"])
    else:
        print("工作流已完成，但未生成最终回复。")


if __name__ == "__main__":
    main()
