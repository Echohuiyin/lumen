"""自迭代验证工作流。

构建完整的自迭代验证流程：
fault_generator → maintenance_workflow_wrapper → evaluation → improvement → knowledge_update → (循环)

工作流闭环：
1. 故障生成：注入已知故障，生成 vmcore 和 boot.log
2. 分析执行：现有 Maintenance Workflow 分析生成的数据
3. 评估对比：对比分析结果与已知故障特征
4. 改进建议：根据差距生成改进方案并自动应用
5. 知识库更新：将验证结果写入知识库
6. 迭代判断：根据评分和迭代次数决定是否继续
"""

from langgraph.graph import END, START, StateGraph

from agents.self_test.fault_generator import fault_generator_node
from agents.self_test.evaluation import evaluation_agent_node
from agents.self_test.improvement import improvement_agent_node
from agents.self_test.knowledge_integration import auto_knowledge_pipeline
from agents.self_test.self_test_workflow import SelfTestIterationState
from graph.rn_workflow import build_maintenance_workflow


def build_self_test_workflow(*, checkpointer=None):
    """构建自迭代验证工作流图。

    START → fault_generator → build_user_input → maintenance_wrapper
           → evaluation → improvement → knowledge_update → iteration_decision → (循环或结束)

    流程说明：
    1. fault_generator: 生成故障测试数据
    2. build_user_input: 将故障数据转换为用户输入格式
    3. maintenance_wrapper: 执行现有的维护分析流程
    4. evaluation: 评估分析结果与预期故障的差距
    5. improvement: 根据差距生成改进建议并自动应用
    6. knowledge_update: 将验证结果写入知识库
    7. iteration_decision: 决定是否继续迭代
    """
    builder = StateGraph(SelfTestIterationState)

    # 添加节点
    builder.add_node("fault_generator", fault_generator_node)
    builder.add_node("build_user_input", build_user_input_node)
    builder.add_node("maintenance_wrapper", maintenance_wrapper_node)
    builder.add_node("evaluation", evaluation_agent_node)
    builder.add_node("improvement", improvement_agent_node)
    builder.add_node("knowledge_update", knowledge_update_node)
    builder.add_node("iteration_decision", iteration_decision_node)

    # 添加边
    builder.add_edge(START, "fault_generator")
    builder.add_edge("fault_generator", "build_user_input")
    builder.add_edge("build_user_input", "maintenance_wrapper")
    builder.add_edge("maintenance_wrapper", "evaluation")
    builder.add_edge("evaluation", "improvement")
    builder.add_edge("improvement", "knowledge_update")
    builder.add_edge("knowledge_update", "iteration_decision")

    # 条件边：决定是否继续迭代
    builder.add_conditional_edges(
        "iteration_decision",
        route_after_decision,
        {
            "continue": "fault_generator",  # 继续迭代
            "stop": END,                    # 结束
        }
    )

    if checkpointer is not None:
        return builder.compile(checkpointer=checkpointer)
    return builder.compile()


def knowledge_update_node(state: SelfTestIterationState) -> dict:
    """知识库更新节点。

    将验证结果自动写入知识库，并导入到 Chroma 向量数据库。
    """
    print(f"\n[知识库更新] 开始处理迭代 {state.get('iteration_count', 1)} 的结果...")

    result = auto_knowledge_pipeline(state)

    return {
        "knowledge_file": result.get("doc_path", ""),
        "knowledge_title": result.get("doc_title", ""),
        "knowledge_import_success": result.get("success", False),
    }


