"""Per-agent capability integration tests.

每个 agent 一个测试函数，调用真实 LLM + 真实 vmcore/crash/QEMU，
断言关键字段非空、文件生成、契约合法。用于迭代回归。

运行方式：
    # 全部 agent 能力测试（约 10-15 min，调真 LLM + QEMU）
    pytest tests/test_agent_capabilities.py --run-online -v

    # 单个 agent
    pytest tests/test_agent_capabilities.py::test_validator_capability --run-online -v

前置条件：
    - maintenance_config.json 配好可用的 GLM-5.2/DeepSeek API key
    - /tmp/deadlock_input.txt 存在（或通过 DEADLOCK_INPUT 环境变量指定）
    - vmcore/vmlinux/boot_kernel 路径在输入文件中正确指向
    - QEMU 已安装（test_expert 测试需要）
    - Claude Code CLI 已安装（kernel_expert 测试需要）
"""

import os
import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.knowledge_base import knowledge_base_node
from agents.kernel_expert import kernel_expert_node
from agents.llm_display import ensure_output_dir, get_expert_output_file
from agents.pm import pm_node
from agents.test_expert import test_expert_node as run_test_expert_node
from agents.tool_expert import tool_expert_node
from agents.validator import validator_node
from config import load_config
from graph.rn_state import make_initial_state


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DEFAULT_INPUT_FILE = "/tmp/deadlock_input.txt"
DEFAULT_CONFIG_PATH = "maintenance_config.json"

DEADLOCK_INPUT = """vmcore 文件: /home/liumingrui/lumen/deadlock_analysis_output/vmcore.elf
vmlinux 文件: /home/liumingrui/code/OLK-6.6/vmlinux
boot_kernel 文件: /home/liumingrui/code/OLK-6.6/arch/x86/boot/bzImage

问题描述: 内核发生 hung_task panic。
故障类型: deadlock
执行模式: real
"""


@pytest.fixture(scope="module")
def deadlock_input() -> str:
    """Load deadlock input text from file or fallback to built-in template."""
    input_file = os.environ.get("DEADLOCK_INPUT", DEFAULT_INPUT_FILE)
    if Path(input_file).exists():
        return Path(input_file).read_text(encoding="utf-8", errors="replace")
    return DEADLOCK_INPUT


@pytest.fixture(scope="module")
def config_path() -> str:
    return os.environ.get("LUMEN_CONFIG", DEFAULT_CONFIG_PATH)


@pytest.fixture(scope="module")
def loaded_config(config_path: str) -> dict:
    return load_config(config_path)


@pytest.fixture(scope="module")
def vmcore_paths(deadlock_input: str):
    """Extract vmcore/vmlinux/boot_kernel paths from input text."""
    paths = {"vmcore_path": "", "vmlinux_path": "", "boot_kernel_path": ""}
    for line in deadlock_input.splitlines():
        lowered = line.lower()
        if "vmcore" in lowered and "文件" in line:
            paths["vmcore_path"] = line.split(":", 1)[-1].strip() if ":" in line else ""
        elif "vmlinux" in lowered and "文件" in line:
            paths["vmlinux_path"] = line.split(":", 1)[-1].strip() if ":" in line else ""
        elif "boot_kernel" in lowered and "文件" in line:
            paths["boot_kernel_path"] = line.split(":", 1)[-1].strip() if ":" in line else ""
    return paths


@pytest.fixture(autouse=True)
def _clean_outputs():
    """Clear /tmp/lumen_outputs before each test for clean state."""
    out_dir = Path("/tmp/lumen_outputs")
    if out_dir.exists():
        for f in out_dir.glob("*.txt"):
            f.unlink()
    ensure_output_dir()
    yield


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def test_validator_capability(deadlock_input: str, config_path: str):
    """validator 应识别 vmcore/vmlinux/boot_kernel 关键词并通过校验。"""
    state = make_initial_state(user_input=deadlock_input, config_path=config_path)
    result = validator_node(state)

    assert result["validation_passed"] is True, (
        f"validator 应通过 deadlock 输入校验，但 validation_passed=False。"
        f" feedback: {result.get('validation_feedback', '')}"
    )
    artifacts = result["input_artifacts_contract"]
    assert artifacts.get("vmcore_path"), "input_artifacts_contract 应包含 vmcore_path"
    assert artifacts.get("vmlinux_path"), "input_artifacts_contract 应包含 vmlinux_path"
    assert artifacts.get("boot_kernel_path"), "input_artifacts_contract 应包含 boot_kernel_path"

    contract = result["validation_contract"]
    assert contract.get("status") == "ok"
    assert "kernel" in (contract.get("detected_signals") or [])


