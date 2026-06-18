#!/usr/bin/env python3
"""内核专家独立测试脚本

测试 kernel_expert 的工具调用能力，验证：
1. 能否实际创建目录
2. 能否实际写入文件
3. 能否编译内核模块
4. 错误处理和降级逻辑

使用方法:
    python tests/test_kernel_expert.py
"""

import os
import sys
from pathlib import Path

# 添加项目根目录到 Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.kernel_tools import create_kernel_tools, create_directory, write_file, read_file, compile_module, check_file_exists
from agents.tool_calling_loop import execute_tool_calling_loop, create_tool_call_messages
from config import get_llm_with_config, load_config


def test_kernel_tools_basic():
    """测试 kernel_tools 的基本功能"""
    print("=" * 60)
    print("[1] 测试 kernel_tools 基本功能")
    print("=" * 60)

    # 创建测试目录
    test_dir = "outputs/test_kernel_expert_basic"
    print(f"\n测试: create_directory('{test_dir}')")
    result = create_directory(test_dir)
    print(f"结果: {result}")

    # 验证目录创建
    assert os.path.exists(test_dir), "目录创建失败"
    print("✓ 目录创建成功")

    # 测试写入文件
    test_file = f"{test_dir}/test.txt"
    test_content = "Hello, Kernel Expert!"
    print(f"\n测试: write_file('{test_file}', '{test_content}')")
    result = write_file(test_file, test_content)
    print(f"结果: {result}")

    # 验证文件写入
    assert os.path.exists(test_file), "文件创建失败"
    with open(test_file) as f:
        assert f.read() == test_content, "文件内容不匹配"
    print("✓ 文件写入成功")

    # 测试读取文件
    print(f"\n测试: read_file('{test_file}')")
    result = read_file(test_file)
    print(f"结果: {result}")
    print("✓ 文件读取成功")

    # 测试文件检查
    print(f"\n测试: check_file_exists('{test_file}')")
    result = check_file_exists(test_file)
    print(f"结果: {result}")
    assert "exists" in result, "文件检查失败"
    print("✓ 文件检查成功")

    # 测试不存在文件
    print(f"\n测试: check_file_exists('nonexistent.txt')")
    result = check_file_exists("nonexistent.txt")
    print(f"结果: {result}")
    assert "not found" in result, "不存在文件检查失败"
    print("✓ 不存在文件检查正确")

    # 清理测试文件
    import shutil
    shutil.rmtree(test_dir)
    print("\n✓ 测试清理完成")

    print("\n[1] 基本功能测试: 全部通过 ✓")


def test_kernel_tools_compile():
    """测试 kernel_tools 的编译功能"""
    print("\n" + "=" * 60)
    print("[2] 测试 kernel_tools 编译功能")
    print("=" * 60)

    # 检查 kernel headers 是否存在
    kernel_headers = f"/lib/modules/{os.uname().release}/build"
    headers_exist = os.path.exists(kernel_headers)

    print(f"\nKernel Headers: {kernel_headers}")
    print(f"状态: {'✓ 存在' if headers_exist else '✗ 不存在'}")

    if not headers_exist:
        print("\n⚠️ Kernel headers 不存在，跳过编译测试")
        return

    # 创建测试模块
    test_dir = "outputs/test_kernel_module"
    print(f"\n创建测试模块目录: {test_dir}")
    create_directory(test_dir)

    # 创建简单的内核模块源码
    module_c = """// SPDX-License-Identifier: GPL-2.0
#include <linux/module.h>
#include <linux/kernel.h>

static int __init test_init(void) {
    pr_info("Test module loaded\\n");
    return 0;
}

static void __exit test_exit(void) {
    pr_info("Test module unloaded\\n");
}

module_init(test_init);
module_exit(test_exit);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Test Module for Kernel Expert");
"""

    write_file(f"{test_dir}/test.c", module_c)

    # 创建 Makefile
    makefile = """obj-m += test.o

KDIR ?= /lib/modules/$(shell uname -r)/build

all:
	make -C $(KDIR) M=$(PWD) modules

clean:
	make -C $(KDIR) M=$(PWD) clean
"""

    write_file(f"{test_dir}/Makefile", makefile)

    print("\n测试: compile_module")
    result = compile_module(test_dir)
    print(f"结果:\n{result}")

    # 检查编译结果（中英文都支持）
    if "成功" in result or "successful" in result.lower() or "Return code: 0" in result:
        print("\n✓ 编译测试通过")

        # 检查编译产物
        ko_file = f"{test_dir}/test.ko"
        ko_result = check_file_exists(ko_file)
        print(f"检查编译产物: {ko_result}")

        if "exists" in ko_result:
            print("✓ 编译产物存在")
        else:
            print("✗ 编译产物不存在")
    else:
        print("\n✗ 编译测试失败")

    # 清理
    import shutil
    shutil.rmtree(test_dir)
    print("\n✓ 测试清理完成")


