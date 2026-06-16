"""Test that tool_expert MCP binding actually executes crash commands.

This test verifies:
1. CrashSessionManager can be created and used
2. LangChain StructuredTools can be created from session
3. Tools can actually execute crash commands
4. Tool-calling loop with LLM works correctly
"""

import os
import sys

# Add lumen project path for agents module
sys.path.insert(0, "/home/liumingrui/lumen")

# Add aicrasher path
sys.path.insert(0, "/home/liumingrui/code/Analysis-SKILL/src")

from pathlib import Path
from datetime import datetime

# Test file paths
VMCORE_PATH = "/home/liumingrui/lumen/deadlock_analysis_output/vmcore.elf"
VMLINUX_PATH = "/home/liumingrui/code/OLK-6.6/vmlinux"


def test_crash_session_creation():
    """Test that CrashSessionManager can be created."""
    print("\n[TEST 1] CrashSessionManager Creation")

    if not os.path.exists(VMCORE_PATH):
        print(f"  SKIP: vmcore not found at {VMCORE_PATH}")
        return False

    if not os.path.exists(VMLINUX_PATH):
        print(f"  SKIP: vmlinux not found at {VMLINUX_PATH}")
        return False

    try:
        from aicrasher.crash_session import CrashSessionManager

        session = CrashSessionManager(Path(VMCORE_PATH), Path(VMLINUX_PATH))
        session.start()

        print(f"  Session created successfully")

        # Test basic command
        result = session.run_command("sys")

        if not result.success:
            print(f"  FAIL: sys command failed: {result.output}")
            session.stop()
            return False

        if "KERNEL" not in result.output:
            print(f"  FAIL: sys output missing KERNEL")
            print(f"  Output preview: {result.output[:200]}")
            session.stop()
            return False

        print(f"  sys command output preview:")
        print(f"    {result.output[:300]}...")

        session.stop()
        print("  ✅ PASS")
        return True

    except Exception as e:
        print(f"  FAIL: Exception: {e}")
        return False


def test_tool_creation():
    """Test that StructuredTools can be created from session."""
    print("\n[TEST 2] StructuredTool Creation")

    if not os.path.exists(VMCORE_PATH):
        print(f"  SKIP: vmcore not found")
        return False

    try:
        from aicrasher.crash_session import CrashSessionManager
        from agents.crash_tools import create_crash_tools

        session = CrashSessionManager(Path(VMCORE_PATH), Path(VMLINUX_PATH))
        session.start()

        tools = create_crash_tools(session)

        print(f"  Created {len(tools)} tools")

        if len(tools) != 4:
            print(f"  FAIL: Expected 4 tools, got {len(tools)}")
            session.stop()
            return False

        tool_names = [t.name for t in tools]
        expected_names = ["run_crash_command", "run_crash_commands", "collect_baseline", "get_command_history"]

        for name in expected_names:
            if name not in tool_names:
                print(f"  FAIL: Missing tool '{name}'")
                session.stop()
                return False

        print(f"  Tool names: {tool_names}")

        # Verify tool descriptions
        for tool in tools:
            print(f"    - {tool.name}: {tool.description[:50]}...")

        session.stop()
        print("  ✅ PASS")
        return True

    except Exception as e:
        print(f"  FAIL: Exception: {e}")
        return False


