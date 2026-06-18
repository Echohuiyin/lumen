#!/usr/bin/env python3
"""测试专家独立测试脚本

测试 test_expert 的工具调用能力，验证：
1. QEMU 工具是否可用
2. 工具调用路径的逻辑（kernel_path 条件）
3. kernel 不存在时直接报错
4. 完整的验证流程

使用方法:
    python tests/test_test_expert.py
"""

import os
import sys
from pathlib import Path

# 添加项目根目录到 Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.qemu_tools import create_qemu_tools, check_qemu_available, create_initramfs, boot_kernel, analyze_boot_log
from agents.test_expert import test_expert_node, _extract_kernel_path, _check_file_exists
from agents.tool_calling_loop import execute_tool_calling_loop, create_tool_call_messages
from config import get_llm_with_config, load_config


def test_qemu_tools_basic():
    """测试 QEMU 工具的基本功能"""
    print("=" * 60)
    print("[1] 测试 QEMU 工具基本功能")
    print("=" * 60)

    # 创建 QEMU 工具
    tools = create_qemu_tools()
    print(f"\n创建的 QEMU 工具: {[t.name for t in tools]}")

    # 测试 check_qemu_available
    print("\n测试: check_qemu_available")
    result = check_qemu_available()
    print(f"结果:\n{result}")

    if "可用" in result or "available" in result.lower():
        print("✓ QEMU 环境可用")
        qemu_available = True
    else:
        print("✗ QEMU 环境不可用")
        qemu_available = False

    return qemu_available


def test_kernel_path_extraction():
    """测试 kernel_path 提取逻辑"""
    print("\n" + "=" * 60)
    print("[2] 测试 kernel_path 提取逻辑")
    print("=" * 60)

    # 测试各种输入格式
    test_cases = [
        "vmlinux 文件：~/vmlinux",
        "kernel: /path/to/kernel",
        "Image: ~/test/Image",
        "vmlinux: ~/crash/vmlinux",
        "没有提到 kernel 文件",
        "用户输入中包含 vmlinux 路径: ~/kernel/vmlinux",
    ]

    print("\n测试 kernel_path 提取:")
    for user_input in test_cases:
        kernel_path = _extract_kernel_path(user_input)
        exists = _check_file_exists(kernel_path)
        print(f"\n输入: '{user_input}'")
        print(f"提取路径: {kernel_path}")
        print(f"文件状态: {'✓ 存在' if exists else '✗ 不存在' if kernel_path else '无路径'}")

    print("\n✓ kernel_path 提取测试完成")


def test_execution_mode_logic():
    """测试执行模式逻辑"""
    print("\n" + "=" * 60)
    print("[3] 测试执行模式逻辑")
    print("=" * 60)

    # 加载配置（使用默认配置文件）
    config_path = Path(__file__).parent.parent / "config.json"
    if not config_path.exists():
        config = {}
    else:
        config = load_config(str(config_path))

    # 测试场景 1: kernel_path 不存在 → 直接报错
    print("\n场景 1: kernel_path 不存在 → 直接报错")
    state = {
        "user_input": "测试一个内核 panic 问题（没有提供 vmlinux 路径）",
        "reproduce_case": "创建测试模块",
        "kernel_diagnosis": "使用 dmesg 检查",
        "config": config,
    }
    kernel_path = _extract_kernel_path(state["user_input"])
    kernel_exists = _check_file_exists(kernel_path)
    print(f"kernel_path={kernel_path}")
    print(f"kernel_exists={kernel_exists}")
    print("预期: 直接报错（ERROR: Kernel 文件不存在或未指定）")

    # 测试场景 2: kernel_path 存在（模拟）
    print("\n场景 2: kernel_path 存在 → 使用 QEMU 工具调用")
    # 创建一个临时 vmlinux 文件来测试
    test_kernel_dir = "outputs/test_kernel"
    os.makedirs(test_kernel_dir, exist_ok=True)
    test_vmlinux = f"{test_kernel_dir}/vmlinux"
    Path(test_vmlinux).touch()

    state = {
        "user_input": f"vmlinux 文件: {test_vmlinux}",
        "reproduce_case": "创建测试模块",
        "kernel_diagnosis": "使用 QEMU 验证",
        "config": config,
    }
    kernel_path = _extract_kernel_path(state["user_input"])
    kernel_exists = _check_file_exists(kernel_path)
    print(f"kernel_path={kernel_path}")
    print(f"kernel_exists={kernel_exists}")
    print("预期: 使用 QEMU 工具调用路径")

    # 清理临时文件
    os.remove(test_vmlinux)
    os.rmdir(test_kernel_dir)
    print("\n✓ 执行模式逻辑测试完成")