# ---------------------------------------------------------------------------
# PM
# ---------------------------------------------------------------------------

def test_pm_capability(deadlock_input: str, loaded_config: dict):
    """pm 应识别 deadlock 场景，选择 lock_analysis + kernel_log_analysis + knowledge_search。"""
    state = {
        "user_input": deadlock_input,
        "config": loaded_config,
        "validation_passed": True,
        "validation_feedback": "",
        "input_artifacts_contract": {},
    }
    result = pm_node(state)

    required = result["required_experts"]
    assert required, "pm 应至少选择一个 tool_expert"
    assert "knowledge_search" in required, "deadlock 场景应包含 knowledge_search"
    assert "lock_analysis" in required, "deadlock 场景应包含 lock_analysis"
    assert result["issue_id"].startswith("ISSUE-"), f"issue_id 格式错误: {result['issue_id']}"
    assert result["issue_url"].startswith("https://"), f"issue_url 格式错误: {result['issue_url']}"
    assert result["pm_routing_reason"], "pm_routing_reason 不应为空"


# ---------------------------------------------------------------------------
# Tool experts (4 个)
# ---------------------------------------------------------------------------

def _build_tool_expert_state(expert_type: str, deadlock_input: str, loaded_config: dict) -> dict:
    return {
        "expert_type": expert_type,
        "user_input": deadlock_input,
        "config": loaded_config,
        "config_path": os.environ.get("LUMEN_CONFIG", DEFAULT_CONFIG_PATH),
        "input_artifacts_contract": {},
    }


def _check_expert_result(result: dict, expert_type: str, min_output_len: int = 200):
    """Common assertions for tool_expert results.

    LangGraph 的 ToolExpertResult 只保留 4 个顶层字段：
    expert_type / expert_name / analysis_output / structured_output。
    evidence/status/artifacts 在 structured_output 里。
    """
    expert_results = result.get("expert_results", [])
    assert expert_results, f"{expert_type} 应返回 expert_results"
    first = expert_results[0]
    if isinstance(first, dict):
        output = first.get("analysis_output", "")
        structured = first.get("structured_output", {}) or {}
        status = structured.get("status", "")
        evidence = structured.get("evidence", [])
    else:
        output = first.analysis_output
        structured = getattr(first, "structured_output", {}) or {}
        status = structured.get("status", "")
        evidence = structured.get("evidence", [])
    assert output, f"{expert_type} analysis_output 不应为空"
    assert len(output) >= min_output_len, (
        f"{expert_type} analysis_output 过短: {len(output)} < {min_output_len}"
    )
    return output, status, evidence


def test_knowledge_search_capability(deadlock_input: str, loaded_config: dict):
    """knowledge_search 应返回历史相似案例摘要。"""
    state = _build_tool_expert_state("knowledge_search", deadlock_input, loaded_config)
    result = tool_expert_node(state)
    output, status, _ = _check_expert_result(result, "knowledge_search", min_output_len=100)
    # knowledge_search 可能因 RAG 库为空而降级，但应至少有 ANALYSIS 段
    assert "ANALYSIS" in output or "分析" in output, (
        "knowledge_search 输出应包含 ANALYSIS 或 分析 段"
    )


def test_lock_analysis_capability(deadlock_input: str, loaded_config: dict, vmcore_paths: dict):
    """lock_analysis 应调用 crash 工具，识别 ABBA 死锁并产出 evidence。"""
    state = _build_tool_expert_state("lock_analysis", deadlock_input, loaded_config)
    result = tool_expert_node(state)
    output, status, evidence = _check_expert_result(result, "lock_analysis", min_output_len=300)
    # lock_analysis 应执行了 crash 工具调用，有 evidence
    assert evidence, "lock_analysis 应至少有 1 条 evidence（crash 命令输出）"
    # 输出应提及死锁关键证据
    output_lower = output.lower()
    assert any(kw in output_lower for kw in ["mutex", "deadlock", "abba", "blocked", "wait"]), (
        "lock_analysis 输出应包含死锁相关关键词"
    )
    # 输出文件应生成
    out_file = Path(get_expert_output_file("lock_analysis"))
    assert out_file.exists(), f"输出文件未生成: {out_file}"
    assert out_file.stat().st_size > 200, f"输出文件过小: {out_file.stat().st_size}"


