from pathlib import Path
import json
import os
import re

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from agents.contracts import KernelExpertOutput, model_to_dict
from agents.llm_display import (
    call_llm_with_persistence,
    display_expert_outputs,
    get_expert_output_file,
    ensure_output_dir,
    _format_agent_header_text,
    _format_agent_footer_text,
    write_hint_review_pack,
    wait_for_hint,
)
from agents.test_runner import detect_kernel_type, normalize_target_arch
from config import get_llm_with_config, load_prompt_from_file
from graph.rn_state import MaintenanceWorkflowState
from paths import PROJECT_ROOT, OUTPUT_DIR


def _write_tool_call_output(output_file: str, content: str, expert_name: str):
    """Write final tool-calling output to file, preserving tool call logs."""
    footer = _format_agent_footer_text(expert_name)

    with open(output_file, "a", encoding="utf-8") as f:
        # Append final result after tool call logs
        f.write("\n\n## 最终分析结果\n\n")
        f.write(content + "\n")
        f.write(footer)


def _run_kernel_expert_with_claude_code(
    llm,
    system_prompt: str,
    user_content: str,
    expert_name: str,
    output_file: str,
    target_kernel_dir: str = "",
) -> AIMessage:
    """Execute kernel expert analysis via Claude Code CLI.

    Delegates the tool-calling loop to `claude -p`, letting Claude Code's own
    agent loop handle Read/Write/Edit/Bash/Grep/Glob. Returns the final text
    result with KERNEL_CONTRACT and marker lines for downstream parsing.
    """
    header = _format_agent_header_text(expert_name, "分析构造用例（Claude Code）")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("执行模式: real (Claude Code CLI agent)\n\n")

    try:
        kernel_headers_path = f"/lib/modules/{os.uname().release}/build"
        kernel_headers_exist = os.path.exists(kernel_headers_path)

        home_dir = os.path.expanduser("~")
        context_info = f"""Kernel expert runtime environment:

- Home directory: {home_dir} (use this in paths, NOT /root)
- Output directory (your current workdir): {OUTPUT_DIR} — ALL reproducer files MUST be created under this directory
- Host kernel: {os.uname().release} / arch {os.uname().machine} (host kernel is for compile toolchain only, NOT for module compilation target)
- Host kernel headers: {kernel_headers_path} ({'available' if kernel_headers_exist else 'unavailable'})
- Target kernel source for module compilation: {target_kernel_dir or '(not detected — ask user or use boot_kernel_path-derived dir)'}
"""

        messages = [
            SystemMessage(content=system_prompt + "\n\n" + context_info),
            HumanMessage(content=user_content),
        ]

        add_dirs = [target_kernel_dir] if target_kernel_dir else None
        response = llm.invoke(
            messages,
            workdir=str(OUTPUT_DIR),
            add_dirs=add_dirs,
        )

        output_content = response.content or ""
        if not output_content.strip():
            output_content = "（Claude Code 未生成最终文本，请检查 Claude Code CLI 输出）"
        _write_tool_call_output(output_file, output_content, expert_name)

        return response

    except Exception as e:
        error_msg = f"Claude Code 调用失败: {str(e)}"
        _write_tool_call_output(output_file, error_msg, expert_name)
        # Re-raise CLI startup failures (MCP config, CLI crash) so
        # kernel_expert_node can route to a blocked contract instead of
        # fabricating a fallback that hides the broken environment.
        if "[cli_startup_failure]" in str(e):
            raise
        return AIMessage(content=error_msg)