def test_qemu_tool_calling(qemu_available: bool):
    """测试 QEMU 工具调用能力"""
    print("\n" + "=" * 60)
    print("[4] 测试 QEMU 工具调用能力")
    print("=" * 60)

    if not qemu_available:
        print("\n⚠️ QEMU 不可用，跳过工具调用测试")
        return

    # 加载配置（使用默认配置文件）
    config_path = Path(__file__).parent.parent / "config.json"
    if not config_path.exists():
        config = {}
    else:
        config = load_config(str(config_path))
    agent_config = config.get("agents", {}).get("test_expert", {})
    default_config = config.get("default", {})
    llm = get_llm_with_config(agent_config, default_config=default_config, agent_name="test_expert")

    if llm is None:
        print("✗ LLM 配置失败，跳过工具调用测试")
        return

    print(f"LLM 类型: {type(llm).__name__}")

    # 创建工具
    tools = create_qemu_tools()
    print(f"工具列表: {[t.name for t in tools]}")

    # 创建测试任务
    test_task = """请验证 QEMU 环境：

1. 检查 QEMU 是否可用
2. 如果可用，尝试创建一个简单的 initramfs
3. 如果可用，尝试启动一个最小内核

注意：这是测试验证，不需要完整的问题复现。
"""

    # 创建消息
    messages = create_tool_call_messages(
        system_prompt="你是测试专家，负责使用 QEMU 验证内核问题。你有 QEMU 工具。",
        user_input=test_task,
        context_info="你有以下工具: check_qemu_available, create_initramfs, boot_kernel, analyze_boot_log"
    )

    print("\n开始执行 QEMU 工具调用循环...")
    print("=" * 60)

    try:
        response = execute_tool_calling_loop(
            llm=llm,
            messages=messages,
            tools=tools,
            max_iterations=10,
            on_tool_call=lambda name, args: print(f"[工具调用] {name}({args})"),
            verbose=True,
        )

        print("\n" + "=" * 60)
        print("工具调用完成")
        print("=" * 60)
        print(f"最终响应:\n{response.content[:500]}...")

        print("\n✓ QEMU 工具调用测试完成")

    except Exception as e:
        print(f"\n✗ QEMU 工具调用失败: {str(e)}")


def test_test_expert_node_no_kernel():
    """测试 test_expert_node（kernel_path 不存在）→ 直接报错"""
    print("\n" + "=" * 60)
    print("[5] 测试 test_expert_node - kernel 不存在 → 直接报错")
    print("=" * 60)

    # 加载配置（使用默认配置文件）
    config_path = Path(__file__).parent.parent / "config.json"
    if not config_path.exists():
        config = {}
    else:
        config = load_config(str(config_path))

    state = {
        "user_input": "分析一个内核 panic 问题（没有提供 vmlinux 路径）",
        "reproduce_case": "创建一个简单的测试模块来触发 panic",
        "kernel_diagnosis": "使用 ftrace 和 kprobe 监控",
        "kernel_analysis": "完整的分析报告",
        "config": config,
        "test_attempts": 0,
    }

    kernel_path = _extract_kernel_path(state["user_input"])
    kernel_exists = _check_file_exists(kernel_path)

    print("\n输入状态:")
    print(f"- 用户输入: {state['user_input']}")
    print(f"- kernel_path: {kernel_path}")
    print(f"- kernel_exists: {kernel_exists}")
    print(f"- 验证次数: {state['test_attempts']}")

    print("\n预期: 直接报错（ERROR: Kernel 文件不存在或未指定）")

    print("\n执行 test_expert_node (no kernel)...")
    print("=" * 60)

    try:
        result = test_expert_node(state)

        print("\n" + "=" * 60)
        print("test_expert_node 执行完成")
        print("=" * 60)

        print("\n输出结果:")
        print(f"- test_result: {result.get('test_result', '')[:300]}...")
        print(f"- test_passed: {result.get('test_passed', False)}")
        print(f"- test_attempts: {result.get('test_attempts', 0)}")

        # 检查是否报错
        if "ERROR" in result.get('test_result', '') or "不存在" in result.get('test_result', ''):
            print("\n✓ 正确报错（kernel 不存在）")

        print("\n✓ kernel 不存在测试完成")

    except Exception as e:
        print(f"\n✗ test_expert_node 失败: {str(e)}")