def test_crash_analysis_capability(deadlock_input: str, loaded_config: dict):
    """crash_analysis 应调用 crash 工具，分析 vmcore 崩溃原因。"""
    state = _build_tool_expert_state("crash_analysis", deadlock_input, loaded_config)
    result = tool_expert_node(state)
    output, status, evidence = _check_expert_result(result, "crash_analysis", min_output_len=200)
    # crash_analysis 在 deadlock 场景下，pm 路由会去重，但直接调用应能跑通
    # 输出应包含崩溃相关关键词
    output_lower = output.lower()
    assert any(kw in output_lower for kw in ["panic", "call trace", "stack", "hung", "deadlock"]), (
        "crash_analysis 输出应包含崩溃相关关键词"
    )


def test_kernel_log_analysis_capability(deadlock_input: str, loaded_config: dict):
    """kernel_log_analysis 应分析内核日志，提取关键错误信息。"""
    state = _build_tool_expert_state("kernel_log_analysis", deadlock_input, loaded_config)
    result = tool_expert_node(state)
    output, status, _ = _check_expert_result(result, "kernel_log_analysis", min_output_len=150)
    # 应包含日志分析相关关键词
    output_lower = output.lower()
    assert any(kw in output_lower for kw in ["log", "日志", "hung_task", "panic", "boot"]), (
        "kernel_log_analysis 输出应包含日志相关关键词"
    )


# ---------------------------------------------------------------------------
# Kernel expert (Claude Code CLI backend)
# ---------------------------------------------------------------------------

def test_kernel_expert_capability(deadlock_input: str, loaded_config: dict, vmcore_paths: dict):
    """kernel_expert (Claude Code) 应生成 KERNEL_CONTRACT 和真实 reproducer 文件。"""
    # 构造前置 state（含工具专家的 evidence 摘要）
    state = {
        "user_input": deadlock_input,
        "config": loaded_config,
        "input_artifacts_contract": vmcore_paths,
        "expert_results": [
            {
                "expert_name": "锁分析专家",
                "expert_type": "lock_analysis",
                "analysis_output": (
                    "Mutex ABBA deadlock: thread1 (PID 89) holds mutex_alpha waits mutex_beta; "
                    "thread2 (PID 90) holds mutex_beta waits mutex_alpha. "
                    "Both blocked in __mutex_lock."
                ),
                "evidence": [],
            },
        ],
        "test_attempts": 0,
        "test_result": "",
    }

    result = kernel_expert_node(state)

    contract = result["kernel_contract"]
    assert contract, "kernel_contract 不应为空"
    assert contract.get("status") == "ok", f"contract status 应为 ok: {contract.get('status')}"
    assert contract.get("target_arch") == "x86_64", (
        f"target_arch 应为 x86_64: {contract.get('target_arch')}"
    )
    assert contract.get("boot_kernel_path"), "boot_kernel_path 不应为空"
    assert contract.get("reproducer_dir"), "reproducer_dir 不应为空"
    assert contract.get("test_script_path"), "test_script_path 不应为空"
    assert contract.get("expected_signal"), "expected_signal 不应为空"
    assert contract.get("build_status") in {"passed", "skipped", "unknown", "failed"}, (
        f"build_status 异常: {contract.get('build_status')}"
    )

    # reproducer_dir 下的关键文件应真实存在
    reproducer_dir = Path(contract["reproducer_dir"])
    assert reproducer_dir.exists(), f"reproducer_dir 不存在: {reproducer_dir}"
    assert (reproducer_dir / "reproducer.c").exists(), "reproducer.c 未生成"
    assert (reproducer_dir / "test.sh").exists(), "test.sh 未生成"

    # kernel_expert.txt 应有完整分析内容
    out_file = Path(get_expert_output_file("kernel_expert"))
    assert out_file.exists(), "kernel_expert.txt 未生成"
    assert out_file.stat().st_size > 1000, (
        f"kernel_expert.txt 内容过少: {out_file.stat().st_size}"
    )
    content = out_file.read_text(encoding="utf-8", errors="replace")
    assert "KERNEL_CONTRACT" in content, "kernel_expert.txt 应包含 KERNEL_CONTRACT 标记"


# ---------------------------------------------------------------------------
# Test expert (QEMU)
# ---------------------------------------------------------------------------