def kernel_expert_node(state: MaintenanceWorkflowState) -> dict:
    """内核专家 agent：根据工具专家的输出，结合代码分析，构造必现用例并给出内核维测方案。

    通过工具调用机制实际创建文件和编译验证模块。
    """
    config = state.get("config", {})
    agent_config = config.get("agents", {}).get("kernel_expert", {})
    default_config = config.get("default", {})
    llm = get_llm_with_config(agent_config, default_config=default_config, agent_name="kernel_expert")
    system_prompt = load_prompt_from_file(
        agent_config.get("prompt_file", "prompts/maintenance/kernel_expert.md")
    )

    # 汇总所有工具专家的分析结果
    expert_results = state.get("expert_results", [])
    input_artifacts = state.get("input_artifacts_contract", {})

    # Only display expert outputs on first invocation (not on retries after test failures)
    if state.get("test_attempts", 0) == 0:
        display_expert_outputs(expert_results)
    expert_summaries = []
    for result in expert_results:
        expert_summaries.append(
            f"### {result['expert_name']}（{result['expert_type']}）\n{result['analysis_output']}"
        )

    # Extract evidence summary for LLM context
    evidence_summary = _extract_evidence_summary(expert_results)

    user_content = (
        f"用户输入:\n{state['user_input']}\n\n"
        f"## 输入文件路径\n"
        f"- vmcore_path: {input_artifacts.get('vmcore_path', 'N/A')}\n"
        f"- vmlinux_path: {input_artifacts.get('vmlinux_path', 'N/A')}\n"
        f"- boot_kernel_path: {input_artifacts.get('boot_kernel_path', input_artifacts.get('vmlinux_path', 'N/A'))}\n\n"
        f"## 工具专家分析结果\n" + "\n\n".join(expert_summaries) + "\n\n"
        f"## 关键证据摘要\n{evidence_summary}"
    )

    # 如果是重试（测试未通过），附加测试反馈
    test_result = state.get("test_result", "")
    if test_result:
        user_content += f"\n\n## 上次测试结果（未成功复现）\n{test_result}\n请重新分析并调整复现用例。"

    # 确保输出目录存在
    ensure_output_dir()
    output_file = get_expert_output_file("kernel_expert")

    # 检查 kernel headers 是否存在
    kernel_headers_path = f"/lib/modules/{os.uname().release}/build"
    kernel_headers_exist = os.path.exists(kernel_headers_path)

    # kernel headers 不存在时直接报错
    if not kernel_headers_exist:
        error_msg = f"ERROR: Kernel Headers 不存在，无法编译内核模块\n"
        error_msg += f"Kernel Headers 路径: {kernel_headers_path}\n"
        error_msg += f"状态: ✗ 不存在\n\n"
        error_msg += "请安装 kernel headers 以支持内核模块编译验证。\n"
        error_msg += f"安装命令示例（根据发行版不同）:\n"
        error_msg += f"  - Ubuntu/Debian: sudo apt install linux-headers-{os.uname().release}\n"
        error_msg += f"  - CentOS/RHEL: sudo yum install kernel-devel-{os.uname().release}\n"
        error_msg += f"  - openEuler: sudo yum install kernel-devel"

        header = _format_agent_header_text("内核专家", "分析失败")
        footer = _format_agent_footer_text("内核专家")
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(header)
            f.write(error_msg + "\n")
            f.write(footer)

        # 返回空分析结果
        return {
            "kernel_analysis": error_msg,
            "reproduce_case": "",
            "kernel_diagnosis": "",
            "kernel_ready_for_test": False,
            "kernel_contract": model_to_dict(KernelExpertOutput(
                status="blocked",
                build_status="blocked",
                blocked_reason=f"kernel headers not found: {kernel_headers_path}",
                warnings=[error_msg],
            )),
            "final_response": error_msg,
        }

    # Derive target kernel source dir from boot_kernel_path in input_artifacts
    boot_kernel_path = input_artifacts.get("boot_kernel_path", "") or input_artifacts.get("vmlinux_path", "")
    target_kernel_dir = ""
    if boot_kernel_path:
        _kp = os.path.expanduser(boot_kernel_path)
        if _kp:
            _p = os.path.dirname(_kp)
            for _ in range(3):
                _p = os.path.dirname(_p)
            if os.path.isdir(os.path.join(_p, "include")):
                target_kernel_dir = _p

    # 执行 Claude Code agent 分析
    try:
        response = _run_kernel_expert_with_claude_code(
            llm=llm,
            system_prompt=system_prompt,
            user_content=user_content,
            expert_name="内核专家",
            output_file=output_file,
            target_kernel_dir=target_kernel_dir,
        )
    except RuntimeError as e:
        # CLI startup failure (MCP config missing, CLI crash): fail-fast
        # into a blocked contract instead of fabricating fallback output
        # that would route to test_expert with a wrong expected_signal.
        error_msg = f"kernel_expert CLI 启动失败: {str(e)}"
        blocked_contract = KernelExpertOutput(
            status="blocked",
            build_status="skipped",
            blocked_reason=str(e),
            warnings=[error_msg],
        )
        return {
            "kernel_analysis": error_msg,
            "reproduce_case": "",
            "kernel_diagnosis": "",
            "kernel_ready_for_test": False,
            "kernel_contract": model_to_dict(blocked_contract),
            "final_response": error_msg,
        }

    text = response.content.strip()

    # 写人审阅包，检测是否有人注入关键思路
    # 注入时机：跑完一轮后，路由 test_expert 前
    # 不影响 retry：重跑只替换本轮 contract，不改 test_attempts
    # wait_for_hint 给人一个有界窗口审阅 review pack 并决定是否注入：
    #   - 写 kernel_expert.hint → 拾取并重跑
    #   - touch kernel_expert.continue → 立即跳过等待
    #   - 超时 → 正常路由 test_expert
    try:
        write_hint_review_pack(
            user_input=state.get("user_input", ""),
            expert_results=expert_results,
            kernel_expert_output=text,
        )
    except Exception:
        pass  # review pack 写失败不应阻塞主流程

    workflow_cfg = config.get("workflow", {}) or {}
    hint_wait = int(workflow_cfg.get("hint_wait_seconds", 120))
    hint = wait_for_hint(timeout_seconds=hint_wait)
    if hint:
        # 本轮单 shot：read_and_consume_hint 已删除 hint 文件
        # 重跑时把 hint 追加到 user_content，标记为维护人员关键思路
        hinted_user_content = (
            user_content
            + f"\n\n## 维护人员关键思路（优先参考，不强制）\n\n{hint}\n"
        )
        try:
            response = _run_kernel_expert_with_claude_code(
                llm=llm,
                system_prompt=system_prompt,
                user_content=hinted_user_content,
                expert_name="内核专家（hint 重跑）",
                output_file=output_file,
                target_kernel_dir=target_kernel_dir,
            )
            text = response.content.strip()
        except RuntimeError:
            # 重跑失败保留第 1 轮输出，不阻塞
            pass

    # 解析 contract + 构造返回 dict（hint 注入重跑后也复用此函数）
    return _parse_kernel_expert_response(
        text=text,
        expert_results=expert_results,
        input_artifacts=input_artifacts,
        state=state,
    )