def test_test_expert_node_with_kernel():
    """测试 test_expert_node（kernel_path 存在）→ 使用 QEMU 工具调用"""
    print("\n" + "=" * 60)
    print("[6] 测试 test_expert_node - kernel 存在 → QEMU 工具调用")
    print("=" * 60)

    # 加载配置（使用默认配置文件）
    config_path = Path(__file__).parent.parent / "config.json"
    if not config_path.exists():
        config = {}
    else:
        config = load_config(str(config_path))

    # 创建临时 kernel 文件
    test_kernel_dir = "outputs/test_kernel_for_qemu"
    os.makedirs(test_kernel_dir, exist_ok=True)
    test_vmlinux = f"{test_kernel_dir}/vmlinux"
    Path(test_vmlinux).touch()

    state = {
        "user_input": f"vmlinux 文件: {test_vmlinux}",
        "reproduce_case": "创建一个简单的测试模块来触发 panic",
        "kernel_diagnosis": "使用 QEMU 验证",
        "kernel_analysis": "完整的分析报告",
        "config": config,
        "test_attempts": 0,
    }

    kernel_path = _extract_kernel_path(state["user_input"])
    kernel_exists = _check_file_exists(kernel_path)

    print("\n输入状态:")
    print(f"- 用户输入: {state['user_input']}")
    print(f"- kernel_path: {kernel_path}")
    print(f"- kernel_exists: {kernel_exists}")
    print(f"- 验证次数: {state['test_attempts']}")

    print("\n预期: 使用 QEMU 工具调用（如果 QEMU 可用）")

    # 检查 QEMU 是否可用
    qemu_available = check_qemu_available()
    if "可用" not in qemu_available and "available" not in qemu_available.lower():
        print("\n⚠️ QEMU 不可用，预期会报错")

    print("\n执行 test_expert_node (kernel exists)...")
    print("=" * 60)

    try:
        result = test_expert_node(state)

        print("\n" + "=" * 60)
        print("test_expert_node 执行完成")
        print("=" * 60)

        print("\n输出结果:")
        print(f"- test_result: {result.get('test_result', '')[:300]}...")
        print(f"- test_passed: {result.get('test_passed', False)}")
        print(f"- test_attempts: {result.get('test_attempts', 0)}")

        # 检查是否使用了工具调用
        if "工具调用" in result.get('test_result', '') or "QEMU" in result.get('test_result', ''):
            print("\n✓ 使用了 QEMU 工具调用")

        print("\n✓ kernel 存在测试完成")

    except Exception as e:
        print(f"\n✗ test_expert_node 失败: {str(e)}")

    # 清理临时文件
    os.remove(test_vmlinux)
    os.rmdir(test_kernel_dir)


def main():
    """运行所有测试"""
    print("=" * 60)
    print("测试专家独立测试")
    print("=" * 60)

    try:
        # 测试 1: QEMU 工具基本功能
        qemu_available = test_qemu_tools_basic()

        # 测试 2: kernel_path 提取逻辑
        test_kernel_path_extraction()

        # 测试 3: 执行模式逻辑
        test_execution_mode_logic()

        # 测试 4: QEMU 工具调用能力（如果可用）
        test_qemu_tool_calling(qemu_available)

        # 测试 5: kernel 不存在 → 直接报错
        test_test_expert_node_no_kernel()

        # 测试 6: kernel 存在 → QEMU 工具调用
        test_test_expert_node_with_kernel()

        print("\n" + "=" * 60)
        print("所有测试完成")
        print("=" * 60)

    except Exception as e:
        print(f"\n测试执行失败: {str(e)}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()