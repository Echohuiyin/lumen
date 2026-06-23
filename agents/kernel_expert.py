from pathlib import Path
import json
import os
import re

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from agents.contracts import KernelExpertOutput, model_to_dict
from agents.llm_display import call_llm_with_persistence, display_expert_outputs, get_expert_output_file, ensure_output_dir, _format_agent_header_text, _format_agent_footer_text
from agents.kernel_tools import create_kernel_tools
from agents.test_runner import detect_kernel_type, normalize_target_arch
from agents.tool_calling_loop import execute_tool_calling_loop, create_tool_call_messages
from config import get_llm_with_config, load_prompt_from_file
from graph.rn_state import MaintenanceWorkflowState
from paths import PROJECT_ROOT, OUTPUT_DIR


def _log_tool_call(output_file: str, tool_name: str, tool_args: dict, expert_name: str):
    """Log tool execution to output file."""
    args_str = ", ".join(f"{k}={v}" for k, v in tool_args.items()) if tool_args else ""
    with open(output_file, "a", encoding="utf-8") as f:
        f.write(f"\n[{expert_name}] 执行工具: {tool_name}({args_str})\n")
        f.write("等待输出...\n")


def _write_tool_call_output(output_file: str, content: str, expert_name: str):
    """Write final tool-calling output to file, preserving tool call logs."""
    footer = _format_agent_footer_text(expert_name)

    with open(output_file, "a", encoding="utf-8") as f:
        # Append final result after tool call logs
        f.write("\n\n## 最终分析结果\n\n")
        f.write(content + "\n")
        f.write(footer)