def test_kernel_expert_tool_calling():
    """测试 kernel_expert 的工具调用能力"""
    print("\n" + "=" * 60)
    print("[3] 测试 kernel_expert 工具调用能力")
    print("=" * 60)

    # 加载配置（使用默认配置文件）
    config_path = Path(__file__).parent.parent / "config.json"
    if not config_path.exists():
        # 如果没有配置文件，使用空配置
        config = {}
    else:
        config = load_config(str(config_path))

    # 获取 LLM
    agent_config = config.get("agents", {}).get("kernel_expert", {})
    default_config = config.get("default", {})
    llm = get_llm_with_config(agent_config, default_config=default_config, agent_name="kernel_expert")

    if llm is None:
        print("✗ LLM 配置失败，跳过工具调用测试")
        return

    print(f"LLM 类型: {type(llm).__name__}")

    # 创建工具
    tools = create_kernel_tools()
    print(f"工具列表: {[t.name for t in tools]}")

    # 创建测试任务
    test_task = """请创建一个简单的内核模块测试用例：

1. 在 outputs/test_kernel_expert_tool 目录创建复现器
2. 创建一个简单的 hello world 内核模块（hello.c）
3. 创建对应的 Makefile
4. 尝试编译验证

注意：使用正确的内核 API（MODULE_LICENSE 等）
"""

    # 创建消息
    messages = create_tool_call_messages(
        system_prompt="你是内核专家，负责创建内核模块复现器。你有文件操作和编译工具。",
        user_input=test_task,
        context_info="你有以下工具: create_directory, write_file, read_file, compile_module, check_file_exists"
    )

    print("\n开始执行工具调用循环...")
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

        # 验证文件创建
        print("\n验证文件创建:")
        check_result = check_file_exists("outputs/test_kernel_expert_tool/hello.c")
        print(f"hello.c: {check_result}")

        check_result = check_file_exists("outputs/test_kernel_expert_tool/Makefile")
        print(f"Makefile: {check_result}")

        print("\n✓ 工具调用测试完成")

    except Exception as e:
        print(f"\n✗ 工具调用失败: {str(e)}")


def test_kernel_expert_node():
    """测试完整的 kernel_expert_node 函数"""
    print("\n" + "=" * 60)
    print("[4] 测试 kernel_expert_node 完整流程")
    print("=" * 60)

    from agents.kernel_expert import kernel_expert_node

    # 加载配置（使用默认配置文件）
    config_path = Path(__file__).parent.parent / "config.json"
    if not config_path.exists():
        config = {}
    else:
        config = load_config(str(config_path))

    # 创建模拟状态
    state = {
        "user_input": "分析一个简单的内核 panic 问题",
        "expert_results": [
            {
                "expert_name": "crash_analysis",
                "expert_type": "crash",
                "analysis_output": "发现内核在 do_exit 函数中 panic"
            }
        ],
        "config": config,
        "execution_mode": "real",  # 测试 real 模式
    }

    print("\n输入状态:")
    print(f"- 用户输入: {state['user_input']}")
    print(f"- 工具专家结果: {len(state['expert_results'])} 个")
    print(f"- 执行模式: {state['execution_mode']}")

    print("\n执行 kernel_expert_node...")
    print("=" * 60)

    try:
        result = kernel_expert_node(state)

        print("\n" + "=" * 60)
        print("kernel_expert_node 执行完成")
        print("=" * 60)

        print("\n输出结果:")
        print(f"- kernel_analysis: {result.get('kernel_analysis', '')[:200]}...")
        print(f"- reproduce_case: {result.get('reproduce_case', '')[:200]}...")
        print(f"- kernel_diagnosis: {result.get('kernel_diagnosis', '')[:200]}...")

        print("\n✓ kernel_expert_node 测试完成")

    except Exception as e:
        print(f"\n✗ kernel_expert_node 失败: {str(e)}")


def main():
    """运行所有测试"""
    print("=" * 60)
    print("内核专家独立测试")
    print("=" * 60)

    try:
        # 测试 1: 基本工具功能
        test_kernel_tools_basic()

        # 测试 2: 编译功能
        test_kernel_tools_compile()

        # 测试 3: 工具调用能力（需要 LLM）
        test_kernel_expert_tool_calling()

        # 测试 4: 完整流程（需要 LLM）
        test_kernel_expert_node()

        print("\n" + "=" * 60)
        print("所有测试完成")
        print("=" * 60)

    except Exception as e:
        print(f"\n测试执行失败: {str(e)}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()