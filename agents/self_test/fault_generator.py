"""故障生成 Agent：调用 kernel-fault-injection skill 生成测试数据。

生成已知内核故障（NULL指针、死锁、软锁定等），产生 vmcore 和 boot.log，
作为 Maintenance Workflow 的输入数据进行自迭代验证。

支持两种模式：
1. 实际执行模式：调用 kernel-fault-injection skill，在 QEMU 中真实触发故障
2. 模拟模式：生成模拟数据用于快速测试 workflow 逻辑
"""

import os
import subprocess
from pathlib import Path
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage

from agents.llm_display import call_llm_with_display
from config import get_llm_with_config, load_prompt_from_file
from paths import PROJECT_ROOT, get_skill_path_candidates


# 故障类型及其预期特征
FAULT_TYPES = {
    "nullptr": {
        "description": "NULL pointer dereference",
        "expected_panic": "kernel BUG at",
        "expected_root_cause": "NULL pointer dereference at address 0",
        "expected_call_trace": "crash_init",
        "difficulty": "easy",
        "panic_keywords": ["kernel BUG at", "NULL pointer dereference", "Oops"],
    },
    "softlockup": {
        "description": "CPU soft lockup in infinite loop",
        "expected_panic": "BUG: soft lockup",
        "expected_root_cause": "CPU stuck in infinite loop with interrupts disabled",
        "expected_call_trace": "softlockup_init",
        "difficulty": "medium",
        "panic_keywords": ["BUG: soft lockup", "CPU# stuck"],
    },
    "deadlock": {
        "description": "Mutex ABBA deadlock",
        "expected_panic": "blocked for more than 120 seconds",
        "expected_root_cause": "ABBA deadlock: two threads holding mutexes in opposite order",
        "expected_call_trace": "deadlock_init",
        "difficulty": "hard",
        "panic_keywords": ["blocked for more than", "hung task", "mutex"],
    },
    "panic": {
        "description": "Direct kernel panic",
        "expected_panic": "Kernel panic",
        "expected_root_cause": "Direct panic call via panic() function",
        "expected_call_trace": "panic_init",
        "difficulty": "easy",
        "panic_keywords": ["Kernel panic", "panic_init"],
    },
    "stack_overflow": {
        "description": "Stack overflow via recursion",
        "expected_panic": "stack-overflow",
        "expected_root_cause": "Stack overflow from deep recursion",
        "expected_call_trace": "recursive_func",
        "difficulty": "medium",
        "panic_keywords": ["stack-overflow", "corrupted stack"],
    },
}


def fault_generator_node(state: dict) -> dict:
    """故障生成节点：生成指定类型的内核故障。

    输入：
    - fault_type: 故障类型 (nullptr/softlockup/deadlock/panic/stack_overflow)
    - iteration_count: 当前迭代次数
    - execution_mode: "real" 或 "mock"

    输出：
    - generated_vmcore: vmcore 文件路径
    - generated_boot_log: boot.log 文件路径
    - expected_fault: 预期故障特征（用于评估）
    """
    fault_type = state.get("fault_type", "nullptr")
    iteration_count = state.get("iteration_count", 1)
    execution_mode = state.get("execution_mode", "mock")  # 默认 mock 模式便于测试

    # 验证故障类型
    if fault_type not in FAULT_TYPES:
        return {
            "fault_generation_error": f"Unknown fault type: {fault_type}",
        }

    fault_info = FAULT_TYPES[fault_type]

    # 输出目录
    output_dir = PROJECT_ROOT / "test_outputs" / f"{fault_type}_iter{iteration_count}"

    if execution_mode == "real":
        # 实际执行模式：调用 kernel-fault-injection skill
        result = _run_fault_injection_real(fault_type, output_dir)
    else:
        # 模拟模式：生成模拟数据
        result = _generate_mock_fault_data(fault_type, output_dir, fault_info)

    if result.get("success"):
        return {
            "generated_vmcore": result["vmcore_path"],
            "generated_boot_log": result["boot_log_path"],
            "generated_vmlinux": result.get("vmlinux_path", ""),
            "expected_fault": fault_info,
            "fault_description": fault_info["description"],
            "execution_mode": execution_mode,
        }
    else:
        return {
            "fault_generation_error": result.get("error", "Unknown error"),
        }


