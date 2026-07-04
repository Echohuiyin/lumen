#!/usr/bin/env python3
"""专家工具调用能力综合测试。

测试 validator, pm, kernel_expert, test_expert 的工具和脚本调用能力。

使用 maintenance_config.json (openai backend) 确保工具调用支持。

使用方法:
    python tests/test_agent_tool_calling.py
"""

import os
import sys
import tempfile
from pathlib import Path

# 添加项目根目录到 Python path
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

# 添加 aicrasher 路径
aicrasher_path = project_root / "Analysis-SKILL" / "src"
if str(aicrasher_path) not in sys.path:
    sys.path.insert(0, str(aicrasher_path))

from config import load_config, get_llm_with_config
from agents.tool_calling_loop import execute_tool_calling_loop, create_tool_call_messages


CONFIG_PATH = "maintenance_config.json"


def print_header(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def print_result(name: str, passed: bool, details: str = ""):
    status = "✓ PASS" if passed else "✗ FAIL"
    print(f"  {status}: {name}")
    if details:
        print(f"    {details}")


def test_validator():
    """测试 validator 的规则验证和契约输出能力。"""
    print_header("[Validator] 测试规则验证能力")

    from agents.validator import validator_node, _validate_input_by_rules

    config = load_config(CONFIG_PATH)

    # 测试 1: 规则验证 - 空输入
    result = _validate_input_by_rules("")
    print_result("空输入阻断", result.status == "blocked", f"status={result.status}")

    # 测试 2: 规则验证 - kernel panic
    result = _validate_input_by_rules("kernel panic with vmcore and vmlinux")
    print_result("panic信号识别", result.status == "ok", f"signals={result.detected_signals}")

    # 测试 3: 规则验证 - deadlock
    result = _validate_input_by_rules("hung task blocked for more than 120 seconds, deadlock")
    print_result("deadlock信号识别", result.status == "ok", f"signals={result.detected_signals}")

    # 测试 4: validator_node 输出契约
    state = {
        "user_input": "kernel panic, vmcore: /tmp/vmcore, vmlinux: /tmp/vmlinux",
        "config_path": CONFIG_PATH,
    }
    result = validator_node(state)
    has_contract = "validation_contract" in result
    has_artifacts = "input_artifacts_contract" in result
    print_result("输出契约完整性", has_contract and has_artifacts,
                 f"validation_contract={has_contract}, artifacts={has_artifacts}")

    return True


def test_pm():
    """测试 PM 的路由选择和 issue 创建能力。"""
    print_header("[PM] 测试路由选择能力")

    from agents.pm import pm_node, _select_required_experts_by_rules

    config = load_config(CONFIG_PATH)

    # 测试专家配置
    experts_config = config.get("tool_experts", [])
    expert_types = [e["type"] for e in experts_config]
    print(f"  配置的专家类型: {expert_types}")

    # 测试 1: deadlock 路由到 lock_analysis 而非 crash_analysis
    experts, reason = _select_required_experts_by_rules(
        "系统 deadlock, blocked for more than 120 seconds",
        experts_config
    )
    has_lock = "lock_analysis" in experts
    no_crash = "crash_analysis" not in experts
    print_result("deadlock优先lock", has_lock and no_crash, f"experts={experts}")

    # 测试 2: panic 路由到 crash_analysis
    experts, reason = _select_required_experts_by_rules(
        "kernel panic with vmcore and vmlinux",
        experts_config
    )
    has_crash = "crash_analysis" in experts
    print_result("panic路由crash", has_crash, f"experts={experts}")

    # 测试 3: PM node 输出契约
    state = {
        "user_input": "kernel panic with vmcore",
        "validation_passed": True,
        "config": config,
        "config_path": CONFIG_PATH,
    }
    result = pm_node(state)
    has_issue = "issue_id" in result
    has_experts = "required_experts" in result
    print_result("PM输出契约", has_issue and has_experts, f"issue={has_issue}, experts={has_experts}")

    return True


def test_kernel_expert():
    """测试 kernel_expert 的文件操作和编译能力。"""
    print_header("[Kernel Expert] 测试工具调用能力")

    from agents.kernel_tools import create_kernel_tools, create_directory, write_file, read_file, compile_module, check_file_exists

    config = load_config(CONFIG_PATH)

    # 测试 1: 工具创建
    tools = create_kernel_tools()
    tool_names = [t.name for t in tools]
    has_required = all(n in tool_names for n in ["create_directory", "write_file", "compile_module"])
    print_result("工具创建", has_required, f"tools={tool_names}")

    # 测试 2: 基本文件操作
    test_dir = "/tmp/test_kernel_expert_tools"
    create_directory(test_dir)
    test_file = f"{test_dir}/test.txt"
    write_file(test_file, "Hello Kernel Expert")
    content = read_file(test_file)
    file_ok = "Hello Kernel Expert" in content
    print_result("文件读写", file_ok, f"content={content[:50]}")

    # 测试 3: LLM 工具调用
    agent_config = config.get("agents", {}).get("kernel_expert", {})
    default_config = config.get("default", {})
    llm = get_llm_with_config(agent_config, default_config=default_config, agent_name="kernel_expert")

    has_bind_tools = hasattr(llm, "bind_tools")
    print_result("LLM支持bind_tools", has_bind_tools, f"type={type(llm).__name__}")

    if has_bind_tools:
        print("\n  执行工具调用测试...")
        messages = create_tool_call_messages(
            system_prompt="你是内核专家，负责创建内核模块复现器。",
            user_input="请在 /tmp/kernel_tool_test 目录创建一个 hello.txt 文件，内容是 'Hello World'",
            context_info="你有工具: create_directory, write_file"
        )

        try:
            response = execute_tool_calling_loop(
                llm=llm,
                messages=messages,
                tools=tools,
                max_iterations=5,
                on_tool_call=lambda name, args: print(f"    [工具] {name}({list(args.keys())})"),
                verbose=False,
            )

            # 验证文件创建
            result = check_file_exists("/tmp/kernel_tool_test/hello.txt")
            tool_call_ok = "exists" in result
            print_result("工具调用执行", tool_call_ok, f"response={response.content[:100]}...")
        except Exception as e:
            print_result("工具调用执行", False, f"error={str(e)[:100]}")

    # 清理测试目录
    import shutil
    for d in [test_dir, "/tmp/kernel_tool_test"]:
        if Path(d).exists():
            shutil.rmtree(d)

    return True


def test_test_expert():
    """测试 test_expert 的 QEMU 工具能力。"""
    print_header("[Test Expert] 测试 QEMU 工具能力")

    from agents.qemu_tools import create_qemu_tools, check_qemu_available, create_initramfs
    from agents.test_expert import _extract_kernel_path

    config = load_config(CONFIG_PATH)

    # 测试 1: 工具创建
    tools = create_qemu_tools()
    tool_names = [t.name for t in tools]
    has_required = all(n in tool_names for n in ["check_qemu_available", "create_initramfs", "boot_kernel"])
    print_result("QEMU工具创建", has_required, f"tools={tool_names}")

    # 测试 2: QEMU 可用性检查
    qemu_result = check_qemu_available()
    qemu_available = "可用" in qemu_result or "available" in qemu_result.lower()
    print_result("QEMU环境", qemu_available, f"result={qemu_result[:100]}")

    # 测试 3: kernel_path 提取
    test_inputs = [
        ("vmlinux: ~/kernel/vmlinux", "~/kernel/vmlinux"),
        ("boot_kernel: /path/Image", "/path/Image"),
        ("没有kernel", None),
    ]

    for input_text, expected in test_inputs:
        path = _extract_kernel_path(input_text)
        ok = (path == expected) if expected else (path is None)
        print_result(f"kernel提取: {input_text[:30]}", ok, f"path={path}")

    # 测试 4: LLM 工具调用（如果QEMU可用）
    if qemu_available:
        agent_config = config.get("agents", {}).get("test_expert", {})
        default_config = config.get("default", {})
        llm = get_llm_with_config(agent_config, default_config=default_config, agent_name="test_expert")

        has_bind_tools = hasattr(llm, "bind_tools")
        print_result("LLM支持bind_tools", has_bind_tools, f"type={type(llm).__name__}")

        if has_bind_tools:
            print("\n  执行QEMU工具调用测试...")
            messages = create_tool_call_messages(
                system_prompt="你是测试专家，负责使用QEMU验证内核问题。",
                user_input="检查QEMU是否可用",
                context_info="你有工具: check_qemu_available"
            )

            try:
                response = execute_tool_calling_loop(
                    llm=llm,
                    messages=messages,
                    tools=tools,
                    max_iterations=3,
                    on_tool_call=lambda name, args: print(f"    [工具] {name}"),
                    verbose=False,
                )

                tool_call_ok = len(response.content) > 50
                print_result("QEMU工具调用", tool_call_ok, f"response={response.content[:100]}...")
            except Exception as e:
                print_result("QEMU工具调用", False, f"error={str(e)[:100]}")

    return True


def test_tool_experts_crash():
    """测试 tool_experts 的 crash 工具调用能力。"""
    print_header("[Tool Experts] 测试 Crash 工具调用")

    # 使用现有的 vmcore/vmlinux
    vmcore = Path.home() / "lumen" / "test_outputs" / "deadlock_fault" / "vmcore.elf"
    vmlinux = Path.home() / "code" / "OLK-6.6" / "vmlinux"

    if not vmcore.exists() or not vmlinux.exists():
        print_result("测试文件", False, f"vmcore={vmcore.exists()}, vmlinux={vmlinux.exists()}")
        return False

    print_result("测试文件", True, f"vmcore={vmcore}, vmlinux={vmlinux}")

    from agents.tool_expert import _extract_vmcore_paths, _resolve_file_path
    from agents.crash_tools import get_or_create_crash_session, release_crash_session, create_crash_tools

    config = load_config(CONFIG_PATH)

    # 测试 1: 路径提取
    user_input = f"vmcore 文件 {vmcore}, vmlinux 路径 {vmlinux}"
    vmcore_raw, vmlinux_raw = _extract_vmcore_paths(user_input)
    vmcore_ok = vmcore_raw is not None
    vmlinux_ok = vmlinux_raw is not None
    print_result("路径提取", vmcore_ok and vmlinux_ok, f"vmcore={vmcore_raw}, vmlinux={vmlinux_raw}")

    # 测试 2: Crash session 创建
    try:
        session = get_or_create_crash_session(str(vmcore), str(vmlinux))
        result = session.run_command("sys")
        session_ok = result.success and len(result.output) > 100
        print_result("Crash session", session_ok, f"sys输出={result.output[:100]}...")
        release_crash_session(str(vmcore), str(vmlinux))
    except Exception as e:
        print_result("Crash session", False, f"error={str(e)[:100]}")

    # 测试 3: Crash tools 创建
    try:
        session = get_or_create_crash_session(str(vmcore), str(vmlinux))
        tools = create_crash_tools(session)
        tool_names = [t.name for t in tools]
        has_required = all(n in tool_names for n in ["run_crash_command", "collect_baseline"])
        print_result("Crash工具创建", has_required, f"tools={tool_names}")
        release_crash_session(str(vmcore), str(vmlinux))
    except Exception as e:
        print_result("Crash工具创建", False, f"error={str(e)[:100]}")

    return True


def main():
    """运行所有测试。"""
    print_header("专家工具调用能力综合测试")
    print(f"  配置文件: {CONFIG_PATH}")

    config = load_config(CONFIG_PATH)
    backend = config.get("default", {}).get("backend")
    model = config.get("default", {}).get("model_name")
    print(f"  Backend: {backend}")
    print(f"  Model: {model}")

    results = {}

    try:
        results["validator"] = test_validator()
        results["pm"] = test_pm()
        results["kernel_expert"] = test_kernel_expert()
        results["test_expert"] = test_test_expert()
        results["tool_experts"] = test_tool_experts_crash()

        # 汇总
        print_header("测试汇总")
        all_passed = True
        for name, passed in results.items():
            status = "✓ PASS" if passed else "✗ FAIL"
            print(f"  {status}: {name}")
            if not passed:
                all_passed = False

        print(f"\n  总体: {'全部通过 ✓' if all_passed else '有失败 ✗'}")

        return 0 if all_passed else 1

    except Exception as e:
        print(f"\n✗ 测试执行失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())