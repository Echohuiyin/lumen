from pathlib import Path
import json
import os
import re
import subprocess

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


# ---------------------------------------------------------------------------
# Preflight: kernel config + test_assets scan
# ---------------------------------------------------------------------------

_EXTRACT_IKCONFIG_CANDIDATES = [
    os.path.expanduser("~/linux-next/scripts/extract-ikconfig"),
    os.path.expanduser("~/linux-stable/scripts/extract-ikconfig"),
    os.path.expanduser("~/code/OLK-6.6/scripts/extract-ikconfig"),
    "/lib/modules/$(uname -r)/build/scripts/extract-ikconfig",
]


def _find_extract_ikconfig() -> str | None:
    """Locate extract-ikconfig script in known kernel source paths."""
    import shutil as _shutil
    for path in _EXTRACT_IKCONFIG_CANDIDATES:
        expanded = os.path.expandvars(path)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
    return _shutil.which("extract-ikconfig")


# Config options that influence reproducer strategy. Extracting these up
# front saves the LLM from running extract-ikconfig itself (and burning
# turns on Bash + Read).
_PERTINENT_CONFIG_OPTIONS = [
    "CONFIG_KVM",
    "CONFIG_KVM_INTEL",
    "CONFIG_KVM_AMD",
    "CONFIG_HYPERV",
    "CONFIG_PARAVIRT_SPINLOCKS",
    "CONFIG_KVM_GUEST",
    "CONFIG_PREEMPT",
    "CONFIG_PREEMPT_DYNAMIC",
    "CONFIG_MODULE_FORCE_LOAD",
    "CONFIG_MODVERSIONS",
    "CONFIG_BASIC_MODVERSIONS",
    "CONFIG_KASAN",
    "CONFIG_KASAN_GENERIC",
    "CONFIG_KASAN_INLINE",
    "CONFIG_PANIC_ON_WARN",
    "CONFIG_PANIC_ON_OOPS",
    "CONFIG_CMDLINE",
    "CONFIG_NR_CPUS",
    "CONFIG_NR_CPUS_RANGE_END",
]


