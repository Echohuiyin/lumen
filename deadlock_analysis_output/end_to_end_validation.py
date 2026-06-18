#!/usr/bin/env python3
"""端到端验证脚本 - Mutex ABBA 死锁分析全流程。

运行 Maintenance Workflow，记录每个 Agent 的中间输出：
Validator → PM → Tool Experts → Kernel Expert → Test Expert → Knowledge Base

输出保存到 deadlock_analysis_output/end_to_end_outputs/

Usage:
    python deadlock_analysis_output/end_to_end_validation.py --vmcore /path/to/vmcore --vmlinux /path/to/vmlinux
"""

import argparse
import json
import logging
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver

# 设置路径 - 使用脚本所在目录推导项目根目录
script_dir = Path(__file__).resolve().parent
project_root = script_dir.parent
sys.path.insert(0, str(project_root))

from config import load_config
from graph.rn_state import make_initial_state
from graph.rn_workflow import build_maintenance_workflow

# 静默日志
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("langchain").setLevel(logging.WARNING)

# 输出目录 - 相对于脚本位置
OUTPUT_DIR = script_dir / "end_to_end_outputs"
OUTPUT_DIR.mkdir(exist_ok=True)


def write_stage_output(stage_name: str, content: str, metadata: dict = None):
    """写入单个阶段的输出文件."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{stage_name}_{timestamp}.txt"
    filepath = OUTPUT_DIR / filename

    header = f"""
{'=' * 70}
Agent: {stage_name}
Timestamp: {timestamp}
{'=' * 70}

"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(header)
        f.write(content)
        if metadata:
            f.write("\n\n--- Metadata ---\n")
            f.write(json.dumps(metadata, indent=2, ensure_ascii=False))

    print(f"[记录] {stage_name} 输出已保存: {filepath}")
    return filepath


def write_workflow_summary(final_state: dict, all_outputs: dict):
    """写入工作流总结报告."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = OUTPUT_DIR / f"workflow_summary_{timestamp}.md"

    content = f"""# Mutex ABBA 死锁分析 - 端到端验证报告

**时间**: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
**故障类型**: Mutex ABBA 死锁
**vmcore**: {final_state.get('user_input', '').split('vmcore')[1][:80] if 'vmcore' in final_state.get('user_input', '') else 'N/A'}

---

## 工作流执行路径

```
START → Validator → PM → [Tool Experts] → Kernel Expert → Knowledge Base → END
```

---

## 各 Agent 输出汇总

### 1. Validator (输入校验)
- **结果**: {'通过' if final_state.get('validation_passed') else '未通过'}
- **反馈**: {final_state.get('validation_feedback', 'N/A')[:200]}...
- **输出文件**: {all_outputs.get('validator', 'N/A')}

### 2. PM (问题分类)
- **创建 Issue**: {final_state.get('issue_id', 'N/A')}
- **所需专家**: {', '.join(final_state.get('required_experts', []))}
- **输出文件**: {all_outputs.get('pm', 'N/A')}

### 3. Tool Experts (并行分析)
"""

    for result in final_state.get('expert_results', []):
        content += f"""
#### {result['expert_name']} ({result['expert_type']})
- **分析摘要**: {result['analysis_output'][:150]}...
- **输出文件**: {all_outputs.get(f'tool_expert_{result["expert_type"]}', 'N/A')}
"""

    content += f"""

### 4. Kernel Expert (内核分析)
- **复现用例**: 提取自分析输出
- **维测方案**: 提取自分析输出
- **输出文件**: {all_outputs.get('kernel_expert', 'N/A')}

### 5. Knowledge Base (知识归档)
- **知识文件**: {final_state.get('knowledge_file', 'N/A')}
- **输出文件**: {all_outputs.get('knowledge_base', 'N/A')}

---

## 最终分析结论

{final_state.get('final_response', '工作流已完成')}

---

## 工作流状态摘要

```json
{json.dumps({
    'validation_passed': final_state.get('validation_passed'),
    'required_experts': final_state.get('required_experts'),
    'expert_results_count': len(final_state.get('expert_results', [])),
    'kernel_analysis_length': len(final_state.get('kernel_analysis', '')),
    'knowledge_file': final_state.get('knowledge_file'),
}, indent=2, ensure_ascii=False)}
```
"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"\n[总结] 工作流报告已保存: {filepath}")
    return filepath


