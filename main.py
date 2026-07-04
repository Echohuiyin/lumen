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
    parser = argparse.ArgumentParser(description="Kernel Debuger Workflow")
    parser.add_argument(
        "input_file",
        nargs="?",
        default="input.txt",
        help="Input file describing the problem (fault type, vmcore/vmlinux/source paths, etc.)",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Workflow config file (default config.json)",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Session ID (auto-generated if omitted)",
    )
    args = parser.parse_args()

    # Parse input file into structured fields
    fields = parse_input_file(args.input_file)
    user_input = format_user_input(fields)
    if not user_input:
        print("[Error] Input file {args.input_file} is empty or malformed.")
        print("Expected fields (one per line, key: value):")
        print("  Bug Promote: <description>")
        print("  vmcore 文件: <path>")
        print("  vmlinux 文件: <path>")
        print("  boot_kernel 文件: <path>")
        print("  kernel_source 文件: <path>")
        print("See input.txt.template for a working example.")
        return

    # Load config
    config = load_config(args.config)

    # ── Init Session ──────────────────────────────────────────────────────
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

    # ── list parsed input fields for display ──────────────────────────────
    model_name = config.get("model_name", "?")
    field_lines = []
    for key, val in fields.items():
        # short label for display
        if val:
            field_lines.append(f"  {key}: {val}")
    input_summary = "\n".join(field_lines)

    print(f"\n{'=' * 60}")
    print(f"  Kernel Debuger Workflow")
    print(f"  {'─' * 56}")
    if input_summary:
        print(input_summary)
        print(f"  {'─' * 56}")
    print(f"  Model: {model_name}")
    print(f"  Input: {args.input_file}")
    print(f"  Config: {args.config}")
    print(f"  Session: {session_id}")
    print(f"{'=' * 60}\n")

    result = graph.invoke(initial_state, run_config)

    print(f"\n{'=' * 60}")
    print("  Result")
    print(f"{'=' * 60}")

    if result.get("validation_passed") is False and result.get("validation_feedback"):
        print("Input validation failed — please provide more info:")
        print(result["validation_feedback"])
    elif result.get("final_response"):
        print(result["final_response"])
    else:
        print("Workflow completed but no final response was generated.")

    print(f"\nSession files: {session_dir}/")


if __name__ == "__main__":
    main()
