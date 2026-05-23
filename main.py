import logging
import subprocess
import sys
import uuid

try:
    import readline  # noqa: F401 - improves line editing on Linux/WSL
except ImportError:
    pass

from dotenv import load_dotenv
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from graph.workflow import build_workflow

load_dotenv()

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("langchain").setLevel(logging.WARNING)


def reset_terminal() -> None:
    """Reset terminal state after subprocess or library output."""
    if not sys.stdin.isatty():
        return
    try:
        subprocess.run(["stty", "sane"], check=False, capture_output=True)
    except OSError:
        pass


def prompt_user(title: str, prompt: str = "> ") -> str:
    """Print prompt and read a line with stable terminal behavior."""
    reset_terminal()
    print(title, flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    return input(prompt).strip()


def run_workflow(user_request: str) -> str:
    graph = build_workflow(checkpointer=MemorySaver())
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    initial_state = {
        "messages": [],
        "user_request": user_request,
        "task_plan": "",
        "task_items": [],
        "current_task": "",
        "task_index": 0,
        "task_batch_offset": 0,
        "execution_results": [],
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

    print(f"\n{'=' * 60}")
    print(f"用户指令: {user_request}")
    print(f"{'=' * 60}")

    result = graph.invoke(initial_state, config)

    while True:
        snapshot = graph.get_state(config)
        if not snapshot.next:
            break

        if snapshot.tasks and snapshot.tasks[0].interrupts:
            interrupt_value = snapshot.tasks[0].interrupts[0].value
            question = (
                interrupt_value.get("question", str(interrupt_value))
                if isinstance(interrupt_value, dict)
                else str(interrupt_value)
            )
            user_reply = prompt_user(
                f"\n{'-' * 40}\n[任务规划] 需要您的输入:\n{question}"
            )
            if not user_reply:
                user_reply = "(用户未提供输入)"
            result = graph.invoke(
                Command(resume={"user_reply": user_reply}),
                config,
            )
        else:
            result = graph.invoke(None, config)

    final = result.get("final_response", "")
    if not final:
        final = result.get("execution_result", "工作流已完成，但未生成最终回复。")

    print(f"\n{'=' * 60}")
    print("最终回复:")
    print(f"{'=' * 60}")
    print(final)
    return final


def main():
    if len(sys.argv) > 1:
        user_request = " ".join(sys.argv[1:])
    else:
        user_request = prompt_user("请输入项目例行指令:")
        if not user_request:
            print("错误: 未提供指令")
            sys.exit(1)

    run_workflow(user_request)


if __name__ == "__main__":
    main()
