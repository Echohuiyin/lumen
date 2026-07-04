import argparse
import logging
import uuid

from langgraph.checkpoint.memory import MemorySaver

from project import format_user_input, load_config, parse_input_file
from graph.rn_state import make_initial_state
from graph.rn_workflow import build_maintenance_workflow

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("langchain").setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(description="维护接口人工作流")
    parser.add_argument(
        "--input-file",
        default="input.txt",
        help="输入文件路径（默认 input.txt），包含问题描述、故障类型、vmcore/vmlinux/source 路径等",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="工作流配置文件路径（默认 config.json）",
    )
    args = parser.parse_args()

    # 解析 input.txt 获取结构化字段
    fields = parse_input_file(args.input_file)
    user_input = format_user_input(fields)
    if not user_input:
        print(f"[错误] 输入文件 {args.input_file} 为空或格式不正确。")
        print("文件应包含以下字段（每行一个 key: value）：")
        print("  问题描述: <描述>")
        print("  故障类型: <deadlock|panic|softlockup>")
        print("  vmcore 文件: <路径>")
        print("  vmlinux 文件: <路径>")
        print("  boot_kernel 文件: <路径>")
        print("  kernel_source 文件: <路径>")
        return

    # 加载配置
    config = load_config(args.config)

    graph = build_maintenance_workflow(checkpointer=MemorySaver())
    thread_id = str(uuid.uuid4())
    run_config = {"configurable": {"thread_id": thread_id}}

    initial_state = make_initial_state(
        user_input=user_input,
        config_path=args.config,
    )

    print(f"\n{'=' * 60}")
    print(f"维护接口人工作流")
    print(f"输入文件: {args.input_file}")
    print(f"配置: {args.config}")
    print(f"{'=' * 60}")
    print(f"\n--- 用户输入 ---")
    print(user_input)
    print(f"---\n")

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