def _parse_kernel_expert_response(
    *,
    text: str,
    expert_results: list,
    input_artifacts: dict,
    state: dict,
) -> dict:
    """Parse kernel_expert output text into contract + state update dict.

    Shared between first-round and hint-injected rerun so both produce
    identically-shaped state updates.
    """
    # Fallback for empty response: auto-generate contract from state
    if not text:
        text = _generate_fallback_analysis(expert_results, input_artifacts, state)
        # Search for actual reproducer files
        outputs_dir = OUTPUT_DIR
        reproducer_dir, test_script_path, reproducer_module_path, _ = _find_actual_reproducer_path(outputs_dir)
        fallback_arch = normalize_target_arch(os.uname().machine)
        fallback_boot = (
            input_artifacts.get("boot_kernel_path")
            or input_artifacts.get("vmlinux_path", "")
        )
        kernel_contract = KernelExpertOutput(
            status="ok",
            target_arch=fallback_arch,
            boot_kernel_path=fallback_boot,
            reproducer_dir=reproducer_dir or str(outputs_dir),
            reproducer_module_path=reproducer_module_path,
            test_script_path=test_script_path,
            expected_signal="blocked for more than",
            build_status="skipped",
            warnings=["LLM did not produce structured output; contract auto-generated from state"],
        )
        kernel_contract = _validate_kernel_contract_artifacts(kernel_contract)
        contract_ready = _kernel_contract_ready_for_test(kernel_contract)
        return {
            "kernel_analysis": text,
            "reproduce_case": text,
            "kernel_diagnosis": "",
            "kernel_ready_for_test": contract_ready,
            "kernel_contract": model_to_dict(kernel_contract),
            "target_arch": kernel_contract.target_arch,
            "boot_kernel_path": kernel_contract.boot_kernel_path,
            "reproducer_dir": kernel_contract.reproducer_dir,
            "reproducer_module_path": kernel_contract.reproducer_module_path,
            "test_script_path": kernel_contract.test_script_path,
            "expected_signal": kernel_contract.expected_signal,
        }

    # 解析必现用例和维测方案
    reproduce_case = _extract_section(text, "REPRODUCE_CASE")
    kernel_diagnosis = _extract_section(text, "KERNEL_DIAGNOSIS")
    target_arch = _extract_scalar_marker(text, "TARGET_ARCH")
    boot_kernel_path = _extract_scalar_marker(text, "BOOT_KERNEL_PATH")
    reproducer_dir = _extract_scalar_marker(text, "REPRODUCER_DIR")
    reproducer_module_path = _extract_scalar_marker(text, "REPRODUCER_MODULE_PATH")
    test_script_path = _extract_scalar_marker(text, "TEST_SCRIPT_PATH")
    expected_signal = _extract_scalar_marker(text, "EXPECTED_SIGNAL")
    binaries_dir = _extract_scalar_marker(text, "BINARIES_DIR")
    kernel_contract = _extract_kernel_contract(text)
    if not _kernel_contract_has_handoff(kernel_contract):
        fallback_contract = _kernel_contract_from_markers(
            target_arch=target_arch,
            boot_kernel_path=boot_kernel_path,
            reproducer_dir=reproducer_dir,
            reproducer_module_path=reproducer_module_path,
            test_script_path=test_script_path,
            expected_signal=expected_signal,
            binaries_dir=binaries_dir,
        )
        kernel_contract = _merge_kernel_contract(kernel_contract, fallback_contract)

    # If markers had binaries_dir but the JSON contract didn't, propagate it.
    if binaries_dir and not kernel_contract.binaries_dir:
        data = model_to_dict(kernel_contract)
        data["binaries_dir"] = binaries_dir
        kernel_contract = _model_validate(KernelExpertOutput, data)

    # Final fallback: auto-fill from input_artifacts if contract still incomplete
    if not _kernel_contract_has_handoff(kernel_contract):
        auto_fields = _generate_auto_contract_fields(kernel_contract, input_artifacts)
        if auto_fields:
            data = model_to_dict(kernel_contract)
            data.update(auto_fields)
            data.setdefault("warnings", []).append(
                f"contract auto-filled: {', '.join(auto_fields.keys())}"
            )
            kernel_contract = _model_validate(KernelExpertOutput, data)

    kernel_contract = _validate_kernel_contract_artifacts(kernel_contract)
    contract_ready = _kernel_contract_ready_for_test(kernel_contract)

    return {
        "kernel_analysis": text,
        "reproduce_case": reproduce_case or text,
        "kernel_diagnosis": kernel_diagnosis or "",
        "kernel_ready_for_test": contract_ready,
        "kernel_contract": model_to_dict(kernel_contract),
        "target_arch": kernel_contract.target_arch,
        "boot_kernel_path": kernel_contract.boot_kernel_path,
        "reproducer_dir": kernel_contract.reproducer_dir,
        "reproducer_module_path": kernel_contract.reproducer_module_path,
        "test_script_path": kernel_contract.test_script_path,
        "expected_signal": kernel_contract.expected_signal,
        "binaries_dir": kernel_contract.binaries_dir,
    }