def _extract_pertinent_kernel_config(bzimage_path: str) -> dict[str, str]:
    """Run extract-ikconfig on a bzImage and return pertinent CONFIG options.

    Returns an empty dict when extract-ikconfig isn't available or the kernel
    doesn't have IKCONFIG embedded. Failure is non-fatal — the LLM still has
    the fallback path of running Bash commands itself.
    """
    if not bzimage_path or not os.path.isfile(bzimage_path):
        return {}
    ikconfig = _find_extract_ikconfig()
    if not ikconfig:
        return {}
    try:
        result = subprocess.run(
            [ikconfig, bzimage_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return {}
    except Exception:
        return {}
    config: dict[str, str] = {}
    for line in result.stdout.splitlines():
        for opt in _PERTINENT_CONFIG_OPTIONS:
            if line.startswith(opt + "="):
                config[opt] = line.split("=", 1)[1].strip()
            elif line.startswith("# " + opt + " is not set"):
                config[opt] = "n"
    return config


def _scan_test_assets_for_reproducers(test_assets_dir: str) -> list[dict[str, str]]:
    """Scan a test_assets/<case>/ directory for existing reproducer files.

    Looks for:
      - repro_c / repro.c / repro (syzbot C reproducer, often precompiled)
      - *.ko (prebuilt kernel module)
      - REPRODUCTION.md (syzbot's notes on the trigger config)
      - any executable binary (userspace trigger)

    Returns a list of {"name", "path", "kind"} dicts. Empty list if the
    directory doesn't exist or has nothing useful.
    """
    if not test_assets_dir:
        return []
    assets_path = Path(os.path.expanduser(test_assets_dir))
    if not assets_path.is_dir():
        return []
    findings: list[dict[str, str]] = []
    try:
        for entry in sorted(assets_path.iterdir()):
            name = entry.name
            if entry.is_file():
                if name in {"repro_c", "repro", "repro.bin"}:
                    findings.append({"name": name, "path": str(entry), "kind": "syzbot_repro_binary"})
                elif name in {"repro.c", "repro_C", "repro.cc"}:
                    findings.append({"name": name, "path": str(entry), "kind": "syzbot_repro_source"})
                elif name.endswith(".ko"):
                    findings.append({"name": name, "path": str(entry), "kind": "kernel_module"})
                elif name == "REPRODUCTION.md":
                    findings.append({"name": name, "path": str(entry), "kind": "reproduction_notes"})
                elif entry.stat().st_size > 0 and os.access(entry, os.X_OK):
                    findings.append({"name": name, "path": str(entry), "kind": "userspace_trigger"})
            elif entry.is_dir():
                # Nested reproducer dir (e.g. test_assets/<case>/poc/)
                for sub in sorted(entry.iterdir()):
                    if sub.is_file() and sub.name in {"repro_c", "repro", "poc"} and os.access(sub, os.X_OK):
                        findings.append({"name": f"{name}/{sub.name}", "path": str(sub), "kind": "syzbot_repro_binary"})
    except OSError:
        pass
    return findings


def _build_preflight_context(boot_kernel_path: str, test_assets_dir: str) -> str:
    """Build a context string summarizing kernel config + test_assets findings.

    This is injected into the kernel_expert system prompt so the LLM starts
    with concrete knowledge of:
      - whether CONFIG_KVM=y (nested KVM PoC is viable for KVM/HyperV bugs)
      - whether CONFIG_MODULE_FORCE_LOAD=y (kernel module reproducer will load)
      - whether a syzbot repro_c is already in test_assets (reuse it)
      - whether a prebuilt .ko exists (skip compilation)
    """
    parts: list[str] = []

    config = _extract_pertinent_kernel_config(boot_kernel_path)
    if config:
        parts.append("## 目标内核配置（extract-ikconfig 自动提取）")
        parts.append("复现器策略相关 CONFIG 选项（已为你预提取，不要再自己跑 extract-ikconfig）：")
        for opt in _PERTINENT_CONFIG_OPTIONS:
            if opt in config:
                val = config[opt]
                # Annotate key options with strategy implications
                note = ""
                if opt == "CONFIG_KVM" and val == "y":
                    note = "  → KVM 内置，nested KVM PoC 可行"
                elif opt == "CONFIG_HYPERV" and val == "y":
                    note = "  → HyperV 客户机驱动启用"
                elif opt == "CONFIG_PARAVIRT_SPINLOCKS" and val == "y":
                    note = "  → PV spinlock 启用（pvqspinlock bug 可触发）"
                elif opt == "CONFIG_MODULE_FORCE_LOAD" and val != "y":
                    note = "  → 模块强制加载禁用，vermagic 不匹配的 .ko 加载会失败"
                elif opt == "CONFIG_KASAN" and val == "y":
                    note = "  → KASAN 启用，UAF/OOB 会触发 BUG: KASAN: 报告"
                elif opt == "CONFIG_PANIC_ON_WARN" and val == "y":
                    note = "  → WARNING 自动升级为 panic"
                parts.append(f"- {opt}={val}{note}")
        # Surface CONFIG_CMDLINE which often embeds panic_on_warn / numa_fake
        cmdline = config.get("CONFIG_CMDLINE", "")
        if cmdline:
            parts.append(f"- 内核内置 cmdline: {cmdline}")

    assets = _scan_test_assets_for_reproducers(test_assets_dir)
    if assets:
        parts.append("\n## test_assets 中已有的复现器文件（优先复用，不要重写）")
        parts.append("扫描到以下现成复现器资源，**优先复用而不是从头写**：")
        for f in assets:
            kind_label = {
                "syzbot_repro_binary": "syzbot 预编译复现器（直接塞 initramfs /bin/）",
                "syzbot_repro_source": "syzbot 复现器源码（需编译）",
                "kernel_module": "预编译内核模块（直接塞 initramfs /modules/）",
                "userspace_trigger": "用户态触发程序（直接塞 initramfs /bin/）",
                "reproduction_notes": "复现说明文档（含触发配置，必读）",
            }.get(f["kind"], f["kind"])
            parts.append(f"- {f['name']} ({kind_label}): {f['path']}")
        parts.append("")
        parts.append("**决策树**：")
        parts.append("1. 有 syzbot_repro_binary → 直接复用，binaries_dir 填该目录，test.sh 跑 `/bin/<repro>`")
        parts.append("2. 有 syzbot_repro_source → 先尝试编译（gcc -static），失败则降级到自写 PoC")
        parts.append("3. 有 kernel_module → 直接复用，reproducer_module_path 填该 .ko")
        parts.append("4. 有 reproduction_notes → 必读，里面有 smp/numa/timeout 等关键配置")

    if not parts:
        return ""
    return "\n".join(parts)


def _run_kernel_expert_with_claude_code(
    llm,
    system_prompt: str,
    user_content: str,
    expert_name: str,
    output_file: str,
    target_kernel_dir: str = "",
    boot_kernel_path: str = "",
    test_assets_dir: str = "",
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

        # Preflight: extract kernel config + scan test_assets for existing
        # reproducers. Injected into context so the LLM doesn't waste turns
        # re-discovering that, e.g., the kernel has CONFIG_KVM=y (so nested
        # KVM PoC is viable) or that a syzbot repro_c binary is already in
        # test_assets (so it can be reused directly instead of writing a
        # new PoC from scratch).
        preflight = _build_preflight_context(boot_kernel_path, test_assets_dir)
        if preflight:
            context_info += "\n" + preflight

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
        # Re-raise CLI startup failures, max_turns exhaustion, and timeouts
        # so kernel_expert_node can route to a blocked contract instead of
        # fabricating a fallback that picks up stale reproducer dirs from
        # previous E2E runs.
        err_str = str(e)
        if (
            "[cli_startup_failure]" in err_str
            or "[cli_max_turns]" in err_str
            or "timed out" in err_str.lower()
        ):
            raise
        return AIMessage(content=error_msg)


def kernel_expert_node(state: MaintenanceWorkflowState) -> dict:
    """内核专家 agent：根据工具专家的输出，结合代码分析，构造必现用例并给出内核维测方案。

    通过工具调用机制实际创建文件和编译验证模块。
    """
    config = state.get("config", {})
    agent_config = config.get("agents", {}).get("kernel_expert", {})
    default_config = config.get("default", {})

    # If the input specifies kernel_source_path, point semcode MCP's db to
    # that tree's index so cross-tree lookups (kvm/btrfs in linux-next vs
    # deadlock/UAF in OLK-6.6) resolve against the correct source.
    input_artifacts = state.get("input_artifacts_contract", {})
    kernel_source_path = input_artifacts.get("kernel_source_path", "")
    if kernel_source_path and "semcode_mcp" in agent_config:
        candidate_db = os.path.join(
            os.path.expanduser(kernel_source_path), ".semcode.db"
        )
        if os.path.exists(candidate_db):
            agent_config = {**agent_config}
            agent_config["semcode_mcp"] = {
                **agent_config["semcode_mcp"],
                "args": ["-d", candidate_db],
            }

    llm = get_llm_with_config(agent_config, default_config=default_config, agent_name="kernel_expert")
    system_prompt = load_prompt_from_file(
        agent_config.get("prompt_file", "prompts/maintenance/kernel_expert.md")
    )

    # 汇总所有工具专家的分析结果
    expert_results = state.get("expert_results", [])

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

    # Derive test_assets_dir from boot_kernel_path: if bzImage is at
    # test_assets/<case>/bzImage, the parent dir is the case assets dir.
    test_assets_dir = ""
    if boot_kernel_path:
        _bk = Path(os.path.expanduser(boot_kernel_path))
        if _bk.parent.is_dir() and (_bk.parent / "input.txt").exists():
            test_assets_dir = str(_bk.parent)

    # 执行 Claude Code agent 分析
    # Snapshot the mtime of the newest reproducer dir BEFORE the CLI runs,
    # so the max_turns recovery path can distinguish files the agent wrote
    # during this invocation from stale dirs left over by a previous case.
    cli_start_mtime = _newest_reproducer_mtime(OUTPUT_DIR)
    try:
        response = _run_kernel_expert_with_claude_code(
            llm=llm,
            system_prompt=system_prompt,
            user_content=user_content,
            expert_name="内核专家",
            output_file=output_file,
            target_kernel_dir=target_kernel_dir,
            boot_kernel_path=boot_kernel_path,
            test_assets_dir=test_assets_dir,
        )
    except RuntimeError as e:
        # CLI startup failure (MCP config missing, CLI crash), max_turns
        # exhaustion, or timeout. Before blocking, check whether the agent
        # actually wrote reproducer files before it ran out of turns — the
        # btrfs case routinely writes a full reproducer.c + test.sh early
        # then keeps analyzing and hits max_turns without emitting the
        # KERNEL_CONTRACT. Discarding that work forces a full re-run.
        err_str = str(e)
        is_max_turns = "[cli_max_turns]" in err_str or "Reached maximum number of turns" in err_str
        if "timed out" in err_str.lower():
            error_msg = f"kernel_expert CLI 超时: {err_str}"
        elif is_max_turns:
            error_msg = f"kernel_expert CLI 达到 max_turns 上限: {err_str}"
        else:
            error_msg = f"kernel_expert CLI 启动失败: {err_str}"

        # Attempt to recover reproducer files written before the CLI failed.
        # Only do this for max_turns (the agent had time to write files but
        # didn't finish the contract); startup failures and timeouts are
        # unlikely to have produced files and should stay blocked.
        recovered = (
            _recover_reproducer_from_outputs(
                input_artifacts, state, min_mtime=cli_start_mtime,
            )
            if is_max_turns
            else None
        )
        if recovered is not None:
            contract, analysis_text = recovered
            return {
                "kernel_analysis": analysis_text,
                "reproduce_case": analysis_text,
                "kernel_diagnosis": "",
                "kernel_ready_for_test": _kernel_contract_ready_for_test(contract),
                "kernel_contract": model_to_dict(contract),
                "target_arch": contract.target_arch,
                "boot_kernel_path": contract.boot_kernel_path,
                "reproducer_dir": contract.reproducer_dir,
                "reproducer_module_path": contract.reproducer_module_path,
                "test_script_path": contract.test_script_path,
                "expected_signal": contract.expected_signal,
                "binaries_dir": contract.binaries_dir,
                "final_response": analysis_text,
            }

        blocked_contract = KernelExpertOutput(
            status="blocked",
            build_status="skipped",
            blocked_reason=err_str,
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
                boot_kernel_path=boot_kernel_path,
                test_assets_dir=test_assets_dir,
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
    # Detect CLI failure text (timeout, startup error, max_turns) that
    # slipped through as a non-empty AIMessage. Block here instead of running
    # the empty-text fallback that would search outputs/ for stale reproducer
    # dirs and route test_expert with the wrong expected_signal.
    if text and (
        "Claude Code 调用失败" in text
        or "Claude Code timed out" in text
        or "Reached maximum number of turns" in text
        or "[cli_max_turns]" in text
    ):
        blocked_contract = KernelExpertOutput(
            status="blocked",
            build_status="skipped",
            blocked_reason=text,
            warnings=["kernel_expert CLI failed; contract blocked to prevent stale fallback"],
        )
        return {
            "kernel_analysis": text,
            "reproduce_case": "",
            "kernel_diagnosis": "",
            "kernel_ready_for_test": False,
            "kernel_contract": model_to_dict(blocked_contract),
            "final_response": text,
        }

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


def _newest_reproducer_mtime(outputs_dir: Path) -> float | None:
    """Return the mtime of the newest reproducer subdir, or None if none exist.

    Used to snapshot state before a CLI invocation so the max_turns recovery
    path can filter to dirs created during this invocation only.
    """
    if not outputs_dir.exists():
        return None
    try:
        mtimes = [
            d.stat().st_mtime
            for d in outputs_dir.iterdir()
            if d.is_dir() and (d / "test.sh").exists()
        ]
    except OSError:
        return None
    return max(mtimes) if mtimes else None


def _find_actual_reproducer_path(
    outputs_dir: Path,
    min_mtime: float | None = None,
) -> tuple[str, str, str, str]:
    """Search outputs directory for actual reproducer files.

    Args:
        outputs_dir: Directory containing reproducer subdirectories.
        min_mtime: If set, only consider subdirectories whose mtime is
            strictly greater than this timestamp. Used by the max_turns
            recovery path to avoid picking up a stale reproducer dir from
            a previous E2E case (the original P0-2 bug).

    Returns: (reproducer_dir, test_script_path, reproducer_module_path, expected_signal)"""
    if not outputs_dir.exists():
        return "", "", "", ""

    # Find the most recently modified subdirectory with test.sh
    candidates = [
        d for d in outputs_dir.iterdir()
        if d.is_dir() and (d / "test.sh").exists()
        and (min_mtime is None or d.stat().st_mtime > min_mtime)
    ]
    reproducer_dirs = sorted(
        candidates,
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

    # Parse expected_signal from test.sh. The agent is instructed to emit a
    # `REPRODUCER_SIGNAL:` line, but in practice it often writes the signal
    # only in a comment (e.g. "# Expected serial output: WARNING: ... can_finish_ordered_extent").
    # Fall back to scanning comment lines for known kernel-error tokens so
    # the contract carries a useful signal instead of empty.
    expected_signal = ""
    try:
        test_sh_text = (reproducer_dirs[0] / "test.sh").read_text()
        import re
        signal_match = re.search(r'REPRODUCER_SIGNAL:\s*(\S.+)', test_sh_text)
        if signal_match:
            expected_signal = signal_match.group(1).strip()
        if not expected_signal:
            expected_signal = _infer_signal_from_test_sh(test_sh_text)
    except Exception:
        pass

    return reproducer_dir, test_script_path, reproducer_module_path, expected_signal


# Known kernel-error tokens that appear in test.sh comments when the agent
# documents the expected serial output. Order matters: more-specific signals
# (KASAN subtypes) come before generic ones (Kernel panic) so we don't grab
# "Kernel panic" when the real signal is a KASAN report.
_KNOWN_SIGNAL_TOKENS: list[str] = [
    "BUG: KASAN: slab-use-after-free",
    "BUG: KASAN: slab-out-of-bounds",
    "BUG: KASAN:",
    "blocked for more than",
    "hung_task: blocked tasks",
    "BUG: soft lockup",
    "unable to handle kernel NULL pointer",
    "kernel BUG at",
    "Out of memory",
    "WARNING: CPU:",
    "WARNING: at ",
    "Kernel panic",
]


def _infer_signal_from_test_sh(test_sh_text: str) -> str:
    """Scan test.sh comments/text for known kernel-error tokens.

    The agent's test.sh typically has a comment like:
        # Expected serial output (the signal test_expert greps for):
        #   WARNING: ... at fs/btrfs/ordered-data.c:390 can_finish_ordered_extent
        #   Kernel panic - not syncing: kernel: panic_on_warn set ...
    We pick the most specific token that appears in the file.
    """
    for token in _KNOWN_SIGNAL_TOKENS:
        if token.lower() in test_sh_text.lower():
            return token
    return ""


def _recover_reproducer_from_outputs(
    input_artifacts: dict, state: dict, *, min_mtime: float | None = None,
) -> tuple[KernelExpertOutput, str] | None:
    """Recover a usable contract from reproducer files the agent already wrote.

    Used when the CLI hit max_turns but the agent had already created a
    reproducer directory with test.sh (and optionally reproducer.c/.ko).
    Returns (contract, analysis_text) or None if no reproducer dir was found.

    The contract is marked status="ok" with a warning noting it was recovered
    from partial output, so test_expert can proceed. The analysis_text is a
    short summary so knowledge_base has something to archive.

    min_mtime: if set, only consider reproducer dirs created after this
    timestamp (i.e. during the current CLI invocation) to avoid picking up
    a stale dir from a previous case.
    """
    outputs_dir = OUTPUT_DIR
    reproducer_dir, test_script_path, reproducer_module_path, expected_signal = (
        _find_actual_reproducer_path(outputs_dir, min_mtime=min_mtime)
    )
    if not reproducer_dir or not test_script_path:
        return None

    fallback_arch = normalize_target_arch(os.uname().machine)
    fallback_boot = (
        input_artifacts.get("boot_kernel_path")
        or input_artifacts.get("vmlinux_path", "")
    )
    # binaries_dir: if reproducer dir has executables (user-space trigger),
    # point binaries_dir at it so test_expert injects them into initramfs.
    binaries_dir = ""
    try:
        rd_path = Path(reproducer_dir)
        has_executable = any(
            p.is_file() and os.access(p, os.X_OK)
            for p in rd_path.iterdir()
            if p.name not in {"Makefile", "test.sh"}
        )
        if has_executable:
            binaries_dir = reproducer_dir
    except Exception:
        pass

    contract = KernelExpertOutput(
        status="ok",
        target_arch=fallback_arch,
        boot_kernel_path=fallback_boot,
        reproducer_dir=reproducer_dir,
        reproducer_module_path=reproducer_module_path,
        test_script_path=test_script_path,
        expected_signal=expected_signal or "Kernel panic",
        binaries_dir=binaries_dir,
        build_status="passed" if reproducer_module_path else "skipped",
        warnings=[
            "Contract recovered from partial output after CLI hit max_turns; "
            "reproducer files were written but KERNEL_CONTRACT was not emitted."
        ],
    )
    contract = _validate_kernel_contract_artifacts(contract)

    analysis_text = (
        f"## kernel_expert 分析（max_turns 后从已生成文件恢复）\n\n"
        f"CLI 达到 max_turns 上限，但已生成复现器文件。从 outputs/ 恢复 contract：\n"
        f"- reproducer_dir: {reproducer_dir}\n"
        f"- test_script_path: {test_script_path}\n"
        f"- reproducer_module_path: {reproducer_module_path or '(none — user-space reproducer)'}\n"
        f"- expected_signal: {contract.expected_signal}\n"
        f"- binaries_dir: {binaries_dir or '(none)'}\n"
    )
    return contract, analysis_text


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