def run_end_to_end_analysis(vmcore_path: str = None, vmlinux_path: str = None):
    """运行端到端分析流程.

    Args:
        vmcore_path: vmcore 文件路径（默认使用脚本目录下的 vmcore.elf）
        vmlinux_path: vmlinux 文件路径（默认从配置或环境变量获取）
    """

    print("\n" + "=" * 70)
    print("端到端验证 - Mutex ABBA 死锁分析")
    print("=" * 70)

    # 默认路径
    if not vmcore_path:
        vmcore_path = str(script_dir / "vmcore.elf")

    if not vmlinux_path:
        # 从配置或环境变量获取 vmlinux
        config_data = load_config("self_test_config.json")
        vmlinux_path = config_data.get("self_test", {}).get("vmlinux_path", "")
        if not vmlinux_path:
            vmlinux_path = os.environ.get("TEST_VMLINUX", "")

    # 加载配置
    config = load_config("self_test_config.json")

    # 构建用户输入 - 提供完整信息绕过校验
    user_input = f"""
vmcore 文件: {vmcore_path}
vmlinux 文件: {vmlinux_path}

问题描述: 内核发生 Mutex ABBA 死锁导致 hung_task panic。
两个线程 deadlock_thread1 和 deadlock_thread2 以相反顺序获取两个 mutex（mutex_alpha 和 mutex_beta），形成经典ABBA死锁。

环境信息: openEuler 23.03, kernel 6.6.0-OLK, ARM64, 64核
涉及进程: deadlock_thread1 (线程A) 和 deadlock_thread2 (线程B)
故障类型: deadlock
触发场景: 加载故障注入模块后立即触发，hung_task 120秒后 panic

关键日志片段:
[    5.234567] === Mutex ABBA Deadlock Module Loaded ===
[    5.234568] Thread1: alpha -> beta order
[    5.234569] Thread2: beta -> alpha order (OPPOSITE!)
[    5.340123] Thread1: Locked mutex_alpha
[    5.340456] Thread2: Locked mutex_beta
[    5.450789] Thread1: Attempting to lock mutex_beta (will block)
[    5.450890] Thread2: Attempting to lock mutex_alpha (will block)
[  120.000000] BUG: soft lockup - CPU#0 stuck for 22s! [deadlock_thread1:123]
[  120.000000] BUG: soft lockup - CPU#1 stuck for 22s! [deadlock_thread2:124]
[  120.500000] Kernel panic - not syncing: hung_task blocked for 120s

请分析此 vmcore，定位死锁的根因，识别涉及的线程和 mutex，给出详细的死锁模型和维测方案。
"""

    print(f"vmcore: {vmcore_path}")
    print(f"vmlinux: {vmlinux_path}")

    # 创建初始状态
    initial_state = make_initial_state(
        user_input=user_input,
        config_path="self_test_config.json",
    )
    initial_state["config"] = config

    # 构建工作流
    graph = build_maintenance_workflow(checkpointer=MemorySaver())
    thread_id = str(uuid.uuid4())
    run_config = {"configurable": {"thread_id": thread_id}}

    # 记录各阶段输出
    stage_outputs = {}

    print("\n开始执行 Maintenance Workflow...")
    print("流程: Validator → PM → Tool Experts → Kernel Expert → Knowledge Base\n")

    # 执行工作流
    result = graph.invoke(initial_state, run_config)

    # 记录 Validator 输出
    if "validation_feedback" in result:
        stage_outputs["validator"] = write_stage_output(
            "Validator",
            result.get("validation_feedback", ""),
            {"passed": result.get("validation_passed", False)}
        )

    # 记录 PM 输出
    if result.get("issue_id") or result.get("required_experts"):
        pm_content = f"""
Issue ID: {result.get('issue_id', 'N/A')}
Issue URL: {result.get('issue_url', 'N/A')}
Required Experts: {', '.join(result.get('required_experts', []))}
"""
        stage_outputs["pm"] = write_stage_output(
            "PM",
            pm_content,
            {"issue_id": result.get("issue_id"), "required_experts": result.get("required_experts")}
        )

    # 记录 Tool Experts 输出
    for expert_result in result.get("expert_results", []):
        expert_type = expert_result.get("expert_type")
        stage_outputs[f"tool_expert_{expert_type}"] = write_stage_output(
            f"ToolExpert_{expert_type}",
            expert_result.get("analysis_output", ""),
            {"expert_name": expert_result.get("expert_name")}
        )

    # 记录 Kernel Expert 输出
    if result.get("kernel_analysis"):
        stage_outputs["kernel_expert"] = write_stage_output(
            "KernelExpert",
            result.get("kernel_analysis", ""),
            {
                "reproduce_case_length": len(result.get("reproduce_case", "")),
                "kernel_diagnosis_length": len(result.get("kernel_diagnosis", "")),
            }
        )

    # 记录 Knowledge Base 输出
    if result.get("knowledge_file"):
        kb_path = result.get("knowledge_file")
        if os.path.exists(kb_path):
            kb_content = Path(kb_path).read_text(encoding="utf-8")
            stage_outputs["knowledge_base"] = write_stage_output(
                "KnowledgeBase",
                kb_content,
                {"knowledge_file": kb_path}
            )

    # 写入总结报告
    summary_path = write_workflow_summary(result, stage_outputs)

    # 打印最终结果
    print("\n" + "=" * 70)
    print("分析完成!")
    print("=" * 70)
    print(f"Validator 校验: {'通过' if result.get('validation_passed') else '未通过'}")
    print(f"调用专家数量: {len(result.get('required_experts', []))}")
    print(f"专家分析结果: {len(result.get('expert_results', []))} 个")
    print(f"内核分析长度: {len(result.get('kernel_analysis', ''))} 字符")
    print(f"知识库文件: {result.get('knowledge_file', 'N/A')}")
    print(f"\n总结报告: {summary_path}")
    print(f"所有输出目录: {OUTPUT_DIR}")

    return result, stage_outputs


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="端到端验证 - Mutex ABBA 死锁分析")
    parser.add_argument("--vmcore", help="vmcore 文件路径")
    parser.add_argument("--vmlinux", help="vmlinux 文件路径")
    args = parser.parse_args()

    run_end_to_end_analysis(
        vmcore_path=args.vmcore,
        vmlinux_path=args.vmlinux,
    )