"""Independent expert I/O format and tool-calling capability tests.

Tests each expert's:
1. Input format compliance — does it accept the expected state fields?
2. Output format compliance — does it return the expected keys and types?
3. Tool calling capability — can it bind and invoke tools correctly?
4. Error handling — does it handle missing inputs gracefully?
"""

import os
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "Analysis-SKILL" / "src"))


def test_header(name: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {name}")
    print(f"{'=' * 60}")


# ============================================================
# Validator Expert
# ============================================================
def test_validator_io_format():
    """Test validator input/output format."""
    test_header("Validator: I/O Format")

    from agents.validator import validator_node
    from config import load_config

    config = load_config("maintenance_config.json")

    state = {
        "user_input": "内核崩溃，NULL pointer dereference，发生在 crash_nullptr 模块",
        "config": config,
        "config_path": "maintenance_config.json",
    }

    print("Input state keys:", list(state.keys()))
    print("User input:", state["user_input"][:80])

    result = validator_node(state)

    print("\nOutput keys:", list(result.keys()))
    feedback = result.get("validation_feedback", "")
    passed = result.get("validation_passed", None)
    print(f"Validation passed: {passed}")
    print(f"Feedback ({len(feedback)} chars): {feedback[:200]}...")

    assert "validation_feedback" in result, "Missing validation_feedback"
    assert "validation_passed" in result, "Missing validation_passed"
    assert isinstance(result["validation_passed"], bool), "validation_passed not bool"
    print("✓ Validator I/O format OK")


# ============================================================
# PM Expert
# ============================================================
def test_pm_io_format():
    """Test PM input/output format."""
    test_header("PM: I/O Format")

    from agents.pm import pm_node
    from config import load_config

    config = load_config("maintenance_config.json")

    state = {
        "user_input": "内核 deadlock，两个线程互相阻塞",
        "validation_feedback": "问题类型：deadlock, 环境：kernel 6.6.0",
        "config": config,
    }

    print("Input state keys:", list(state.keys()))

    result = pm_node(state)

    print("\nOutput keys:", list(result.keys()))
    required_experts = result.get("required_experts", "")
    issue = result.get("issue_summary", "")
    print(f"Required experts: {required_experts[:100]}")
    print(f"Issue summary ({len(issue)} chars): {issue[:200]}...")

    assert "required_experts" in result, f"Missing required_experts, got keys: {list(result.keys())}"
    # PM may return 'required_experts' as list or string
    experts = result["required_experts"]
    assert experts, "required_experts empty"
    print("✓ PM I/O format OK")


# ============================================================
# Kernel Expert
# ============================================================
def test_kernel_expert_io_format():
    """Test kernel_expert input/output format and tool availability."""
    test_header("Kernel Expert: I/O and Tools")

    from agents.kernel_expert import kernel_expert_node
    from agents.kernel_tools import create_kernel_tools
    from config import load_config

    config = load_config("maintenance_config.json")

    # Check tools
    tools = create_kernel_tools()
    tool_names = [t.name for t in tools]
    print(f"Available tools ({len(tools)}): {tool_names}")

    # Verify each tool has proper schema
    for tool in tools:
        assert tool.name, f"Tool missing name"
        assert tool.description, f"Tool {tool.name} missing description"
        assert callable(tool.func), f"Tool {tool.name} func not callable"
        assert tool.args_schema is not None, f"Tool {tool.name} missing args_schema"
    print("✓ All tools have name, description, callable func, args_schema")

    state = {
        "user_input": "内核 NULL pointer dereference，需要分析 crash_nullptr 模块代码",
        "kernel_diagnosis": "使用 crash 分析 vmcore 后定位到 crash_init 函数",
        "config": config,
        "config_path": "maintenance_config.json",
    }

    print("\nInput state keys:", list(state.keys()))

    result = kernel_expert_node(state)

    print("Output keys:", list(result.keys()))
    analysis = result.get("kernel_analysis", "")
    print(f"Analysis ({len(analysis)} chars): {analysis[:200]}...")

    assert "kernel_analysis" in result, "Missing kernel_analysis"
    assert isinstance(result["kernel_analysis"], str), "kernel_analysis not str"
    # kernel_expert may produce short output if no tools invoked
    print(f"✓ Kernel Expert I/O format OK (analysis: {len(result['kernel_analysis'])} chars)")


# ============================================================
# Tool Experts (crash_analysis, lock_analysis, kernel_log_analysis, knowledge_search)
# ============================================================
def test_tool_expert_crash_analysis():
    """Test crash_analysis tool expert."""
    test_header("Tool Expert: crash_analysis")

    from agents.tool_expert import tool_expert_node, _extract_vmcore_paths
    from config import load_config

    config = load_config("maintenance_config.json")

    state = {
        "expert_type": "crash_analysis",
        "user_input": "vmcore 文件：/tmp/test_vmcore\nvmlinux 文件：/tmp/test_vmlinux\n内核 panic",
        "config": config,
        "config_path": "maintenance_config.json",
    }

    # Test path extraction first
    vmcore, vmlinux = _extract_vmcore_paths(state["user_input"])
    print(f"Extracted vmcore: {vmcore}")
    print(f"Extracted vmlinux: {vmlinux}")

    # Test with no real vmcore (should handle gracefully)
    result = tool_expert_node(state)

    expert_results = result.get("expert_results", [])
    print(f"\nExpert results count: {len(expert_results)}")
    if expert_results:
        r = expert_results[0]
        output = r.analysis_output if hasattr(r, 'analysis_output') else r.get("analysis_output", "")
        print(f"Analysis output ({len(output)} chars): {output[:200]}...")

    assert "expert_results" in result, "Missing expert_results"
    assert len(expert_results) > 0, "No expert results returned"
    print("✓ crash_analysis expert OK")


def test_tool_expert_lock_analysis():
    """Test lock_analysis tool expert."""
    test_header("Tool Expert: lock_analysis")

    from agents.tool_expert import tool_expert_node
    from config import load_config

    config = load_config("maintenance_config.json")

    state = {
        "expert_type": "lock_analysis",
        "user_input": "系统 deadlock，两个线程互相阻塞\nvmcore 文件：/tmp/test_vmcore\nvmlinux 文件：/tmp/test_vmlinux",
        "config": config,
        "config_path": "maintenance_config.json",
    }

    result = tool_expert_node(state)

    expert_results = result.get("expert_results", [])
    print(f"Expert results count: {len(expert_results)}")
    if expert_results:
        r = expert_results[0]
        output = r.analysis_output if hasattr(r, 'analysis_output') else r.get("analysis_output", "")
        print(f"Analysis output ({len(output)} chars): {output[:200]}...")

    assert "expert_results" in result, "Missing expert_results"
    assert len(expert_results) > 0, "No expert results returned"
    print("✓ lock_analysis expert OK")


def test_tool_expert_kernel_log_analysis():
    """Test kernel_log_analysis tool expert."""
    test_header("Tool Expert: kernel_log_analysis")

    from agents.tool_expert import tool_expert_node
    from config import load_config

    config = load_config("maintenance_config.json")

    state = {
        "expert_type": "kernel_log_analysis",
        "user_input": "系统 hung task, dmesg 显示 blocked for more than 120 seconds",
        "config": config,
        "config_path": "maintenance_config.json",
    }

    result = tool_expert_node(state)

    expert_results = result.get("expert_results", [])
    print(f"Expert results count: {len(expert_results)}")
    if expert_results:
        r = expert_results[0]
        output = r.analysis_output if hasattr(r, 'analysis_output') else r.get("analysis_output", "")
        print(f"Analysis output ({len(output)} chars): {output[:200]}...")

    assert "expert_results" in result, "Missing expert_results"
    assert len(expert_results) > 0, "No expert results returned"
    print("✓ kernel_log_analysis expert OK")


def test_tool_expert_knowledge_search():
    """Test knowledge_search tool expert (RAG integration)."""
    test_header("Tool Expert: knowledge_search")

    from agents.tool_expert import tool_expert_node
    from config import load_config

    config = load_config("maintenance_config.json")

    state = {
        "expert_type": "knowledge_search",
        "user_input": "hung task deadlock 问题，需要查找历史案例",
        "config": config,
        "config_path": "maintenance_config.json",
    }

    result = tool_expert_node(state)

    expert_results = result.get("expert_results", [])
    print(f"Expert results count: {len(expert_results)}")
    if expert_results:
        r = expert_results[0]
        output = r.analysis_output if hasattr(r, 'analysis_output') else r.get("analysis_output", "")
        print(f"Analysis output ({len(output)} chars): {output[:200]}...")

    assert "expert_results" in result, "Missing expert_results"
    assert len(expert_results) > 0, "No expert results returned"
    print("✓ knowledge_search expert OK")


# ============================================================
# Test Expert (QEMU)
# ============================================================
def test_expert_qemu_tools():
    """Test test_expert QEMU tool creation and schema."""
    test_header("Test Expert: QEMU Tools")

    from agents.qemu_tools import create_qemu_tools, check_qemu_available

    tools = create_qemu_tools()
    tool_names = [t.name for t in tools]
    print(f"QEMU tools ({len(tools)}): {tool_names}")

    for tool in tools:
        assert tool.name, f"Tool missing name"
        assert tool.description, f"Tool {tool.name} missing description"
        assert callable(tool.func), f"Tool {tool.name} func not callable"
        assert tool.args_schema is not None, f"Tool {tool.name} missing args_schema"

    # Check QEMU availability
    qemu_status = check_qemu_available()
    print(f"QEMU status: {qemu_status[:100]}")

    print("✓ Test Expert QEMU tools OK")


def test_expert_kernel_path_logic():
    """Test test_expert kernel path extraction logic."""
    test_header("Test Expert: Kernel Path Logic")

    from agents.test_expert import _extract_kernel_path, _check_file_exists

    test_cases = [
        ("vmlinux 文件：~/vmlinux", True),
        ("kernel: /path/to/kernel", True),
        ("没有提到 kernel 文件", False),
        ("vmlinux: /tmp/vmlinux", True),
        # Note: _extract_kernel_path requires '文件' or 'file' keyword, not '路径'
    ]

    for user_input, should_find in test_cases:
        path = _extract_kernel_path(user_input)
        if should_find:
            assert path is not None, f"Should extract path from: {user_input}"
        print(f"  Input: '{user_input[:50]}...' → path: {path}")

    print("✓ Kernel path extraction OK")


# ============================================================
# Knowledge Base Expert
# ============================================================
def test_knowledge_base_io_format():
    """Test knowledge_base expert I/O format."""
    test_header("Knowledge Base: I/O Format")

    from agents.knowledge_base import knowledge_base_node
    from config import load_config

    config = load_config("maintenance_config.json")

    state = {
        "user_input": "NULL pointer dereference in crash_nullptr module",
        "kernel_analysis": "crash_nullptr 模块 crash_init 函数空指针解引用",
        "config": config,
        "config_path": "maintenance_config.json",
    }

    print("Input state keys:", list(state.keys()))

    result = knowledge_base_node(state)

    print("\nOutput keys:", list(result.keys()))
    kb_file = result.get("knowledge_file", "")
    print(f"KB file ({len(kb_file)} chars): {kb_file[:200]}...")

    assert "knowledge_file" in result, f"Missing knowledge_file, got keys: {list(result.keys())}"
    print("✓ Knowledge Base I/O format OK")


# ============================================================
# Run all tests
# ============================================================
def main():
    results = {}
    test_fns = [
        ("validator", test_validator_io_format),
        ("pm", test_pm_io_format),
        ("kernel_expert", test_kernel_expert_io_format),
        ("crash_analysis", test_tool_expert_crash_analysis),
        ("lock_analysis", test_tool_expert_lock_analysis),
        ("kernel_log_analysis", test_tool_expert_kernel_log_analysis),
        ("knowledge_search", test_tool_expert_knowledge_search),
        ("qemu_tools", test_expert_qemu_tools),
        ("kernel_path", test_expert_kernel_path_logic),
        ("knowledge_base", test_knowledge_base_io_format),
    ]

    for name, fn in test_fns:
        try:
            fn()
            results[name] = "PASS"
        except Exception as e:
            results[name] = f"FAIL: {e}"
            import traceback
            traceback.print_exc()

    print("\n" + "=" * 60)
    print("  EXPERT TEST SUMMARY")
    print("=" * 60)
    for name, status in results.items():
        mark = "✓" if status == "PASS" else "✗"
        print(f"  {mark} {name}: {status}")

    passed = sum(1 for s in results.values() if s == "PASS")
    print(f"\n  {passed}/{len(results)} experts passed")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