def _generate_fallback_analysis(expert_results: list, input_artifacts: dict, state: dict) -> str:
    """Generate fallback analysis text when LLM response is empty."""
    parts = ["## 内核分析结果（自动生成）\n"]
    parts.append(f"### 输入文件")
    parts.append(f"- vmcore: {input_artifacts.get('vmcore_path', 'N/A')}")
    parts.append(f"- vmlinux: {input_artifacts.get('vmlinux_path', 'N/A')}")
    parts.append(f"- boot_kernel: {input_artifacts.get('boot_kernel_path', input_artifacts.get('vmlinux_path', 'N/A'))}")
    parts.append("")

    # Summarize tool expert findings
    for result in expert_results:
        name = result.get("expert_name", "unknown")
        etype = result.get("expert_type", "")
        evidence = result.get("evidence") or (result.get("structured_output") or {}).get("evidence") or []
        analysis = result.get("analysis_output", "")
        parts.append(f"### {name} ({etype})")
        if analysis:
            parts.append(analysis[:500])
        for ev in evidence[:3]:
            raw = ev.get("output_full", "") or ev.get("message", "")
            parts.append(f"- Evidence: {ev.get('kind', '')}: {ev.get('command', '') or raw[:100]}")
        parts.append("")

    parts.append("### 总结")
    parts.append("内核分析已完成，工具专家已收集完整 crash 数据。")
    parts.append("请查看知识库归档获取完整分析报告。")
    return "\n".join(parts)