def test_test_expert_capability(deadlock_input: str, loaded_config: dict, vmcore_paths: dict):
    """test_expert 应执行 QEMU 测试，复现 hung_task panic。"""
    # 先跑 kernel_expert 拿到 contract（依赖 kernel_expert 能力）
    ke_state = {
        "user_input": deadlock_input,
        "config": loaded_config,
        "input_artifacts_contract": vmcore_paths,
        "expert_results": [
            {
                "expert_name": "锁分析专家",
                "expert_type": "lock_analysis",
                "analysis_output": "Mutex ABBA deadlock detected.",
                "evidence": [],
            },
        ],
        "test_attempts": 0,
        "test_result": "",
    }
    ke_result = kernel_expert_node(ke_state)
    contract = ke_result["kernel_contract"]

    # 检查 QEMU 可用
    if not shutil.which("qemu-system-x86_64"):
        pytest.skip("qemu-system-x86_64 未安装，跳过 QEMU 测试")

    # 调用 test_expert
    te_state = {
        "user_input": deadlock_input,
        "config": loaded_config,
        "input_artifacts_contract": vmcore_paths,
        "kernel_contract": contract,
        "test_attempts": 0,
    }
    result = run_test_expert_node(te_state)

    assert result["test_passed"] is True, (
        f"test_expert 应复现成功，但 test_passed=False。test_result:\n{result.get('test_result', '')[:1000]}"
    )
    te_contract = result["test_contract"]
    # boot_log_path 在 artifacts 里（TestResultContract.artifacts）
    artifacts = te_contract.get("artifacts", {}) or {}
    assert artifacts.get("boot_log_path"), (
        f"test_contract.artifacts 应包含 boot_log_path: {artifacts}"
    )
    assert Path(artifacts["boot_log_path"]).exists(), "boot_log 文件应存在"

    # 输出文件
    out_file = Path(get_expert_output_file("test_expert"))
    assert out_file.exists(), "test_expert.txt 未生成"
    content = out_file.read_text(encoding="utf-8", errors="replace")
    assert "TEST PASSED: True" in content or "PASSED_REPRODUCED" in content, (
        f"test_expert.txt 应显示 TEST PASSED: True: {content[:500]}"
    )


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------

def test_knowledge_base_capability(deadlock_input: str, loaded_config: dict, vmcore_paths: dict):
    """knowledge_base 应生成知识库 markdown 文件并导入 Chroma。"""
    # 构造前置 state（模拟完整 workflow 跑完后的状态）
    state = {
        "user_input": deadlock_input,
        "config": loaded_config,
        "input_artifacts_contract": vmcore_paths,
        "validation_contract": {"status": "ok", "validation_passed": True},
        "expert_results": [
            {
                "expert_name": "锁分析专家",
                "expert_type": "lock_analysis",
                "analysis_output": "Mutex ABBA deadlock detected.",
                "evidence": [],
            },
        ],
        "kernel_analysis": "ABBA 死锁分析完成。",
        "kernel_contract": {
            "status": "ok",
            "target_arch": "x86_64",
            "boot_kernel_path": vmcore_paths["boot_kernel_path"],
            "reproducer_dir": "/tmp/test_reproducer",
            "reproducer_module_path": "/tmp/test_reproducer/reproducer.ko",
            "test_script_path": "/tmp/test_reproducer/test.sh",
            "expected_signal": "blocked for more than",
            "build_status": "passed",
        },
        "reproduce_case": "ABBA 死锁复现用例",
        "kernel_diagnosis": "添加 lockdep 检测",
        "target_arch": "x86_64",
        "boot_kernel_path": vmcore_paths["boot_kernel_path"],
        "reproducer_dir": "/tmp/test_reproducer",
        "reproducer_module_path": "/tmp/test_reproducer/reproducer.ko",
        "test_script_path": "/tmp/test_reproducer/test.sh",
        "expected_signal": "blocked for more than",
        "test_result": "QEMU TEST STATUS: ok\nTEST PASSED: True",
        "test_contract": {"test_passed": True, "boot_log_path": "/tmp/qemu_serial.log"},
        "test_attempts": 1,
        "test_passed": True,
        "issue_id": "ISSUE-test0001",
        "issue_url": "https://example.com/issues/ISSUE-test0001",
    }

    result = knowledge_base_node(state)

    knowledge_file = result["knowledge_file"]
    assert knowledge_file, "knowledge_file 不应为空"
    assert Path(knowledge_file).exists(), f"知识库文件未生成: {knowledge_file}"
    assert knowledge_file.endswith(".md"), f"知识库文件应为 .md: {knowledge_file}"

    final_response = result["final_response"]
    assert "问题分析已完成" in final_response, "final_response 应包含完成提示"
    assert "Chroma" in final_response, "final_response 应包含 Chroma 导入结果"
    # Chroma 导入应成功（环境就绪时）
    assert "✓" in final_response or "成功" in final_response, (
        f"Chroma 导入应成功: {final_response}"
    )