def _run_kernel_expert_with_tools(
    llm,
    system_prompt: str,
    user_content: str,
    expert_name: str,
    output_file: str,
    max_iterations: int = 15,
) -> AIMessage:
    """Execute kernel expert analysis with tool calling.

    Creates kernel tools, binds to LLM, runs tool-calling loop,
    and returns final AIMessage with analysis result.
    """
    # Write initial header
    header = _format_agent_header_text(expert_name, "分析构造用例（工具调用）")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("执行模式: real (文件操作和编译工具)\n\n")

    try:
        # Create kernel tools
        tools = create_kernel_tools()

        # Build context info for LLM
        kernel_headers_path = f"/lib/modules/{os.uname().release}/build"
        kernel_headers_exist = os.path.exists(kernel_headers_path)

        home_dir = os.path.expanduser("~")
        context_info = f"""Kernel expert tool environment:

- Home directory: {home_dir} (use this in paths, NOT /root or other guessed paths)
- Kernel Headers: {kernel_headers_path} ({'available' if kernel_headers_exist else 'unavailable'})
- Kernel Version: {os.uname().release}
- Arch: {os.uname().machine}
- Output directory: {OUTPUT_DIR} (for reproducer files)

IMPORTANT: You do NOT need to call crash tools directly! The tool_experts have already
performed crash analysis (sys, ps, bt, log, etc.) and the results are in the evidence.
Use the extracted evidence summary provided in the input to get architecture, panic
reason, and process information. Do NOT use bash to call crash commands!

You have these tools for creating reproducer:
- create_directory: create a directory for the reproducer
- write_file: write source code, Makefile, README, test.sh, etc.
- read_file: read file contents to verify
- compile_module: compile a kernel module (.ko)
- check_file_exists: verify file existence
- list_directory: inspect generated files
- search_files: search source files with ripgrep
- bash: limited to build/test operations ONLY (NOT for crash analysis)

Tool use rules:
- Use write_file for file creation. Do not use bash heredocs for source files.
- Use compile_module for kernel module builds.
- Use search_files for code search.
- Use bash only for: ls, cat, file, make commands (read-only or build operations).
- NEVER use bash to call crash, gdb, or any analysis tools.

Suggested workflow:
1. Read the evidence summary from tool_experts to understand the problem
2. Get target_arch from the evidence summary or sys output excerpt
3. Get boot_kernel_path from the input_artifacts (vmlinux_path or boot_kernel_path)
4. Create reproducer directory: {OUTPUT_DIR}/<bug_type>_reproducer
5. Write reproducer source code (.c) based on the analysis findings
6. Write Makefile with correct kernel build system integration
7. Write test.sh that loads the module and emits clear pass/fail evidence
8. If kernel headers exist, compile the module to verify correctness
9. Output KERNEL_CONTRACT JSON with ALL required fields

KERNEL_CONTRACT MUST include these fields for test_expert handoff:
- target_arch: from evidence summary (MACHINE field in sys output)
- boot_kernel_path: from input_artifacts or vmlinux_path
- reproducer_dir: the directory you created
- reproducer_module_path: the compiled .ko path (if compiled successfully)
- test_script_path: the test.sh you created
- expected_signal: what the boot log should show (e.g., "blocked for more than 120 seconds")

KERNEL_CONTRACT:
```json
{{
  "status": "ok|blocked|failed|degraded",
  "target_arch": "x86_64|arm64|arm32",
  "vmlinux_path": "",
  "boot_kernel_path": "<bootable bzImage/Image path, or vmlinux if no separate boot kernel>",
  "reproducer_dir": "<directory containing generated reproducer files>",
  "reproducer_module_path": "<compiled .ko path>",
  "test_script_path": "<script that loads/runs the reproducer>",
  "expected_signal": "<boot log pattern proving reproduction>",
  "build_status": "passed|failed|skipped",
  "evidence": [],
  "warnings": [],
  "blocked_reason": ""
}}
```

Notes:
- Use {home_dir} in all paths
- Use correct kernel APIs (kthread_run, mutex_lock, init_completion, etc.)
- Makefile MUST use Tab indentation (not spaces)
- For deadlock: create two threads that acquire locks in opposite order (ABBA)
- For panic: create code that triggers the specific kernel panic condition
"""

        # Create messages for tool-calling loop
        messages = create_tool_call_messages(
            system_prompt=system_prompt,
            user_input=user_content,
            context_info=context_info,
        )

        # Execute tool-calling loop
        response = execute_tool_calling_loop(
            llm=llm,
            messages=messages,
            tools=tools,
            max_iterations=max_iterations,
            on_tool_call=lambda name, args: _log_tool_call(output_file, name, args, expert_name),
            verbose=False,
        )

        # Write final output — fallback to summary if content empty
        output_content = response.content
        if not output_content or not output_content.strip():
            # Extract last non-tool messages for summary
            summary_lines = ["（工具调用已完成，LLM 未生成最终文本）"]
            for msg in reversed(messages):
                if hasattr(msg, "content") and msg.content:
                    content = msg.content.strip()
                    if content and len(content) > 50 and not content.startswith("Error"):
                        import textwrap
                        summary_lines.append("\n最后一次有效响应:")
                        summary_lines.append(textwrap.shorten(content, width=300))
                        break
            output_content = "\n".join(summary_lines)
        _write_tool_call_output(output_file, output_content, expert_name)

        return response

    except Exception as e:
        # Execution failed
        error_msg = f"工具调用执行失败: {str(e)}"
        _write_tool_call_output(output_file, error_msg, expert_name)
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

    # 执行工具调用
    response = _run_kernel_expert_with_tools(
        llm=llm,
        system_prompt=system_prompt,
        user_content=user_content,
        expert_name="内核专家",
        output_file=output_file,
        max_iterations=20,
    )

    text = response.content.strip()

    # Fallback for empty response: auto-generate contract from state
    if not text:
        text = _generate_fallback_analysis(expert_results, input_artifacts, state)
        # Search for actual reproducer files
        outputs_dir = OUTPUT_DIR
        reproducer_dir, test_script_path, reproducer_module_path = _find_actual_reproducer_path(outputs_dir)
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
    kernel_contract = _extract_kernel_contract(text)
    if not _kernel_contract_has_handoff(kernel_contract):
        fallback_contract = _kernel_contract_from_markers(
            target_arch=target_arch,
            boot_kernel_path=boot_kernel_path,
            reproducer_dir=reproducer_dir,
            reproducer_module_path=reproducer_module_path,
            test_script_path=test_script_path,
            expected_signal=expected_signal,
        )
        kernel_contract = _merge_kernel_contract(kernel_contract, fallback_contract)

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
        evidence = result.get("evidence", [])
        analysis = result.get("analysis_output", "")
        parts.append(f"### {name} ({etype})")
        if analysis:
            parts.append(analysis[:500])
        for ev in evidence[:3]:
            parts.append(f"- Evidence: {ev.get('kind', '')}: {ev.get('command', '') or ev.get('output_excerpt', '')[:100]}")
        parts.append("")

    parts.append("### 总结")
    parts.append("内核分析已完成，工具专家已收集完整 crash 数据。")
    parts.append("请查看知识库归档获取完整分析报告。")
    return "\n".join(parts)