def _run_fault_injection_real(fault_type: str, output_dir: Path) -> dict:
    """实际执行故障注入。

    调用 kernel-fault-injection skill 在 QEMU 中真实触发故障。
    """
    # 查找 skill 路径（优先使用 Analysis-SKILL 子模块）
    skill_paths = get_skill_path_candidates("kernel-fault-injection")

    skill_path = None
    for path in skill_paths:
        if path.exists():
            skill_path = path
            break

    if not skill_path:
        return {
            "success": False,
            "error": "kernel-fault-injection skill not found in any known location",
        }

    output_dir.mkdir(parents=True, exist_ok=True)

    script_path = skill_path / "scripts" / "run_fault_injection.sh"

    if not script_path.exists():
        return {
            "success": False,
            "error": f"run_fault_injection.sh not found at {script_path}",
        }

    # 执行故障注入
    print(f"\n[故障生成] 执行实际故障注入: {fault_type}")
    print(f"  Skill: {skill_path}")
    print(f"  Output: {output_dir}")

    try:
        result = subprocess.run(
            ["bash", str(script_path), fault_type, "--output", str(output_dir)],
            capture_output=True,
            text=True,
            timeout=300,  # 3 分钟超时
            cwd=str(skill_path),
        )

        if result.returncode != 0:
            return {
                "success": False,
                "error": f"Script failed (exit {result.returncode}): {result.stderr[:500]}",
                "stdout": result.stdout[:1000],
            }

        # 检查输出文件
        vmcore_path = output_dir / "vmcore.elf"
        boot_log_path = output_dir / "boot.log"
        vmlinux_path = output_dir / "vmlinux"  # 脚本会复制 vmlinux 到输出目录

        # 检查可能的其他输出位置（Analysis-SKILL/test_outputs/）
        if not vmcore_path.exists():
            # 检查 skill 默认输出位置
            default_output = SKILLS_PATH.parent / "test_outputs" / f"{fault_type}_x86_64"
            alt_vmcore = default_output / "vmcore.elf"
            alt_boot_log = default_output / "boot.log"
            alt_vmlinux = default_output / "vmlinux"

            if alt_vmcore.exists():
                vmcore_path = alt_vmcore
                boot_log_path = alt_boot_log
            if alt_vmlinux.exists():
                vmlinux_path = alt_vmlinux

        if vmcore_path.exists() or boot_log_path.exists():
            return {
                "success": True,
                "vmcore_path": str(vmcore_path) if vmcore_path.exists() else "",
                "boot_log_path": str(boot_log_path) if boot_log_path.exists() else "",
                "vmlinux_path": str(vmlinux_path) if vmlinux_path.exists() else "",
                "stdout": result.stdout[:500],
            }
        else:
            return {
                "success": False,
                "error": f"Output files not generated. stdout: {result.stdout[:500]}",
            }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "Fault injection timed out (180s)",
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


def _generate_mock_fault_data(fault_type: str, output_dir: Path, fault_info: dict) -> dict:
    """生成模拟故障数据。

    创建模拟的 vmcore 和 boot.log 文件，用于快速测试 workflow 逻辑。
    模拟数据包含真实的故障特征，便于评估 agent 进行分析。
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[故障生成] 生成模拟故障数据: {fault_type}")
    print(f"  Output: {output_dir}")

    # 模拟 vmcore 文件（空文件，仅标记存在）
    vmcore_path = output_dir / "vmcore.elf"
    vmcore_path.touch()

    # 生成模拟 boot.log
    boot_log_path = output_dir / "boot.log"
    boot_log_content = _generate_mock_boot_log(fault_type, fault_info)
    boot_log_path.write_text(boot_log_content, encoding="utf-8")

    # 生成分析摘要
    analysis_path = output_dir / "analysis.txt"
    analysis_content = _generate_mock_analysis(fault_type, fault_info)
    analysis_path.write_text(analysis_content, encoding="utf-8")

    return {
        "success": True,
        "vmcore_path": str(vmcore_path),
        "boot_log_path": str(boot_log_path),
        "is_mock": True,
    }


def _generate_mock_boot_log(fault_type: str, fault_info: dict) -> str:
    """生成模拟的 boot.log 内容。

    包含真实的 panic 日志模式，便于分析。
    """
    timestamp = "2024-01-15 10:23:45"
    panic_keywords = fault_info.get("panic_keywords", [])

    base_log = f"""[{timestamp}] Linux version 6.6.0-OLK (root@build-host) (gcc version 10.3.0)