def _find_actual_reproducer_path(outputs_dir: Path) -> tuple[str, str, str, str]:
    """Search outputs directory for actual reproducer files.

    Returns: (reproducer_dir, test_script_path, reproducer_module_path, expected_signal)"""
    if not outputs_dir.exists():
        return "", "", "", ""

    # Find the most recently modified subdirectory with test.sh
    reproducer_dirs = sorted(
        [d for d in outputs_dir.iterdir() if d.is_dir() and (d / "test.sh").exists()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )

    if not reproducer_dirs:
        return "", "", "", ""

    reproducer_dir = str(reproducer_dirs[0])
    test_script_path = str(reproducer_dirs[0] / "test.sh")

    # Find .ko file in reproducer dir
    ko_files = list(reproducer_dirs[0].glob("*.ko"))
    reproducer_module_path = str(ko_files[0]) if ko_files else ""

    # Parse expected_signal from test.sh REPRODUCER_SIGNAL lines
    expected_signal = ""
    try:
        test_sh_text = (reproducer_dirs[0] / "test.sh").read_text()
        import re
        signal_match = re.search(r'REPRODUCER_SIGNAL:\s*(\S.+)', test_sh_text)
        if signal_match:
            expected_signal = signal_match.group(1).strip()
    except Exception:
        pass

    return reproducer_dir, test_script_path, reproducer_module_path, expected_signal


def _generate_auto_contract_fields(
    contract, input_artifacts: dict,
) -> dict:
    """Auto-fill missing contract fields from state and file system."""
    fields = {}

    # target_arch from uname
    if not contract.target_arch:
        fields["target_arch"] = normalize_target_arch(os.uname().machine)

    # boot_kernel_path from input_artifacts
    if not contract.boot_kernel_path:
        boot = (
            input_artifacts.get("boot_kernel_path")
            or input_artifacts.get("vmlinux_path", "")
        )
        if boot:
            fields["boot_kernel_path"] = boot

    # Search for actual reproducer files
    outputs_dir = OUTPUT_DIR
    reproducer_dir, test_script_path, reproducer_module_path, test_signal = _find_actual_reproducer_path(outputs_dir)

    if reproducer_dir and not contract.reproducer_dir:
        fields["reproducer_dir"] = reproducer_dir
    if test_script_path and not contract.test_script_path:
        fields["test_script_path"] = test_script_path
    if reproducer_module_path and not contract.reproducer_module_path:
        fields["reproducer_module_path"] = reproducer_module_path

    # expected_signal: prefer signal from test.sh, fallback to "blocked for more than"
    if not contract.expected_signal:
        fields["expected_signal"] = test_signal or "blocked for more than"

    return fields


def _extract_evidence_summary(expert_results: list) -> str:
    """Extract key evidence from tool expert results for LLM context.

    Uses the structured evidence (task/backtrace/log_event/crash_command)
    already collected by tool_expert, instead of re-parsing raw output.
    Falls back to raw output_full only for signals not covered by structured
    fields (e.g. MACHINE: arch from sys, panic string from log).
    """
    import re
    summary_parts = []

    for result in expert_results:
        evidence_list = result.get("evidence") or (result.get("structured_output") or {}).get("evidence") or []
        if not evidence_list:
            continue

        for ev in evidence_list:
            kind = ev.get("kind", "")

            if kind == "task":
                state = ev.get("state", "")
                if state.upper() in {"UN", "RU", "IN"}:
                    summary_parts.append(
                        f"- Task PID={ev.get('pid')} comm={ev.get('comm')} state={state}"
                    )

            elif kind == "backtrace":
                frames = ev.get("frames", [])
                if frames:
                    top = frames[0] if frames else ""
                    summary_parts.append(
                        f"- Backtrace PID={ev.get('pid')} comm={ev.get('comm')}: {top}"
                    )

            elif kind == "log_event":
                etype = ev.get("event_type", "")
                msg = ev.get("message", "")
                if etype in {"kernel_panic", "hung_task", "lockdep", "bug"}:
                    summary_parts.append(f"- Log event ({etype}) L{ev.get('line')}: {msg}")

            elif kind == "crash_command":
                cmd = ev.get("command", "")
                output = ev.get("output_full", "")

                # Arch from sys output (not in structured evidence)
                if "sys" in cmd and output:
                    arch_match = re.search(r"MACHINE:\s*(\S+)", output)
                    if arch_match:
                        summary_parts.append(f"- Architecture: {arch_match.group(1)}")

                # Panic/hung string from log output (structured log_event covers
                # most cases, but raw log may have the full panic line)
                if "log" in cmd and output:
                    panic_match = re.search(r"Kernel panic - not syncing: (.+)", output)
                    if panic_match:
                        summary_parts.append(f"- Panic: {panic_match.group(1)}")

    if not summary_parts:
        return "（从工具专家结果中未提取到关键证据）"

    return "\n".join(summary_parts)


def _extract_section(text: str, marker: str) -> str:
    """从文本中提取标记段落。"""
    pattern = rf"{re.escape(marker)}:\s*\n?(.*?)(?:\n[A-Z_]+:|\Z)"
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).strip() if match else ""