def _find_actual_reproducer_path(outputs_dir: Path) -> tuple[str, str, str]:
    """Search outputs directory for actual reproducer files.

    Returns: (reproducer_dir, test_script_path, reproducer_module_path)"""
    if not outputs_dir.exists():
        return "", "", ""

    # Find the most recently modified subdirectory with test.sh
    reproducer_dirs = sorted(
        [d for d in outputs_dir.iterdir() if d.is_dir() and (d / "test.sh").exists()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )

    if not reproducer_dirs:
        return "", "", ""

    reproducer_dir = str(reproducer_dirs[0])
    test_script_path = str(reproducer_dirs[0] / "test.sh")

    # Find .ko file in reproducer dir
    ko_files = list(reproducer_dirs[0].glob("*.ko"))
    reproducer_module_path = str(ko_files[0]) if ko_files else ""

    return reproducer_dir, test_script_path, reproducer_module_path


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
    reproducer_dir, test_script_path, reproducer_module_path = _find_actual_reproducer_path(outputs_dir)

    if reproducer_dir and not contract.reproducer_dir:
        fields["reproducer_dir"] = reproducer_dir
    if test_script_path and not contract.test_script_path:
        fields["test_script_path"] = test_script_path
    if reproducer_module_path and not contract.reproducer_module_path:
        fields["reproducer_module_path"] = reproducer_module_path

    # expected_signal from common patterns
    if not contract.expected_signal:
        fields["expected_signal"] = "blocked for more than"

    return fields


def _extract_evidence_summary(expert_results: list) -> str:
    """Extract key evidence from tool expert results for LLM context.

    This provides extracted info so kernel_expert doesn't need to call crash directly.
    """
    summary_parts = []

    for result in expert_results:
        expert_name = result.get("expert_name", "unknown")
        expert_type = result.get("expert_type", "")
        evidence_list = result.get("evidence", [])

        if not evidence_list:
            continue

        # Extract key information from crash evidence
        for ev in evidence_list:
            if ev.get("kind") == "crash_command":
                cmd = ev.get("command", "")
                output = ev.get("output_excerpt", "")

                # Extract arch from sys output
                if "sys" in cmd and output:
                    import re
                    arch_match = re.search(r"MACHINE:\s*(\S+)", output)
                    if arch_match:
                        summary_parts.append(f"- Architecture: {arch_match.group(1)}")

                # Extract panic info
                if "log" in cmd and output:
                    panic_match = re.search(r"Kernel panic - not syncing: (.+)", output)
                    if panic_match:
                        summary_parts.append(f"- Panic: {panic_match.group(1)}")

                    hung_match = re.search(r"blocked for more than (\d+) seconds", output)
                    if hung_match:
                        summary_parts.append(f"- Hung task: blocked for {hung_match.group(1)} seconds")

                # Extract task info from ps output
                if "ps" in cmd and output:
                    # Look for D-state (UN) processes
                    import re
                    un_procs = re.findall(r"^(\S+)\s+(\d+)\s+\d+\s+\S+\s+UN", output, re.MULTILINE)
                    if un_procs:
                        procs_str = ", ".join(f"{p[0]}(PID:{p[1]})" for p in un_procs[:3])
                        summary_parts.append(f"- D-state processes: {procs_str}")

    if not summary_parts:
        return "（从工具专家结果中未提取到关键证据）"

    return "\n".join(summary_parts)


def _extract_section(text: str, marker: str) -> str:
    """从文本中提取标记段落。"""
    pattern = rf"{re.escape(marker)}:\s*\n?(.*?)(?:\n[A-Z_]+:|\Z)"
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).strip() if match else ""


def _extract_scalar_marker(text: str, marker: str) -> str:
    """Extract a one-line marker value and ignore empty placeholders."""
    match = re.search(rf"^{re.escape(marker)}:\s*(.+?)\s*$", text, re.MULTILINE)
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