[{timestamp}] Command line: console=ttyS0 panic=10 oops=panic
[{timestamp}] Kernel command line: console=ttyS0 panic=10 oops=panic
[{timestamp}] PID hash table entries: 4096 (order: 3)
[{timestamp}] Memory: 512M available
[{timestamp}] CPU: Intel(R) Core(TM) i7-10700K CPU @ 3.80GHz
[{timestamp}] SMP: 2 CPUs activated
[{timestamp}] Loading crash_{fault_type}.ko module...
[{timestamp}] Module crash_{fault_type} loaded successfully
"""

    # 根据故障类型添加 panic 日志
    if fault_type == "nullptr":
        panic_log = """
[2024-01-15 10:23:50] BUG: kernel NULL pointer dereference at 0000000000000000
[2024-01-15 10:23:50] Oops: 0002 [#1] SMP PTI
[2024-01-15 10:23:50] CPU: 0 PID: 1 Comm: swapper/0 Not tainted 6.6.0-OLK
[2024-01-15 10:23:50] Hardware name: QEMU Standard PC (Q35 + ICH9)
[2024-01-15 10:23:50] RIP: 0010:crash_init+0x5/0x10 [crash_nullptr]
[2024-01-15 10:23:50] Code: Unable to access opcode bytes at RIP 0x5
[2024-01-15 10:23:50] Call Trace:
[2024-01-15 10:23:50]  <TASK>
[2024-01-15 10:23:50]  do_one_initcall+0x42/0x210
[2024-01-15 10:23:50]  kernel_init_freeable+0x150/0x290
[2024-01-15 10:23:50]  kernel_init+0x18/0x130
[2024-01-15 10:23:50]  ret_from_fork+0x2c/0x50
[2024-01-15 10:23:50]  </TASK>
[2024-01-15 10:23:50] Kernel panic - not syncing: Attempted to kill init!
[2024-01-15 10:23:50] Kernel Offset: 0x0 from 0xffffffff80000000
"""
    elif fault_type == "softlockup":
        panic_log = """
[2024-01-15 10:23:55] BUG: soft lockup - CPU#0 stuck for 26s!
[2024-01-15 10:23:55] CPU: 0 PID: 1 Comm: swapper/0 Not tainted 6.6.0-OLK
[2024-01-15 10:23:55] Hardware name: QEMU Standard PC (Q35 + ICH9)
[2024-01-15 10:23:55] RIP: 0010:softlockup_init+0x0/0x10 [crash_softlockup]
[2024-01-15 10:23:55] Call Trace:
[2024-01-15 10:23:55]  <TASK>
[2024-01-15 10:23:55]  do_one_initcall
[2024-01-15 10:23:55]  kernel_init_freeable
[2024-01-15 10:23:55]  </TASK>
[2024-01-15 10:23:56] Kernel panic - not syncing: softlockup_panic
"""
    elif fault_type == "deadlock":
        panic_log = """
[2024-01-15 10:24:05] INFO: task swapper/0:1 blocked for more than 120 seconds.
[2024-01-15 10:24:05]       Not tainted 6.6.0-OLK
[2024-01-15 10:24:05] "echo 0 > /proc/sys/kernel/hung_task_timeout_secs" disables this message.
[2024-01-15 10:24:05] task:swapper/0     state:D stack:0     pid:1     ppid:0
[2024-01-15 10:24:05] Call Trace:
[2024-01-15 10:24:05]  __schedule+0x2a5/0x6e0
[2024-01-15 10:24:05]  schedule+0x3b/0xa0
[2024-01-15 10:24:05]  schedule_preempt_disabled+0xe/0x10
[2024-01-15 10:24:05]  __mutex_lock_slowpath+0x13e/0x380
[2024-01-15 10:24:05]  mutex_lock+0x2b/0x40
[2024-01-15 10:24:05]  deadlock_init+0x15/0x20 [crash_deadlock]
[2024-01-15 10:24:05]
[2024-01-15 10:24:05] INFO: task swapper/1:2 blocked for more than 120 seconds.
[2024-01-15 10:24:05] Call Trace:
[2024-01-15 10:24:05]  mutex_lock+0x2b/0x40
[2024-01-15 10:24:05]  deadlock_thread2+0x15/0x20 [crash_deadlock]
"""
    elif fault_type == "panic":
        panic_log = """
[2024-01-15 10:24:10] Kernel panic - not syncing: Test panic from crash_panic module
[2024-01-15 10:24:10] CPU: 0 PID: 1 Comm: swapper/0 Not tainted 6.6.0-OLK
[2024-01-15 10:24:10] Hardware name: QEMU Standard PC (Q35 + ICH9)
[2024-01-15 10:24:10] Call Trace:
[2024-01-15 10:24:10]  <TASK>
[2024-01-15 10:24:10]  panic+0x40/0x100
[2024-01-15 10:24:10]  panic_init+0x5/0x10 [crash_panic]
[2024-01-15 10:24:10]  do_one_initcall+0x42/0x210
[2024-01-15 10:24:10]  </TASK>
"""
    elif fault_type == "stack_overflow":
        panic_log = """
[2024-01-15 10:24:15] stack-overflow detected on CPU 0
[2024-01-15 10:24:15] CPU: 0 PID: 1 Comm: swapper/0 Not tainted 6.6.0-OLK
[2024-01-15 10:24:15] Hardware name: QEMU Standard PC (Q35 + ICH9)
[2024-01-15 10:24:15] Stack pointer: 0xffffc90000000000 (corrupted)
[2024-01-15 10:24:15] Call Trace:
[2024-01-15 10:24:15]  recursive_func+0x5/0x10 [crash_stack_overflow]
[2024-01-15 10:24:15]  recursive_func+0x5/0x10 [crash_stack_overflow]
[2024-01-15 10:24:15]  recursive_func+0x5/0x10 [crash_stack_overflow]
[2024-01-15 10:24:15]  ... (repeated 1000+ times)
[2024-01-15 10:24:15] Kernel panic - not syncing: stack-overflow
"""
    else:
        panic_log = f"""
[2024-01-15 10:24:20] Kernel panic - unknown fault type: {fault_type}
"""

    return base_log + panic_log


def _generate_mock_analysis(fault_type: str, fault_info: dict) -> str:
    """生成模拟的分析摘要。"""
    return f"""=== Fault Injection Summary ===
Fault Type: {fault_type}
Mode: Mock (Simulated Data)
Expected Panic: {fault_info['expected_panic']}
Expected Root Cause: {fault_info['expected_root_cause']}
Difficulty: {fault_info['difficulty']}

=== Generated Files ===
- vmcore.elf: Mock empty file (real vmcore would be ~100MB)
- boot.log: Simulated panic log with real kernel patterns

=== Purpose ===
This mock data allows testing the evaluation workflow logic
without requiring actual QEMU execution and vmcore generation.

To use real fault injection, set execution_mode='real' in state.
"""


def get_fault_types_for_testing() -> list[dict]:
    """获取可用于测试的故障类型列表。

    Returns list of dicts with 'type', 'description', 'difficulty'
    """
    return [
        {
            "type": ft,
            "description": info["description"],
            "difficulty": info["difficulty"],
            "expected_root_cause": info["expected_root_cause"],
        }
        for ft, info in FAULT_TYPES.items()
    ]


def get_fault_info(fault_type: str) -> dict | None:
    """获取指定故障类型的详细信息。"""
    return FAULT_TYPES.get(fault_type)