def build_user_input_node(state: SelfTestIterationState) -> dict:
    """将生成的故障数据转换为用户输入格式。

    构建 Maintenance Workflow 可接受的输入格式，
    包含 vmcore 路径、vmlinux 路径、boot.log 内容、问题描述等。
    """
    vmcore_path = state.get("generated_vmcore", "")
    boot_log_path = state.get("generated_boot_log", "")
    fault_description = state.get("fault_description", "")
    iteration_count = state.get("iteration_count", 0)
    config = state.get("config", {})

    # 增加迭代计数
    iteration_count += 1

    # 检查故障生成是否成功
    if state.get("fault_generation_error"):
        user_input = f"故障生成失败: {state['fault_generation_error']}"
    else:
        # 从配置获取vmlinux路径
        kernel_config = config.get("kernel", {})
        vmlinux_path = kernel_config.get("vmlinux_path", "/path/to/vmlinux")

        # 读取 boot.log 内容
        boot_log_content = ""
        if boot_log_path:
            try:
                from pathlib import Path
                boot_log_content = Path(boot_log_path).read_text(encoding="utf-8")
                # 限制长度，避免过长
                if len(boot_log_content) > 8000:
                    boot_log_content = boot_log_content[:8000] + "\n... (截断)"
            except Exception as e:
                boot_log_content = f"无法读取 boot.log: {boot_log_path}\n错误: {e}"

        user_input = f"""内核崩溃问题分析请求

故障描述: {fault_description}
迭代次数: {iteration_count}

## 内核调试符号文件
vmlinux 文件: {vmlinux_path}
内核版本: 6.6.0-OLK

## Vmcore 文件
路径: {vmcore_path}

## Boot.log 关键内容
```
{boot_log_content}
```

请分析此内核崩溃问题，给出根因定位和复现用例。
"""

    return {
        "user_input": user_input,
        "iteration_count": iteration_count,
        # 清空之前的分析结果，准备新一轮分析
        "expert_results": [],
        "kernel_analysis": "",
        "reproduce_case": "",
        "kernel_diagnosis": "",
        "test_result": "",
        "test_passed": False,
        "test_attempts": 0,
        "validation_passed": False,
        "validation_feedback": "",
    }


def maintenance_wrapper_node(state: SelfTestIterationState) -> dict:
    """Maintenance Workflow 包装节点。

    执行现有的 Maintenance Workflow，分析生成的故障数据。
    """
    # 检查故障生成是否成功
    if state.get("fault_generation_error"):
        print(f"\n[迭代 {state.get('iteration_count', 1)}] 故障生成失败，跳过分析")
        return {
            "kernel_analysis": "故障生成失败，无法分析",
            "reproduce_case": "",
            "kernel_diagnosis": "",
        }

    # 调用现有的 Maintenance Workflow
    maintenance_graph = build_maintenance_workflow()

    # 构建 Maintenance Workflow 的输入状态
    from graph.rn_state import make_initial_state

    maintenance_input = make_initial_state(
        user_input=state["user_input"],
        config_path=state["config_path"],
    )
    maintenance_input["config"] = state.get("config", {})

    print(f"\n[迭代 {state.get('iteration_count', 1)}] 执行 Maintenance Workflow...")

    # 执行 Maintenance Workflow
    result = maintenance_graph.invoke(maintenance_input)

    print(f"[迭代 {state.get('iteration_count', 1)}] Maintenance Workflow 完成")

    # 提取关键结果
    return {
        "validation_passed": result.get("validation_passed", False),
        "validation_feedback": result.get("validation_feedback", ""),
        "expert_results": result.get("expert_results", []),
        "kernel_analysis": result.get("kernel_analysis", ""),
        "reproduce_case": result.get("reproduce_case", ""),
        "kernel_diagnosis": result.get("kernel_diagnosis", ""),
        "test_result": result.get("test_result", ""),
        "test_passed": result.get("test_passed", False),
        "test_attempts": result.get("test_attempts", 0),
    }


def iteration_decision_node(state: SelfTestIterationState) -> dict:
    """迭代决策节点：准备下一次迭代或结束。

    根据评估分数和改进建议，决定是否继续迭代。
    """
    should_continue = state.get("should_continue", False)
    iteration_count = state.get("iteration_count", 0)
    evaluation_score = state.get("evaluation_score", 0)
    target_score = state.get("target_score", 90)
    max_iterations = state.get("max_iterations", 5)

    # 决策逻辑
    if iteration_count >= max_iterations:
        reason = f"已达最大迭代次数 {max_iterations}"
        decision = "stop"
    elif evaluation_score >= target_score:
        reason = f"评分 {evaluation_score} 已达目标 {target_score}"
        decision = "stop"
    elif not should_continue:
        reason = "改进建议不建议继续迭代"
        decision = "stop"
    else:
        reason = f"评分 {evaluation_score} < {target_score}，继续改进"
        decision = "continue"

    if decision == "continue":
        print(f"\n[迭代 {iteration_count}] 评分 {evaluation_score}/100，继续迭代...")
    else:
        print(f"\n[迭代 {iteration_count}] 评分 {evaluation_score}/100，停止迭代。原因: {reason}")

    return {
        "decision": decision,
        "final_response": f"自迭代验证完成。共 {iteration_count} 次迭代，最终评分 {evaluation_score}/100。{reason}",
    }


def route_after_decision(state: SelfTestIterationState) -> str:
    """根据决策节点的输出路由到下一步。"""
    decision = state.get("decision", "stop")
    return decision


# 导出可用的 workflow
self_test_graph = build_self_test_workflow()