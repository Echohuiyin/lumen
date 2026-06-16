"""故障生成 Agent：调用本地 kernel-fault-injection skill 生成测试数据

生成已知内核故障（NULL指针、死锁、软锁定等），产生 vmcore 和 boot.log，
作为 Maintenance Workflow 的输入数据进行自迭代验证。
"""

import subprocess
from pathlib import Path

from config import PROJECT_ROOT, get_skill_path


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
    """故障生成节点：生成指定类型的内核故障

    输入：
    - fault_type: 故障类型 (nullptr/softlockup/deadlock/panic/stack_overflow)
    - iteration_count: 当前迭代次数

    输出：
    - generated_vmcore: vmcore 文件路径
    - generated_boot_log: boot.log 文件路径
    - expected_fault: 预期故障特征（用于评估）
    """
    fault_type = state.get("fault_type", "nullptr")
    iteration_count = state.get("iteration_count", 1)

    if fault_type not in FAULT_TYPES:
        return {"fault_generation_error": f"Unknown fault type: {fault_type}"}

    fault_info = FAULT_TYPES[fault_type]
    output_dir = PROJECT_ROOT / "test_outputs" / f"{fault_type}_iter{iteration_count}"

    # 执行真实故障注入
    result = _run_fault_injection(fault_type, output_dir)

    if result.get("success"):
        return {
            "generated_vmcore": result["vmcore_path"],
            "generated_boot_log": result["boot_log_path"],
            "expected_fault": fault_info,
            "fault_description": fault_info["description"],
        }
    else:
        return {"fault_generation_error": result.get("error", "Unknown error")}


def _run_fault_injection(fault_type: str, output_dir: Path) -> dict:
    """执行故障注入 - 使用本地技能"""
    skill_path = get_skill_path("kernel-fault-injection")

    if not skill_path:
        return {
            "success": False,
            "error": "kernel-fault-injection skill not found in local skills directory"
        }

    output_dir.mkdir(parents=True, exist_ok=True)

    script_path = skill_path / "scripts" / "run_fault_injection.sh"

    if not script_path.exists():
        return {
            "success": False,
            "error": f"run_fault_injection.sh not found at {script_path}"
        }

    print(f"\n[故障生成] 执行故障注入: {fault_type}")
    print(f"  Local Skill: {skill_path}")
    print(f"  Output: {output_dir}")

    try:
        result = subprocess.run(
            ["bash", str(script_path), fault_type, "--output", str(output_dir)],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(skill_path),
        )

        if result.returncode != 0:
            return {
                "success": False,
                "error": f"Script failed (exit {result.returncode}): {result.stderr[:500]}",
                "stdout": result.stdout[:1000],
            }

        vmcore_path = output_dir / "vmcore.elf"
        boot_log_path = output_dir / "boot.log"

        # 检查技能默认输出位置
        if not vmcore_path.exists():
            default_output = skill_path.parent.parent / "test_outputs" / f"{fault_type}_x86_64"
            alt_vmcore = default_output / "vmcore.elf"
            alt_boot_log = default_output / "boot.log"

            if alt_vmcore.exists():
                vmcore_path = alt_vmcore
                boot_log_path = alt_boot_log

        if vmcore_path.exists() or boot_log_path.exists():
            return {
                "success": True,
                "vmcore_path": str(vmcore_path) if vmcore_path.exists() else "",
                "boot_log_path": str(boot_log_path) if boot_log_path.exists() else "",
                "stdout": result.stdout[:500],
            }
        else:
            return {
                "success": False,
                "error": f"Output files not generated. stdout: {result.stdout[:500]}"
            }

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Fault injection timed out (300s)"}
    except Exception as e:
        return {"success": False, "error": str(e)}