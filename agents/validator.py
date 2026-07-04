from langchain_core.messages import HumanMessage, SystemMessage
from pathlib import Path
import re

from agents.contracts import ValidationResultContract, model_to_dict
from agents.input_artifacts import parse_input_artifacts
from agents.llm_display import call_llm_with_persistence, _print_agent_header, _print_agent_footer, GREEN, YELLOW, _c
from config import get_llm_with_config, load_config, load_prompt_from_file
from graph.rn_state import MaintenanceWorkflowState


def validator_node(state: MaintenanceWorkflowState) -> dict:
    """校验用户输入信息是否完备。

    只负责判断信息是否完整，不完整则要求用户补充，完整则交给 PM。
    """
    config = load_config(state["config_path"])
    input_artifacts = parse_input_artifacts(state.get("user_input", ""))
    rule_result = _validate_input_by_rules(state.get("user_input", ""))
    if rule_result.status in {"ok", "blocked"}:
        # Display rule-based validation result
        _print_agent_header("validator", "Rule-based validation")
        print(f"Status: {_c(GREEN if rule_result.validation_passed else YELLOW, rule_result.status)}")
        print(f"Reason: {rule_result.reason}")
        if rule_result.detected_signals:
            print(f"Detected signals: {_c(GREEN, str(rule_result.detected_signals))}")
        if rule_result.missing_fields:
            print(f"Missing fields: {rule_result.missing_fields}")
        if rule_result.feedback:
            print(f"Feedback: {rule_result.feedback}")
        _print_agent_footer("validator")

        return {
            "validation_passed": rule_result.validation_passed,
            "validation_feedback": rule_result.feedback,
            "validation_contract": model_to_dict(rule_result),
            "input_artifacts_contract": model_to_dict(input_artifacts),
            "config": config,
        }

    agent_config = config.get("agents", {}).get("validator", {})
    default_config = config.get("default", {})
    llm = get_llm_with_config(agent_config, default_config=default_config, agent_name="validator")
    system_prompt = load_prompt_from_file(
        agent_config.get("prompt_file", "prompts/validator.md")
    )

    user_content = f"用户输入:\n{state['user_input']}"

    # 使用持久化版本，自动保存输出到 outputs/
    response = call_llm_with_persistence(
        "validator", "校验输入", llm,
        [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
        persist_dir=Path("outputs"),
    )

    text = response.content.strip()

    # 解析校验结果
    if "VALIDATION: PASSED" in text:
        contract = ValidationResultContract(
            status="ok",
            validation_passed=True,
            reason="llm_fallback_passed",
            feedback="",
        )
        return {
            "validation_passed": True,
            "validation_feedback": "",
            "validation_contract": model_to_dict(contract),
            "input_artifacts_contract": model_to_dict(input_artifacts),
            "config": config,
        }
    else:
        # 提取反馈信息
        feedback = text
        if "VALIDATION: FAILED" in text:
            # 取 FAILED 标记之后的内容作为反馈
            idx = text.find("VALIDATION: FAILED")
            feedback = text[idx + len("VALIDATION: FAILED"):].strip()
            if not feedback:
                feedback = text
        contract = ValidationResultContract(
            status="blocked",
            validation_passed=False,
            reason="llm_fallback_failed",
            feedback=feedback,
        )

        return {
            "validation_passed": False,
            "validation_feedback": feedback,
            "validation_contract": model_to_dict(contract),
            "input_artifacts_contract": model_to_dict(input_artifacts),
            "config": config,
        }


def _validate_input_by_rules(user_input: str) -> ValidationResultContract:
    """Validate obvious input cases without involving an LLM."""
    text = (user_input or "").strip()
    if not text:
        return ValidationResultContract(
            status="blocked",
            validation_passed=False,
            reason="empty_input",
            missing_fields=["problem_description"],
            feedback="输入为空，请补充内核问题现象、日志片段、vmcore/vmlinux 路径或复现信息。",
        )

    if len(text) < 8:
        return ValidationResultContract(
            status="blocked",
            validation_passed=False,
            reason="input_too_short",
            missing_fields=["problem_description"],
            feedback="输入过短，请补充具体内核问题现象、错误日志或相关文件路径。",
        )

    lowered = text.lower()
    signal_patterns = {
        "vmcore": r"\bvmcore\b|/proc/vmcore|kdump",
        "vmlinux": r"\bvmlinux\b",
        "boot_kernel": r"\bboot_kernel\b|\bbzimage\b|arch/.*/boot/(?:bzimage|image)\b",
        "kernel_log": r"\bdmesg\b|\bconsole\b|\blog\b|call trace|stack trace",
        "crash": r"panic|oops|null pointer|unable to handle|kernel bug|bug:|crash|general protection fault",
        "lock": r"deadlock|hung task|blocked for more than|soft lockup|hard lockup|lockdep|mutex|spinlock|rwsem|d state",
        "memory": r"oom|out of memory|page fault|slab|kmemleak|use-after-free|uaf|double free",
        "kernel": r"\bkernel\b|内核",
        "reproducer": r"reproducer|复现|test\.sh|\.ko\b|makefile",
    }
    detected = [
        name
        for name, pattern in signal_patterns.items()
        if re.search(pattern, lowered, re.IGNORECASE)
    ]

    if detected:
        return ValidationResultContract(
            status="ok",
            validation_passed=True,
            reason="rule_detected_kernel_signals",
            detected_signals=detected,
            feedback="",
        )

    vague_patterns = [
        r"有问题",
        r"帮我看看",
        r"不工作",
        r"失败了",
        r"出错了",
        r"problem$",
        r"error$",
    ]
    if any(re.search(pattern, lowered) for pattern in vague_patterns):
        return ValidationResultContract(
            status="blocked",
            validation_passed=False,
            reason="vague_problem_description",
            missing_fields=["problem_symptom", "kernel_log_or_artifact"],
            feedback="问题描述过于笼统，请补充内核错误现象、日志片段、vmcore/vmlinux 路径或复现步骤。",
        )

    return ValidationResultContract(
        status="inconclusive",
        validation_passed=False,
        reason="needs_llm_validation",
        feedback="",
    )