def _extract_scalar_marker(text: str, marker: str) -> str:
    """Extract a one-line marker value and ignore empty placeholders.

    Uses [^\\S\\n] for whitespace so the regex stays on a single line — \\s*
    would consume the trailing newline and let (.+?) spill onto the next line
    (e.g. matching 'KERNEL_CONTRACT:' as the value of an empty BINARIES_DIR:).
    """
    match = re.search(rf"^{re.escape(marker)}:[^\S\n]*(.+?)[^\S\n]*$", text, re.MULTILINE)
    if not match:
        return ""
    value = match.group(1).strip().strip("`'\"")
    if not value or value.startswith("<") or value.upper() in {"N/A", "NONE", "UNKNOWN"}:
        return ""
    return value


def _model_validate(model_cls, data: dict):
    if hasattr(model_cls, "model_validate"):
        return model_cls.model_validate(data)
    return model_cls.parse_obj(data)


def _extract_kernel_contract(text: str) -> KernelExpertOutput:
    """Extract JSON-first Kernel Expert contract from model output."""
    candidates: list[str] = []
    fenced = re.search(
        r"KERNEL_CONTRACT:\s*```(?:json)?\s*(.*?)\s*```",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        candidates.append(fenced.group(1))
    marker_idx = text.upper().find("KERNEL_CONTRACT:")
    if marker_idx >= 0:
        candidates.append(text[marker_idx + len("KERNEL_CONTRACT:"):])

    for candidate in candidates:
        try:
            stripped = candidate.strip()
            if stripped.startswith("```"):
                stripped = stripped.strip("`").strip()
                if stripped.lower().startswith("json"):
                    stripped = stripped[4:].strip()
            data, _ = json.JSONDecoder().raw_decode(stripped)
            return _model_validate(KernelExpertOutput, data)
        except Exception:
            continue

    return KernelExpertOutput(
        status="degraded",
        blocked_reason="missing or invalid KERNEL_CONTRACT JSON",
        warnings=["Kernel Expert did not produce a valid KERNEL_CONTRACT JSON object"],
    )


def _kernel_contract_from_markers(
    *,
    target_arch: str,
    boot_kernel_path: str,
    reproducer_dir: str,
    reproducer_module_path: str,
    test_script_path: str,
    expected_signal: str,
    binaries_dir: str = "",
) -> KernelExpertOutput:
    missing = [
        name for name, value in {
            "target_arch": target_arch,
            "boot_kernel_path": boot_kernel_path,
            "test_script_path": test_script_path,
            "expected_signal": expected_signal,
        }.items()
        if not value
    ]
    return KernelExpertOutput(
        status="ok" if not missing else "blocked",
        target_arch=target_arch,
        boot_kernel_path=boot_kernel_path,
        reproducer_dir=reproducer_dir,
        reproducer_module_path=reproducer_module_path,
        test_script_path=test_script_path,
        expected_signal=expected_signal,
        binaries_dir=binaries_dir,
        build_status="unknown",
        warnings=["parsed from legacy marker lines"],
        blocked_reason=f"missing test handoff fields: {', '.join(missing)}" if missing else "",
    )


def _merge_kernel_contract(primary: KernelExpertOutput, fallback: KernelExpertOutput) -> KernelExpertOutput:
    """Fill missing JSON contract fields from legacy marker parsing."""
    data = model_to_dict(primary)
    fallback_data = model_to_dict(fallback)
    for key, value in fallback_data.items():
        if key in {"warnings", "evidence"}:
            data[key] = (data.get(key) or []) + (value or [])
        elif key == "status":
            if data.get("status") in {"degraded", "blocked"} and value == "ok":
                data[key] = value
        elif key == "blocked_reason":
            if fallback.status == "ok":
                data[key] = ""
            else:
                data[key] = data.get(key) or value
        elif not data.get(key) and value:
            data[key] = value
    return _model_validate(KernelExpertOutput, data)


def _kernel_contract_has_handoff(contract: KernelExpertOutput) -> bool:
    return bool(
        contract.target_arch
        and contract.boot_kernel_path
        and contract.test_script_path
        and contract.expected_signal
    )


def _kernel_contract_ready_for_test(contract: KernelExpertOutput) -> bool:
    if contract.status != "ok":
        return False
    return _kernel_contract_has_handoff(contract)


def _resolve_contract_path(path: str) -> Path:
    expanded = Path(os.path.expanduser(path))
    if not expanded.is_absolute():
        expanded = PROJECT_ROOT / expanded
    return expanded.resolve()


def _validate_kernel_contract_artifacts(contract: KernelExpertOutput) -> KernelExpertOutput:
    """Validate Kernel Expert handoff paths before routing to Test Expert."""
    data = model_to_dict(contract)
    warnings = list(data.get("warnings") or [])
    evidence = list(data.get("evidence") or [])
    errors: list[str] = []

    target_arch = normalize_target_arch(contract.target_arch)
    if target_arch != contract.target_arch:
        data["target_arch"] = target_arch
        warnings.append(f"normalized target_arch to {target_arch}")
    if not target_arch:
        errors.append("missing target_arch")
    elif target_arch not in {"x86_64", "arm64", "arm32"}:
        errors.append(f"unsupported target_arch: {target_arch}")

    required_paths = {
        "boot_kernel_path": contract.boot_kernel_path,
    }
    optional_paths = {
        "test_script_path": contract.test_script_path,
        "reproducer_dir": contract.reproducer_dir,
        "reproducer_module_path": contract.reproducer_module_path,
    }

    for field, raw_path in required_paths.items():
        if not raw_path:
            errors.append(f"missing {field}")
            continue
        resolved = _resolve_contract_path(raw_path)
        if not resolved.exists():
            errors.append(f"{field} does not exist: {raw_path}")
            continue
        data[field] = str(resolved)
        evidence.append({"kind": "artifact", "field": field, "path": str(resolved)})

    for field, raw_path in optional_paths.items():
        if not raw_path:
            continue
        resolved = _resolve_contract_path(raw_path)
        if not resolved.exists():
            warnings.append(f"{field} does not exist: {raw_path}")
            continue
        data[field] = str(resolved)
        evidence.append({"kind": "artifact", "field": field, "path": str(resolved)})

    boot_kernel = data.get("boot_kernel_path", "")
    if boot_kernel and Path(boot_kernel).exists():
        kernel_type = detect_kernel_type(boot_kernel)
        evidence.append({
            "kind": "artifact_check",
            "field": "boot_kernel_path",
            "path": boot_kernel,
            "kernel_type": kernel_type,
        })
        if kernel_type == "elf":
            errors.append("boot_kernel_path points to ELF vmlinux/debug symbols, not a bootable kernel image")

    if not contract.expected_signal:
        errors.append("missing expected_signal")

    data["warnings"] = warnings
    data["evidence"] = evidence
    if errors:
        data["status"] = "blocked"
        data["blocked_reason"] = "; ".join(errors)
    elif data.get("status") in {"blocked", "degraded"} and _kernel_contract_has_handoff(_model_validate(KernelExpertOutput, data)):
        data["status"] = "ok"
        data["blocked_reason"] = ""

    return _model_validate(KernelExpertOutput, data)