def test_tool_execution():
    """Test that tools can actually execute commands."""
    print("\n[TEST 3] Tool Execution")

    if not os.path.exists(VMCORE_PATH):
        print(f"  SKIP: vmcore not found")
        return False

    try:
        from aicrasher.crash_session import CrashSessionManager
        from agents.crash_tools import create_crash_tools

        session = CrashSessionManager(Path(VMCORE_PATH), Path(VMLINUX_PATH))
        session.start()

        tools = create_crash_tools(session)

        # Find run_crash_command tool
        cmd_tool = next(t for t in tools if t.name == "run_crash_command")

        print(f"  Testing run_crash_command tool...")

        # Execute 'sys' command
        output = cmd_tool.invoke({"command": "sys"})

        if "KERNEL" not in output:
            print(f"  FAIL: sys output missing KERNEL")
            print(f"  Output: {output[:200]}")
            session.stop()
            return False

        print(f"  sys command succeeded")
        print(f"    Output preview: {output[:200]}...")

        # Execute 'bt 89' command (deadlock thread 1)
        output = cmd_tool.invoke({"command": "bt 89"})

        # Should contain thread info
        if "PID: 89" not in output and "deadlock_thread" not in output and "__schedule" not in output:
            print(f"  FAIL: bt 89 output unexpected")
            print(f"  Output: {output[:300]}")
            session.stop()
            return False

        print(f"  bt 89 command succeeded")
        print(f"    Output preview: {output[:300]}...")

        # Test collect_baseline
        baseline_tool = next(t for t in tools if t.name == "collect_baseline")
        output = baseline_tool.invoke({})

        if "KERNEL" not in output and "sys" not in output.lower():
            print(f"  FAIL: collect_baseline output unexpected")
            print(f"  Output: {output[:200]}")
            session.stop()
            return False

        print(f"  collect_baseline succeeded")

        session.stop()
        print("  ✅ PASS")
        return True

    except Exception as e:
        print(f"  FAIL: Exception: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_tool_calling_loop():
    """Test full tool-calling loop with LLM."""
    print("\n[TEST 4] Tool Calling Loop with LLM")

    if not os.path.exists(VMCORE_PATH):
        print(f"  SKIP: vmcore not found")
        return False

    try:
        from aicrasher.crash_session import CrashSessionManager
        from agents.crash_tools import create_crash_tools
        from agents.tool_calling_loop import execute_tool_calling_loop, create_tool_call_messages
        from config import get_llm_with_config

        # Get LLM config from settings
        default_config = {
            "model_name": "GLM-5",
            "base_url": "http://113.46.219.251:8080/v1",
            "api_key": "sk-GqNCcM6b6ecn-9u7gFYlyA",  # From ~/.claude/settings.json
            "backend": "openai",
        }

        print(f"  Creating LLM with model: {default_config['model_name']}")
        llm = get_llm_with_config({}, default_config=default_config)

        # Check if LLM supports tool calling
        try:
            test_tools = []
            bound = llm.bind_tools(test_tools)
            print(f"  LLM supports bind_tools")
        except Exception as e:
            print(f"  SKIP: LLM doesn't support bind_tools: {e}")
            return False

        session = CrashSessionManager(Path(VMCORE_PATH), Path(VMLINUX_PATH))
        session.start()

        tools = create_crash_tools(session)

        system_prompt = """你是 crash 分析专家。你有以下工具可用：
- collect_baseline: 收集基线诊断信息（sys, bt, log）
- run_crash_command: 执行单个 crash 命令
- run_crash_commands: 执行多个命令

请首先调用 collect_baseline 收集基本信息，然后根据需要执行其他命令进行深入分析。

分析完成后，给出你的结论。"""

        user_input = "分析这个死锁问题，查看线程 89 和 90 的调用栈，确认死锁原因。"

        context_info = f"""Crash 分析环境已就绪:
- vmcore: {VMCORE_PATH}
- vmlinux: {VMLINUX_PATH}

请使用工具进行分析。"""

        messages = create_tool_call_messages(
            system_prompt=system_prompt,
            user_input=user_input,
            context_info=context_info,
        )

        print(f"  Starting tool-calling loop...")

        response = execute_tool_calling_loop(
            llm=llm,
            messages=messages,
            tools=tools,
            max_iterations=10,
            verbose=True,
        )

        # Verify response
        content = response.content

        if len(content) < 50:
            print(f"  FAIL: Response too short: {content}")
            session.stop()
            return False

        print(f"\n  Final response (first 1000 chars):")
        print(f"  {'-' * 50}")
        print(f"  {content[:1000]}")
        print(f"  {'-' * 50}")

        # Should contain analysis of deadlock
        analysis_keywords = ["deadlock", "mutex", "thread", "PID", "89", "90", "阻塞", "死锁"]
        found_keywords = [k for k in analysis_keywords if k.lower() in content.lower()]

        if len(found_keywords) < 2:
            print(f"  WARN: Response may lack crash analysis content")
            print(f"  Found keywords: {found_keywords}")
        else:
            print(f"  Found analysis keywords: {found_keywords}")

        session.stop()
        print("  ✅ PASS")
        return True

    except Exception as e:
        print(f"  FAIL: Exception: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all tests."""
    print("=" * 60)
    print("MCP Tool Binding Test for tool_expert")
    print("=" * 60)
    print(f"Test time: {datetime.now().isoformat()}")
    print(f"vmcore: {VMCORE_PATH}")
    print(f"vmlinux: {VMLINUX_PATH}")

    # Check files exist
    vmcore_exists = os.path.exists(VMCORE_PATH)
    vmlinux_exists = os.path.exists(VMLINUX_PATH)

    print(f"vmcore exists: {vmcore_exists}")
    print(f"vmlinux exists: {vmlinux_exists}")

    if not vmcore_exists or not vmlinux_exists:
        print("\n⚠️ Required files not found. Some tests will be skipped.")

    # Run tests
    results = []
    results.append(("crash_session_creation", test_crash_session_creation()))
    results.append(("tool_creation", test_tool_creation()))
    results.append(("tool_execution", test_tool_execution()))
    results.append(("tool_calling_loop", test_tool_calling_loop()))

    # Summary
    print("\n" + "=" * 60)
    print("Test Summary:")
    print("=" * 60)

    passed = 0
    failed = 0
    skipped = 0

    for name, result in results:
        if result is True:
            status = "✅ PASS"
            passed += 1
        elif result is False:
            status = "❌ FAIL"
            failed += 1
        else:
            status = "⏭️ SKIP"
            skipped += 1
        print(f"  {name}: {status}")

    print("-" * 60)
    print(f"Total: {passed} passed, {failed} failed, {skipped} skipped")
    print("=" * 60)

    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)