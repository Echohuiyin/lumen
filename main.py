import argparse
import logging
import uuid
from datetime import datetime

from langgraph.checkpoint.memory import MemorySaver

from agents.session import create_session_dir
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
    parser.add_argument(
        "--session-id",
        default=None,
        help="Session ID（默认自动生成）",
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

    # ── 初始化 Session ───────────────────────────────────────────────────
    session_id = args.session_id or (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    )
    session_dir = create_session_dir(session_id)

    graph = build_maintenance_workflow(checkpointer=MemorySaver())
    thread_id = str(uuid.uuid4())
    run_config = {"configurable": {"thread_id": thread_id}}

    initial_state = make_initial_state(
        user_input=user_input,
        config_path=args.config,
        session_id=session_id,
        session_dir=str(session_dir),
    )

    print(f"\n{'=' * 60}")
    print(f"  维护接口人工作流")
    print(f"  输入文件: {args.input_file}")
    print(f"  配置: {args.config}")
    print(f"  Session: {session_id}")
    print(f"{'=' * 60}\n")

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

    print(f"\nSession 文件: {session_dir}/")


if __name__ == "__main__":
    main()
