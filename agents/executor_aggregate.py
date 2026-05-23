from config import MAX_PARALLEL_TASKS
from graph.state import WorkflowState


def batch_advance_node(state: WorkflowState) -> dict:
    offset = state.get("task_batch_offset", 0)
    return {"task_batch_offset": offset + MAX_PARALLEL_TASKS}


def executor_aggregate_node(state: WorkflowState) -> dict:
    results = sorted(
        state.get("execution_results") or [],
        key=lambda item: item.get("index", 0),
    )

    sections: list[str] = []
    for item in results:
        task = item.get("task", "")
        status = item.get("status", "success")
        content = item.get("content", "")
        sections.append(f"### 子任务 {item.get('index', 0) + 1}: {task}\n状态: {status}\n{content}")

    execution_result = "\n\n".join(sections) if sections else ""
    overall_status = "failed" if any(r.get("status") == "failed" for r in results) else "success"

    return {
        "execution_result": execution_result,
        "executor_status": overall_status,
    }
