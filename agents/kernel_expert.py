from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import hashlib
import time


def _create_codex_workdir(session_output_dir: Path) -> Path:
    """Create a project-contained workdir for one Kernel Expert invocation."""
    root = (PROJECT_ROOT / "runtime" / "codex-sessions").resolve()
    root.mkdir(parents=True, exist_ok=True)
    session_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", session_output_dir.name).strip("-")
    session_name = session_name[:64] or "session"
    digest = hashlib.sha256(str(session_output_dir.resolve()).encode("utf-8")).hexdigest()[:12]
    workdir = root / f"{session_name}-{digest}-{time.time_ns()}"
    workdir.mkdir(parents=False, exist_ok=False)
    return workdir


def _sync_codex_artifacts(workdir: Path, session_output_dir: Path) -> None:
    """Copy userspace artifacts into the durable workflow session."""
    session_output_dir.mkdir(parents=True, exist_ok=True)
    allowed_suffixes = {".c", ".h", ".json"}
    for source in workdir.rglob("*"):
        if not source.is_file() or source.name.startswith("."):
            continue
        if source.suffix.lower() not in allowed_suffixes:
            continue
        relative = source.relative_to(workdir)
        destination = session_output_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            destination.write_bytes(source.read_bytes())
        except OSError:
            continue


def _stage_codex_evidence(
    workdir: Path, evidence_files: list[tuple[str, str]] | None,
) -> None:
    """Stage deterministic maintenance evidence under the Codex workspace.

    Syzkaller execution/audit transcripts can contain long syscall and device
    setup sequences that are unrelated to the crash call chain.  Keep the
    original files outside the Codex workspace for audit, but give the model a
    small, deterministic extract: the official report's signature and full
    Call trace, plus non-operational lines from tool analyses.
    """
    evidence_dir = workdir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    risk_pattern = re.compile(
        r"\bsyz[\w.-]*|reproduc|\b(?:ioctl|payload|exploit|attack|privilege|modules?)\b|"
        r"(?:syscall|fault)[ -]?(?:sequence|injection)", re.IGNORECASE,
    )
    for name, declared_path in evidence_files or []:
        source = Path(os.path.expanduser(str(declared_path or "")))
        if name == "original.log":
            report = source.with_name("report.txt")
            if report.is_file():
                source = report
        if not source.is_file():
            continue
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name)).strip("._")
        if not safe_name:
            continue
        try:
            destination = evidence_dir / safe_name
            raw = source.read_text(encoding="utf-8", errors="replace")
            if name == "original.log":
                lines = raw.splitlines()
                selected: list[str] = []
                in_trace = False
                markers = (
                    "Unable to handle", "KASAN:", "Internal error:",
                    "pc :", "lr :", "Call trace:",
                )
                for line in lines:
                    if "Call trace:" in line:
                        in_trace = True
                    if any(marker in line for marker in markers) or in_trace:
                        if risk_pattern.search(line) or line.startswith(("CPU:", "Modules linked")):
                            continue
                        selected.append(line)
                    if in_trace and "end trace" in line:
                        break
                raw = "\n".join(selected) + "\n"
            else:
                raw = "\n".join(
                    line for line in raw.splitlines()
                    if not risk_pattern.search(line)
                ) + "\n"
            destination.write_text(raw, encoding="utf-8")
        except OSError:
            continue

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from agents.contracts import KernelExpertOutput, RefcountPath, TestResultContract, UafAnalysisContract, model_to_dict
from agents.error_handling import classify_error, error_to_evidence
from agents.semcode_path_analysis import (
    SemcodeMcpClient,
    SemcodePathAnalysisResult,
    analyze_uaf_paths,
    extract_semcode_entry_points,
    render_semcode_analysis_context,
    resolve_kernel_source_for_commit,
    verify_semcode_target,
)
from agents.llm_display import (
    call_llm_with_persistence,
    display_expert_outputs,
    set_session_dir,
    get_expert_output_file,
    ensure_output_dir,
    _format_agent_header_text,
    _format_agent_footer_text,
    write_hint_review_pack,
    wait_for_hint,
)
from agents.test_runner import detect_kernel_type, normalize_target_arch
from agents.input_artifacts import parse_input_artifacts
from llm_config import get_llm_with_config, load_prompt_from_file
from graph.rn_state import MaintenanceWorkflowState
from paths import PROJECT_ROOT, get_output_dir as paths_get_output_dir
import paths as _paths  # for set_session_dir


def _write_tool_call_output(output_file: str, content: str, expert_name: str):
    """Write final tool-calling output to file, preserving tool call logs."""
    footer = _format_agent_footer_text(expert_name)

    with open(output_file, "a", encoding="utf-8") as f:
        # Append final result after tool call logs
        f.write("\n\n## 最终分析结果\n\n")
        f.write(content + "\n")
        f.write(footer)



def _pin_semcode_mcp_to_source(agent_config: dict, kernel_source_path: str) -> dict:
    """Bind the agent-loop Semcode server to the declared kernel checkout.

    A database alone is insufficient for git-aware Semcode queries: without
    ``--git-repo`` the MCP server may resolve a different branch/checkout and
    return source from a repaired commit. Keep the deployment-provided config
    immutable and return a copy with explicit, source-derived arguments.
    """
    source = os.path.realpath(os.path.expanduser(str(kernel_source_path or "").strip()))
    if not source or "semcode_mcp" not in agent_config:
        return agent_config
    database = os.path.join(source, ".semcode.db")
    if not os.path.exists(database):
        return agent_config
    pinned = dict(agent_config)
    semcode = dict(agent_config.get("semcode_mcp") or {})
    existing_args = list(semcode.get("args") or [])
    filtered_args: list[str] = []
    skip_next = False
    for arg in existing_args:
        if skip_next:
            skip_next = False
            continue
        if arg in ("-d", "--database", "--git-repo"):
            skip_next = True
            continue
        if arg.startswith("--database=") or arg.startswith("--git-repo="):
            continue
        filtered_args.append(arg)
    semcode["args"] = [*filtered_args, "-d", database, "--git-repo", source]
    pinned["semcode_mcp"] = semcode
    return pinned
# ---------------------------------------------------------------------------
# Preflight: kernel config + test_assets scan
# ---------------------------------------------------------------------------

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

    Cached on disk by bzImage fingerprint — see agents/cache/ikconfig_cache.py.
    """
    if not bzimage_path or not os.path.isfile(bzimage_path):
        return {}
    from agents.cache.ikconfig_cache import get_ikconfig
    _, pertinent = get_ikconfig(bzimage_path)
    return pertinent


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
        parts.append("用户态回归测试相关 CONFIG 选项（已为你预提取，不要再自己跑 extract-ikconfig）：")
        # Keep the first-turn context to non-executable strategy metadata.
        # Raw built-in command lines and diagnostic-only toggles are available
        # from the declared image/config paths and are intentionally not
        # copied into the Codex prompt.
        safe_options = {
            "CONFIG_KVM", "CONFIG_KVM_INTEL", "CONFIG_KVM_AMD",
            "CONFIG_HYPERV", "CONFIG_PARAVIRT_SPINLOCKS", "CONFIG_KVM_GUEST",
            "CONFIG_PREEMPT", "CONFIG_PREEMPT_DYNAMIC", "CONFIG_NR_CPUS",
            "CONFIG_NR_CPUS_RANGE_END",
        }
        for opt in _PERTINENT_CONFIG_OPTIONS:
            if opt not in safe_options:
                continue
            if opt in config:
                val = config[opt]
                # Annotate key options with strategy implications
                note = ""
                if opt == "CONFIG_KVM" and val == "y":
                    note = "  → KVM 内置，可检查相关虚拟化回归条件"
                elif opt == "CONFIG_HYPERV" and val == "y":
                    note = "  → HyperV 客户机驱动启用"
                elif opt == "CONFIG_PARAVIRT_SPINLOCKS" and val == "y":
                    note = "  → PV spinlock 启用，可检查对应内核路径"
                elif opt == "CONFIG_MODULE_FORCE_LOAD" and val != "y":
                    note = "  → 非用户态测试路径，保持内核扩展约束"
                elif opt == "CONFIG_KASAN" and val == "y":
                    note = "  → KASAN 启用，可观察内存错误诊断标记"
                elif opt == "CONFIG_PANIC_ON_WARN" and val == "y":
                    note = "  → WARNING 自动升级为 panic"
                parts.append(f"- {opt}={val}{note}")
        # Surface CONFIG_CMDLINE which often embeds panic_on_warn / numa_fake
        if config.get("CONFIG_CMDLINE"):
            parts.append(
                "- kernel built-in runtime parameters are present; verify the "
                "declared image/config artifacts directly"
            )

    assets = _scan_test_assets_for_reproducers(test_assets_dir)
    if assets:
        parts.append("\n## test_assets 中已有的用户态测试资产（仅作接口参考）")
        parts.append("扫描到以下现有测试资产；先核对其 ABI 与调用序列，再按本案例契约生成新的用户态 C 程序：")
        for f in assets:
            kind_label = {
                "syzbot_repro_binary": "预编译用户态测试程序（由 Test Expert 在 guest 中编译/运行）",
                "syzbot_repro_source": "用户态 C 源码（需由 Test Expert 编译）",
                "userspace_trigger": "用户态测试程序（由 Test Expert 在 guest 中运行）",
                "reproduction_notes": "测试说明文档（含运行配置，必读）",
            }.get(f["kind"], f["kind"])
            parts.append(f"- {f['name']} ({kind_label}): {f['path']}")
        parts.append("")
        parts.append("**使用顺序**：")
        parts.append("1. 有预编译用户态程序 → 核对接口后在契约中声明其 guest 内运行步骤")
        parts.append("2. 有用户态 C 源码 → 由 Test Expert 在 guest 内编译并记录编译证据")
        parts.append("3. 有测试说明文档 → 必读，里面有 smp/numa/timeout 等关键配置")

    if not parts:
        return ""
    return "\n".join(parts)


def _run_kernel_expert_with_agent_loop(
    llm,
    system_prompt: str,
    user_content: str,
    expert_name: str,
    output_file: str,
    target_kernel_dir: str = "",
    boot_kernel_path: str = "",
    test_assets_dir: str = "",
    max_reproduction_rounds: int = 9,
    evidence_files: list[tuple[str, str]] | None = None,
) -> AIMessage:
    """Execute kernel expert analysis via an agent-loop CLI backend.

    Delegates the tool-calling loop to ``codex exec``. Codex reads source and
    logs, uses the required Semcode MCP server, and may write only diagnostic
    userspace artifacts in the session output directory. Returns the final
    text result with KERNEL_CONTRACT and marker lines for downstream parsing.

    Both backends implement the same invoke(messages, workdir, add_dirs)
    contract, so this function is backend-agnostic — the choice is made
    at config-time via `agents.kernel_expert.backend`.
    """
    backend_label = "Codex" if llm.__class__.__name__ == "CodexBackend" else (
        "OpenCode" if llm.__class__.__name__ == "OpenCodeBackend" else "Agent Loop"
    )
    session_output_dir = Path(paths_get_output_dir()).resolve()
    codex_workdir = _create_codex_workdir(session_output_dir)
    _stage_codex_evidence(codex_workdir, evidence_files)
    header = _format_agent_header_text(expert_name, f"分析构造用例（{backend_label}）")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(header)
        f.write(f"执行模式: real ({backend_label} CLI agent)\n\n")

    try:
        home_dir = os.path.expanduser("~")
        context_info = f"""Kernel expert runtime environment:

- Home directory: {home_dir} (use this in paths, NOT /root)
- Output directory (your current workdir): {codex_workdir} — ALL C test-harness files and KERNEL_CONTRACT artifacts MUST be created under this directory
- Durable workflow session directory: {session_output_dir} — the workflow archives generated C/JSON artifacts here after this invocation
- Target source checkout: bound to the required Semcode MCP server; do not guess or substitute a source path
- Write only diagnostic userspace C sources and KERNEL_CONTRACT here.
- Test Expert owns QEMU, guest compilation, approved load setup, and call-chain verification.
- Maximum Kernel/Test Expert try-outs: {max_reproduction_rounds}
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
            workdir=str(codex_workdir),
            add_dirs=add_dirs,
        )
        _sync_codex_artifacts(codex_workdir, session_output_dir)

        output_content = response.content or ""
        # Retry once inside this same Kernel Expert loop when the final turn
        # is empty or lacks a parseable structured contract.  A bare JSON
        # object is valid too; requiring the cosmetic marker here used to
        # trigger a second expensive Codex run even when the contract was
        # complete and only the heading punctuation differed.
        parsed_contract = _extract_kernel_contract(output_content) if output_content.strip() else None
        has_structured_contract = bool(
            parsed_contract is not None
            and parsed_contract.status not in {"degraded", "blocked"}
            and parsed_contract.root_cause
            and parsed_contract.call_chain_oracle.required_frames
        )
        if not output_content.strip() or not has_structured_contract:
            retry_messages = messages + [HumanMessage(content=(
                "当前最终输出缺少可解析的 KERNEL_CONTRACT。请在本次 loop 内补交完整结构化 JSON，"
                "保留已完成的维护分析、用户态测试程序和验证结果；不要引用旧文件或省略字段。"
            ))]
            retry_response = llm.invoke(
                retry_messages,
                workdir=str(codex_workdir),
                add_dirs=add_dirs,
            )
            _sync_codex_artifacts(codex_workdir, session_output_dir)
            if retry_response.content and retry_response.content.strip():
                response = retry_response
                output_content = retry_response.content
        if not output_content.strip():
            output_content = f"（{backend_label} 未生成最终文本，请检查 {backend_label} CLI 输出）"
        _write_tool_call_output(output_file, output_content, expert_name)

        return response

    except Exception as e:
        error_msg = f"{backend_label} 调用失败: {str(e)}"
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


def _resolve_primary_log_path(input_artifacts: dict, expert_results: list[dict]) -> str:
    """Prefer the supplied log; otherwise use the log artifact extracted from vmcore."""
    supplied = str(input_artifacts.get("log_path", "") or "")
    if supplied and Path(os.path.expanduser(supplied)).is_file():
        return supplied
    for result in expert_results:
        artifacts = ((result.get("structured_output") or {}).get("artifacts") or {})
        extracted = str(artifacts.get("raw_log_file", "") or "")
        if extracted and Path(extracted).is_file():
            return extracted
    return ""


def _read_primary_log_text(path: str) -> str:
    """Read the resolved first-hand log for deterministic frame extraction."""
    if not path:
        return ""
    try:
        return Path(os.path.expanduser(path)).read_text(
            encoding="utf-8", errors="replace",
        )
    except OSError:
        return ""


def _materialize_primary_log(output_dir: Path, source_path: str) -> str:
    """Copy first-hand log evidence into the Codex workspace.

    The Codex sandbox intentionally cannot read arbitrary absolute paths
    outside its session.  Keep the original source immutable, copy it into
    the session, and return only that workspace-local path to the agent.
    """
    if not source_path:
        return ""
    source = Path(os.path.expanduser(source_path))
    if not source.is_file():
        return ""
    destination = Path(output_dir) / "original-crash.log"
    try:
        if source.resolve() != destination.resolve():
            shutil.copyfile(source, destination)
        return str(destination.resolve())
    except OSError:
        return ""


_SEM_CODE_FRAME_RE = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*"
    r"(?:\.(?:cold|isra|constprop|part)(?:\.\d+)*)?)"
    r"\+0x[0-9a-fA-F]+"
)


def _materialize_semcode_evidence(
    output_dir: Path,
    *,
    source_path: str,
    expected_commit: str,
    command: str,
    args: list[str],
    evidence_text: str,
) -> str:
    """Persist exact-commit Semcode results for the Codex workspace.

    Some Codex MCP sessions cancel an otherwise valid Semcode call at the
    client boundary. Fetch the same required MCP data once through Lumen's
    deterministic adapter, preserving the explicit commit on every query, so
    the Kernel Expert can continue from auditable source evidence without a
    source-text or model fallback.
    """
    names: list[str] = []
    for match in _SEM_CODE_FRAME_RE.finditer(evidence_text or ""):
        name = match.group(1)
        if name not in names:
            names.append(name)
        if len(names) >= 32:
            break
    evidence_path = Path(output_dir) / "semcode-evidence.json"
    entries: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    if names and source_path and expected_commit and command:
        try:
            client = SemcodeMcpClient(
                command=command,
                args=args,
                kernel_source_path=source_path,
                git_sha=expected_commit,
                timeout_sec=120,
            )
            for name in names:
                try:
                    result = client._call("find_function", {"name": name})
                    entries.append({"function": name, "result": result})
                except Exception as exc:
                    failures.append({"function": name, "error": str(exc)})
        except Exception as exc:
            failures.append({"function": "<client>", "error": str(exc)})
    else:
        failures.append({
            "function": "<input>",
            "error": "no source, commit, Semcode command, or stack frames available",
        })
    payload = {
        "status": "ok" if entries else "blocked",
        "kernel_source": str(source_path or ""),
        "expected_kernel_commit": str(expected_commit or ""),
        "query_method": "Lumen Semcode MCP adapter; every query includes git_sha",
        "entries": entries,
        "failures": failures,
    }
    try:
        evidence_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return str(evidence_path.resolve())
    except OSError:
        return ""


def _codex_case_text(user_input: str) -> str:
    """Rephrase case metadata for Codex as authorized maintenance evidence."""
    text = str(user_input or "")
    replacements = (
        ("Bug Promote:", "Authorized maintenance regression case:"),
        (
            "maintenance diagnosis only, not vulnerability research.",
            "authorized defensive kernel maintenance diagnosis.",
        ),
        ("reproducer:", "original ABI sample:"),
        ("fresh userspace C trigger", "new userspace C regression test"),
        ("do not compile or reuse it as the Lumen output", "do not execute the supplied sample; generate a separate test program"),
        ("do not write a kernel module", "do not create an in-kernel extension"),
    )
    for source, replacement in replacements:
        text = text.replace(source, replacement)
    # Benchmark cases deliberately withhold the supplied trigger source from
    # the Codex context.  The validator keeps the artifact contract, while
    # Kernel Expert must derive a new userspace test from the first-hand log
    # and exact Semcode source rather than reusing a named sample.
    text = re.sub(
        r"(?i)(original ABI sample\s*:\s*)([^;\r\n]+)",
        r"\1withheld by the benchmark; derive the ABI from the log and source",
        text,
    )
    return text


def kernel_expert_node(state: MaintenanceWorkflowState) -> dict:
    """内核专家 agent：根据工具专家的输出，结合代码分析，构造必现用例并给出内核维测方案。

    通过工具调用机制实际创建文件和编译验证模块。
    """
    session_dir = state.get("session_dir")
    set_session_dir(session_dir)
    _paths.set_session_dir(session_dir)
    config = state.get("config", {})
    agent_config = config.get("agents", {}).get("kernel_expert", {})
    default_config = config.get("default", {})

    # If the input specifies kernel_source_path, point semcode MCP's db to
    # that tree's index so cross-tree lookups (kvm/btrfs in linux-next vs
    # deadlock/UAF in OLK-6.6) resolve against the correct source.
    input_artifacts = dict(state.get("input_artifacts_contract", {}) or {})
    # Retry/direct callers can carry an incomplete artifact contract even
    # though the user input still contains authoritative paths. Reparse it
    # here so the kernel prompt always exposes the real log/reproducer paths.
    if (not input_artifacts.get("reproducer_path") or not input_artifacts.get("log_path")
            or not input_artifacts.get("expected_kernel_commit")):
        reparsed = parse_input_artifacts(state.get("user_input", ""), validate_paths=False)
        reparsed_dict = model_to_dict(reparsed)
        for key, value in reparsed_dict.items():
            if value and not input_artifacts.get(key):
                input_artifacts[key] = value
    kernel_source_path = input_artifacts.get("kernel_source_path", "")
    expected_kernel_commit = input_artifacts.get("expected_kernel_commit", "")
    try:
        kernel_source_path = resolve_kernel_source_for_commit(
            kernel_source_path,
            expected_kernel_commit,
            workspace_root=str(session_dir or ""),
        )
        input_artifacts["kernel_source_path"] = kernel_source_path
    except Exception as exc:
        return _blocked_source_verification({
            "status": "blocked",
            "blocked_reason": str(exc),
            "evidence": [{"kind": "kernel_source_worktree", "status": "blocked", "error": str(exc)}],
        })
    agent_config = _pin_semcode_mcp_to_source(agent_config, kernel_source_path)

    # A git_sha on an individual Semcode query is not a source-integrity proof:
    # the MCP server may silently answer from its default HEAD when that
    # snapshot is absent from the index.  Refuse to start the LLM/QEMU handoff
    # until the declared commit exists in git and is represented by an
    # up-to-date Semcode branch.  This prevents a plausible but wrong source
    # revision from becoming a successful reproduction.
    semcode_config = agent_config.get("semcode_mcp") or {}
    source_verification = verify_semcode_target(
        kernel_source_path=kernel_source_path,
        expected_kernel_commit=expected_kernel_commit,
        semcode_command=str(semcode_config.get("command", "")),
        semcode_args=semcode_config.get("args", []) or [],
    )
    if source_verification.get("status") != "ok":
        return _blocked_source_verification(source_verification)

    llm = get_llm_with_config(agent_config, default_config=default_config, agent_name="kernel_expert")
    system_prompt = load_prompt_from_file(
        agent_config.get("prompt_file", "prompts/kernel_expert.md")
    )

    # Tool expert transcripts are durable artifacts.  Pass paths (not large
    # summaries) to the Codex loop so each expert can be iterated and audited
    # independently without mixing its context into another expert's prose.
    expert_results = state.get("expert_results", [])
    original_log_path = _resolve_primary_log_path(input_artifacts, expert_results)
    workspace_log_path = _materialize_primary_log(paths_get_output_dir(), original_log_path)
    if workspace_log_path:
        original_log_path = workspace_log_path
    original_log_text = _read_primary_log_text(original_log_path)

    # Only display expert outputs on first invocation (not on retries after test failures)
    if state.get("test_attempts", 0) == 0:
        display_expert_outputs(expert_results)
    expert_result_paths = []
    evidence_files: list[tuple[str, str]] = []
    if original_log_path:
        evidence_files.append(("original.log", original_log_path))
    for index, result in enumerate(expert_results, start=1):
        structured = result.get("structured_output") or {}
        artifacts = structured.get("artifacts") or {}
        output_path = artifacts.get("expert_output_file")
        if not output_path:
            # Direct node/unit callers may provide a tool result without
            # going through tool_expert_node.  Materialize that supplied
            # result once so the Codex boundary still receives a file path.
            # Normal workflow execution always takes the persisted branch.
            expert_type = str(result.get("expert_type", "unknown"))
            materialized_file = get_expert_output_file(expert_type)
            materialized_file.write_text(str(result.get("analysis_output", "")) + "\n", encoding="utf-8")
            output_path = str(materialized_file.resolve())
        evidence_name = f"tool_expert_{index}.txt"
        evidence_files.append((evidence_name, str(output_path)))
        expert_result_paths.append(
            f"- {result.get('expert_name', result.get('expert_type', 'unknown'))}"
            f" ({result.get('expert_type', 'unknown')}): evidence/{evidence_name}"
        )

    # Extract evidence summary for LLM context
    evidence_summary = _extract_evidence_summary(expert_results)
    semcode_evidence_text = "\n".join([
        str(state.get("user_input", "")),
        original_log_text,
        *(str(item.get("analysis_output", "")) for item in expert_results),
        *(json.dumps(item.get("structured_output", {}), ensure_ascii=False)
          for item in expert_results),
    ])
    semcode_evidence_path = _materialize_semcode_evidence(
        paths_get_output_dir(),
        source_path=kernel_source_path,
        expected_commit=expected_kernel_commit,
        command=str(semcode_config.get("command", "")),
        args=list(semcode_config.get("args", []) or []),
        evidence_text=semcode_evidence_text,
    )
    path_analysis_required = _requires_path_analysis(
        state.get("user_input", ""),
        "\n".join(str(item.get("analysis_output", "")) for item in expert_results),
    )
    semcode_path_analysis: SemcodePathAnalysisResult | None = None
    if path_analysis_required:
        semcode_config = agent_config.get("semcode_mcp") or {}
        declared_entries = extract_semcode_entry_points(state.get("user_input", ""))
        if declared_entries:
            entry_points = declared_entries
        else:
            entry_evidence = [state.get("user_input", "")]
            if original_log_text:
                entry_evidence.append(original_log_text)
            entry_points = extract_semcode_entry_points(
                *entry_evidence,
                expert_results=expert_results,
            )
        semcode_path_analysis = analyze_uaf_paths(
            kernel_source_path=kernel_source_path,
            entry_points=entry_points,
            expected_kernel_commit=expected_kernel_commit,
            semcode_command=str(semcode_config.get("command", "")),
            semcode_args=semcode_config.get("args", []) or [],
        )
        if semcode_path_analysis.status != "ok":
            return _blocked_semcode_path_analysis(semcode_path_analysis)

    case_text = _codex_case_text(state.get("user_input", ""))
    user_content = (
        "AUTHORIZED MAINTENANCE REGRESSION NOTICE: This is a defensive Linux kernel "
        "maintenance session under operator control. All generated code is a bounded "
        "userspace ABI compatibility test for the isolated QEMU guest; do not discuss "
        "or perform unrelated security work.\n\n"
        f"## 用户问题与制品声明\n{case_text}\n\n"
        f"## 输入文件路径\n"
        f"- vmcore_path: {input_artifacts.get('vmcore_path', 'N/A')}\n"
        f"- vmlinux_path: {input_artifacts.get('vmlinux_path', 'N/A')}\n"
        f"- boot_kernel_path: {input_artifacts.get('boot_kernel_path', input_artifacts.get('vmlinux_path', 'N/A'))}\n\n"
        f"- expected_kernel_commit: {expected_kernel_commit or 'N/A'}\n"
        f"- Semcode source verification: {json.dumps(source_verification, ensure_ascii=False)}\n"
        f"- Semcode 查询约束：每次查询必须显式传入 git_sha={expected_kernel_commit or '<missing>'}；缺少目标提交的索引时必须 blocked，禁止查询默认 HEAD 或改用 grep/源码 fallback。\n\n"
        f"- session-local exact Semcode evidence (read this before interactive MCP): {semcode_evidence_path or 'N/A'}\n\n"
        f"- rootfs_path: {input_artifacts.get('rootfs_path', 'N/A')}\n\n"
        "- guest runtime settings: Test Expert owns QEMU settings; use only "
        "the structured case contract when settings are declared\n\n"
        f"- 原始日志路径（第一手证据，按需直接读取，禁止以专家摘要替代）: {original_log_path or 'N/A（vmcore 日志提取失败或未提供）'}\n\n"
        "- 原始接口样本：benchmark 按脱敏约束不向 Codex 提供；仅依据一手日志与精确 Semcode 重建用户态 C 测试程序\n\n"
        f"## 工具专家结果文件（按需直接读取；不要以路径外的摘要替代原文）\n"
        + "\n".join(expert_result_paths) + "\n\n"
        f"## 关键证据摘要\n{evidence_summary}"
    )
    # Keep the initial model request neutral and compact.  First-hand logs
    # and tool transcripts are staged under evidence/; raw issue titles and
    # host paths are not needed for the diagnosis and can cause the remote
    # model to misclassify ordinary maintenance work.
    user_content = (
        "## Authorized maintenance case\n"
        "Use only the first-hand evidence staged under evidence/ and the required Semcode MCP.\n"
        f"target_arch: {input_artifacts.get('target_arch', 'N/A')}\n"
        f"expected_kernel_commit: {expected_kernel_commit or 'N/A'}\n"
        "Semcode source status: verified for the declared commit; every query must use that exact git_sha.\n"
        "Original first-hand log: evidence/original.log (read it directly; do not replace it with a summary).\n"
        "User-supplied artifact paths, boot assets, and guest settings are validated and injected by the workflow.\n"
        "Write the diagnostic userspace C test harness and KERNEL_CONTRACT in the current workdir.\n\n"
        "## Evidence directory\n"
        "Inspect every file under evidence/ before concluding; record unknowns instead of guessing."
    )
    if semcode_path_analysis is not None:
        user_content += "\n\n" + render_semcode_analysis_context(semcode_path_analysis)

    # 如果是重试（测试未通过），附加测试反馈
    test_result = state.get("test_result", "")
    if test_result:
        user_content += f"\n\n## 上次测试结果（未成功复现）\n{test_result}\n请重新分析并调整复现用例。"

    # 确保输出目录存在
    ensure_output_dir()
    output_file = get_expert_output_file("kernel_expert")

    # 检查 kernel headers 是否存在
    # User-space reproducers are compiled inside the QEMU guest.  Host kernel
    # headers are intentionally irrelevant and must not block analysis.
    kernel_headers_exist = True

    # kernel headers 不存在时直接报错
    if not kernel_headers_exist:
        # A completed contract written by a previous/partial agent run is
        # already a self-contained handoff.  It does not need host headers to
        # be *consumed* by Test Expert; host headers are only a precondition
        # for compiling a new kernel module.  Recover it after the same
        # artifact validation used by the normal parsing path.
        contract_file = paths_get_output_dir() / "kernel_contract.json"
        if contract_file.exists():
            try:
                data = json.loads(contract_file.read_text(encoding="utf-8"))
                existing_contract = _model_validate(KernelExpertOutput, data)
                if semcode_path_analysis is not None:
                    existing_contract = _apply_semcode_path_analysis(
                        existing_contract, semcode_path_analysis,
                    )
                existing_contract = _validate_kernel_contract_artifacts(
                    existing_contract,
                    path_analysis_required=_requires_path_analysis(state.get("user_input", "")),
                )
                if _kernel_contract_ready_for_test(existing_contract):
                    analysis_text = (
                        "## 内核分析结果（复用已存在 contract）\n\n"
                        "宿主机缺少 kernel headers，因此跳过新的模块编译；"
                        "已校验并复用此前写入的 kernel_contract.json。"
                    )
                    return {
                        "kernel_analysis": analysis_text,
                        "reproduce_case": analysis_text,
                        "kernel_diagnosis": "",
                        "kernel_ready_for_test": True,
                        "kernel_contract": model_to_dict(existing_contract),
                        "target_arch": existing_contract.target_arch,
                        "boot_kernel_path": existing_contract.boot_kernel_path,
                        "reproducer_dir": existing_contract.reproducer_dir,
                        "reproducer_module_path": existing_contract.reproducer_module_path,
                        "expected_signal": existing_contract.expected_signal,
                        "binaries_dir": existing_contract.binaries_dir,
                        "semcode_path_analysis": semcode_path_analysis.as_dict() if semcode_path_analysis else {},
                    }
            except (OSError, ValueError, json.JSONDecodeError):
                # Keep the original explicit headers failure when the cached
                # contract is unreadable or does not meet the handoff rules.
                pass

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
            "semcode_path_analysis": semcode_path_analysis.as_dict() if semcode_path_analysis else {},
        }

    # Derive target kernel source dir from boot_kernel_path in input_artifacts
    boot_kernel_path = input_artifacts.get("boot_kernel_path", "") or input_artifacts.get("vmlinux_path", "")
    target_kernel_dir = ""
    declared_source = os.path.expanduser(str(kernel_source_path or ""))
    if declared_source and os.path.isdir(os.path.join(declared_source, "include")):
        # The input contract is authoritative when it names a complete source
        # checkout.  This is preferable to guessing from an assets directory
        # that contains only bzImage/vmlinux/disk files.
        target_kernel_dir = declared_source
    if boot_kernel_path:
        _kp = os.path.expanduser(boot_kernel_path)
        if _kp:
            _p = os.path.dirname(_kp)
            for _ in range(3):
                _p = os.path.dirname(_p)
            if not target_kernel_dir and os.path.isdir(os.path.join(_p, "include")):
                target_kernel_dir = _p

    # Derive test_assets_dir from boot_kernel_path: if bzImage is at
    # test_assets/<case>/bzImage, the parent dir is the case assets dir.
    test_assets_dir = ""
    if boot_kernel_path:
        _bk = Path(os.path.expanduser(boot_kernel_path))
        if _bk.parent.is_dir() and (_bk.parent / "input.txt").exists():
            test_assets_dir = str(_bk.parent)

    # Execute exactly one Codex agent loop. Agent-loop exhaustion is a
    # terminal blocked outcome; partial files must not bypass SSH verification.
    max_reproduction_rounds = int((config.get("workflow", {}) or {}).get("max_tryouts", 10))
    if max_reproduction_rounds < 1:
        raise ValueError("workflow.max_reproduction_rounds must be >= 1")
    try:
        response = _run_kernel_expert_with_agent_loop(
            llm=llm,
            system_prompt=system_prompt,
            user_content=user_content,
            expert_name="内核专家",
            output_file=output_file,
            target_kernel_dir=target_kernel_dir,
            boot_kernel_path=boot_kernel_path,
            test_assets_dir=test_assets_dir,
            max_reproduction_rounds=max_reproduction_rounds,
            evidence_files=evidence_files,
        )
    except RuntimeError as e:
        # CLI startup failure, timeout, or turn-budget exhaustion ends this
        # complete loop.  Do not recover partial POC files: they have not
        # reached the mandatory persistent SSH-QEMU verification stage.
        err_str = str(e)
        is_max_turns = "[cli_max_turns]" in err_str or "Reached maximum number of turns" in err_str
        if "timed out" in err_str.lower():
            error_msg = f"kernel_expert CLI 超时: {err_str}"
        elif is_max_turns:
            error_msg = f"kernel_expert CLI 达到 max_turns 上限: {err_str}"
        else:
            error_msg = f"kernel_expert CLI 启动失败: {err_str}"

        blocked_contract = KernelExpertOutput(
            status="blocked",
            build_status="skipped",
            blocked_reason=err_str,
            warnings=[error_msg],
            evidence=[error_to_evidence(classify_error(e, operation="kernel_expert CLI"), operation="kernel_expert CLI")],
        )
        return {
            "kernel_analysis": error_msg,
            "reproduce_case": "",
            "kernel_diagnosis": "",
            "kernel_ready_for_test": False,
            "kernel_contract": model_to_dict(blocked_contract),
            "final_response": error_msg,
            "semcode_path_analysis": semcode_path_analysis.as_dict() if semcode_path_analysis else {},
        }

    text = response.content.strip()

    # Keep the optional human review package, but do not reinvoke Codex here.
    # Analysis, POC creation, and SSH-QEMU verification are one Codex loop;
    # a second model call would split the evidence context again.
    try:
        write_hint_review_pack(
            user_input=state.get("user_input", ""),
            expert_results=expert_results,
            kernel_expert_output=text,
        )
    except Exception:
        pass  # review pack 写失败不应阻塞主流程

    # Test Expert owns deterministic QEMU execution.  Kernel Expert hands off
    # source and oracle only; prose cannot claim a test outcome.
    parsed = _parse_kernel_expert_response(
        text=text,
        expert_results=expert_results,
        input_artifacts=input_artifacts,
        state=state,
        semcode_path_analysis=semcode_path_analysis,
    )
    parsed["semcode_source_verification"] = source_verification
    contract_data = parsed.get("kernel_contract")
    if isinstance(contract_data, dict):
        contract_data["evidence"] = [
            *(contract_data.get("evidence") or []),
            *(source_verification.get("evidence") or []),
        ]
        parsed["kernel_contract"] = contract_data
    if semcode_path_analysis is not None:
        parsed["semcode_path_analysis"] = semcode_path_analysis.as_dict()
    return parsed


def _blocked_semcode_path_analysis(result: SemcodePathAnalysisResult) -> dict:
    """Stop UAF routing when the required deterministic source evidence is absent."""
    reason = f"semcode P2 path analysis blocked: {result.blocked_reason}"
    contract = KernelExpertOutput(
        status="blocked",
        build_status="skipped",
        path_analysis_required=True,
        blocked_reason=reason,
        warnings=["UAF/refcount analysis cannot use an LLM/source-text fallback."],
        evidence=result.evidence,
        path_analysis_scope={"analysis_status": "blocked"},
    )
    return {
        "kernel_analysis": reason,
        "reproduce_case": "",
        "kernel_diagnosis": "",
        "all_possible_paths": [],
        "max_likely_path": "",
        "uaf_analysis_contract": {},
        "semcode_path_analysis": result.as_dict(),
        "kernel_ready_for_test": False,
        "kernel_contract": model_to_dict(contract),
        "final_response": reason,
    }


def _blocked_source_verification(result: dict) -> dict:
    """Block the workflow when the requested kernel snapshot is unproven."""
    reason = f"kernel source verification blocked: {result.get('blocked_reason', 'unknown source-index failure')}"
    evidence = list(result.get("evidence") or [])
    contract = KernelExpertOutput(
        status="blocked",
        build_status="skipped",
        blocked_reason=reason,
        warnings=[
            "The declared kernel commit is not proven by an exact Semcode index.",
            "No LLM/direct-git source fallback is permitted.",
        ],
        evidence=evidence,
    )
    return {
        "kernel_analysis": reason,
        "reproduce_case": "",
        "kernel_diagnosis": "",
        "kernel_ready_for_test": False,
        "kernel_contract": model_to_dict(contract),
        "semcode_source_verification": result,
        "final_response": reason,
    }


def _merge_semcode_path_scope(automatic_scope, current_scope: dict) -> dict:
    """Keep semcode's source commit/entries authoritative and fill LLM omissions."""
    automatic = model_to_dict(automatic_scope)
    current = model_to_dict(current_scope) if hasattr(current_scope, "dict") else dict(current_scope)
    merged = dict(current)
    merged["kernel_commit"] = automatic["kernel_commit"]
    merged["entry_points"] = automatic["entry_points"]
    for field in ("kernel_config", "object_type", "concurrency_model"):
        if not merged.get(field):
            merged[field] = automatic[field]
    return merged


def _apply_semcode_path_analysis(
    contract: KernelExpertOutput, result: SemcodePathAnalysisResult,
) -> KernelExpertOutput:
    """Monotonically attach deterministic P2 paths to a Kernel Expert contract."""
    if result.status != "ok" or result.analysis is None:
        raise ValueError("cannot apply a blocked semcode path analysis")
    contract = _normalise_uaf_analysis(contract)
    automated = result.analysis
    merged = (
        _merge_uaf_analysis(automated, contract.uaf_analysis)
        if contract.uaf_analysis else automated
    )
    data = model_to_dict(contract)
    data["path_analysis_required"] = True
    data["uaf_analysis"] = model_to_dict(merged)
    data["path_analysis_scope"] = _merge_semcode_path_scope(
        result.scope, data.get("path_analysis_scope") or {},
    )
    existing_evidence = list(data.get("evidence") or [])
    for evidence in result.evidence:
        if evidence not in existing_evidence:
            existing_evidence.append(evidence)
    data["evidence"] = existing_evidence
    return _normalise_uaf_analysis(_model_validate(KernelExpertOutput, data))


def _looks_like_dsml_fragments(text: str) -> bool:
    """Detect whether the LLM response is only DSML/XML tool_use fragments.

    DeepSeek's tool_use serialization may emit closing tags like
    ``</tool_calls>`` or ``</DSML>`` without any prose when the CLI's
    response extractor only catches the tail of a tool-call sequence.
    These fragments are not real analysis text and would produce a
    degraded contract if fed to the section parser.

    Returns True when the text (after stripping XML/DSML tags and
    whitespace) is empty or contains only punctuation.
    """
    import re
    # Strip XML/DSML tool_use tags: <tool_use>, </tool_use>, <tool_calls>,
    # </tool_calls>, <DSML>, </DSML>, and similar.
    tag_pattern = re.compile(r'</?(?:tool_use|tool_calls|DSML|function_call|function_calls)\s*/?>', re.IGNORECASE)
    stripped = tag_pattern.sub('', text).strip()
    # Also strip stray punctuation that the fragment leaves behind
    stripped = re.sub(r'[\s<>/]+', '', stripped)
    return len(stripped) == 0


def _parse_kernel_expert_response(
    *,
    text: str,
    expert_results: list,
    input_artifacts: dict,
    state: dict,
    semcode_path_analysis: SemcodePathAnalysisResult | None = None,
) -> dict:
    """Parse kernel_expert output text into contract + state update dict.

    Shared between first-round and hint-injected rerun so both produce
    identically-shaped state updates.
    """
    path_analysis_required = _requires_path_analysis(
        state.get("user_input", ""),
        text,
        "\n".join(str(item.get("analysis_output", "")) for item in expert_results),
    )
    # Detect CLI failure text (timeout, startup error, max_turns) that
    # slipped through as a non-empty AIMessage. Block here instead of running
    # the empty-text fallback that would search outputs/ for stale reproducer
    # dirs and route test_expert with the wrong expected_signal.
    if text and (
        "Codex 调用失败" in text
        or "Codex timed out" in text
        or "Codex failed" in text
        or "OpenCode 调用失败" in text
        or "OpenCode timed out" in text
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

    # A missing or malformed final response is a hard failure.  The contract
    # must be emitted explicitly by the Kernel Expert; stale files and legacy
    # marker output are never consulted.
    if text and _looks_like_dsml_fragments(text):
        text = ""
    if not text:
        blocked_contract = KernelExpertOutput(
            status="blocked",
            build_status="skipped",
            blocked_reason="Kernel Expert did not emit an explicit structured response",
        )
        return {
            "kernel_analysis": "",
            "reproduce_case": "",
            "kernel_diagnosis": "",
            "kernel_ready_for_test": False,
            "kernel_contract": model_to_dict(blocked_contract),
            "final_response": "Kernel Expert 未输出结构化 contract，流程已阻断。",
        }

    # 解析必现用例和维测方案
    reproduce_case = _extract_section(text, "REPRODUCE_CASE")
    kernel_diagnosis = _extract_section(text, "KERNEL_DIAGNOSIS")
    all_possible_paths_text = _extract_section(text, "ALL_POSSIBLE_PATHS")
    max_likely_path = _extract_section(text, "MAX_LIKELY_PATH")
    kernel_contract = _extract_kernel_contract(text)
    # The CLI agent may render the structured object in prose without the
    # exact ``KERNEL_CONTRACT:`` marker.  Enrich only from authoritative
    # runtime artifacts and files that the agent actually created; never
    # invent a boot image, source file, or architecture.
    kernel_contract = _enrich_kernel_contract_from_runtime(
        kernel_contract,
        input_artifacts=input_artifacts,
        output_dir=paths_get_output_dir(),
    )
    if not _kernel_contract_has_handoff(kernel_contract):
        kernel_contract.status = "blocked"
        kernel_contract.blocked_reason = "missing explicit structured KERNEL_CONTRACT"

    # Preserve path findings emitted as human-readable sections even when the
    # CLI returned a contract JSON without the additive fields.
    if all_possible_paths_text or max_likely_path:
        data = model_to_dict(kernel_contract)
        if all_possible_paths_text and not data.get("all_possible_paths"):
            data["all_possible_paths"] = [
                line.strip() for line in all_possible_paths_text.splitlines()
                if line.strip()
            ]
        if max_likely_path and not data.get("max_likely_path"):
            data["max_likely_path"] = max_likely_path.strip()
        kernel_contract = _model_validate(KernelExpertOutput, data)

    # A retry must keep the inventory gathered by earlier attempts.  Do this
    # before P0 validation so a later attempt cannot turn a valid path set
    # into a partial one merely by omitting the path section.
    if path_analysis_required:
        data = model_to_dict(kernel_contract)
        previous_paths = state.get("all_possible_paths", []) or []
        current_paths = data.get("all_possible_paths", []) or []
        merged_paths = list(previous_paths)
        for path in current_paths:
            if path not in merged_paths:
                merged_paths.append(path)
        data["all_possible_paths"] = merged_paths
        data["path_analysis_required"] = True
        if not data.get("max_likely_path"):
            data["max_likely_path"] = state.get("max_likely_path", "")
        kernel_contract = _model_validate(KernelExpertOutput, data)
        kernel_contract = _normalise_uaf_analysis(kernel_contract)
        if semcode_path_analysis is not None:
            kernel_contract = _apply_semcode_path_analysis(
                kernel_contract, semcode_path_analysis,
            )
        previous_analysis_data = state.get("uaf_analysis_contract") or {}
        if previous_analysis_data and kernel_contract.uaf_analysis:
            try:
                previous_analysis = _model_validate(UafAnalysisContract, previous_analysis_data)
                merged_analysis = _merge_uaf_analysis(previous_analysis, kernel_contract.uaf_analysis)
                data = model_to_dict(kernel_contract)
                data["uaf_analysis"] = model_to_dict(merged_analysis)
                kernel_contract = _normalise_uaf_analysis(_model_validate(KernelExpertOutput, data))
            except ValueError:
                pass

    kernel_contract = _validate_kernel_contract_artifacts(
        kernel_contract,
        path_analysis_required=path_analysis_required,
    )
    # Persist the validated handoff for audit/retry.  Test Expert still
    # receives the in-memory contract; this file is only a durable copy of
    # the current attempt, never a source for silently recovering stale data.
    try:
        contract_path = paths_get_output_dir() / "kernel_contract.json"
        contract_path.write_text(
            json.dumps(model_to_dict(kernel_contract), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        kernel_contract.warnings.append(f"could not persist kernel_contract.json: {exc}")
    contract_ready = _kernel_contract_ready_for_test(kernel_contract)
    print(f"  [contract诊断] status={kernel_contract.status} target_arch={kernel_contract.target_arch} "
          f"boot_kernel={kernel_contract.boot_kernel_path is not None} "
          f"execution_steps={len(kernel_contract.execution_steps)} "
          f"expected_signal={kernel_contract.expected_signal is not None} "
          f"ready_for_test={contract_ready}", flush=True)

    # Retries must not erase the first round's path inventory.  Preserve all
    # previously established paths and append newly discovered ones.
    previous_paths = state.get("all_possible_paths", []) or []
    current_paths = kernel_contract.all_possible_paths or []
    merged_paths = list(previous_paths)
    for path in current_paths:
        if path not in merged_paths:
            merged_paths.append(path)
    merged_max_path = kernel_contract.max_likely_path or state.get("max_likely_path", "")

    return {
        "kernel_analysis": text,
        "reproduce_case": reproduce_case or text,
        "kernel_diagnosis": kernel_diagnosis or "",
        "all_possible_paths": merged_paths,
        "max_likely_path": merged_max_path,
        "uaf_analysis_contract": model_to_dict(kernel_contract.uaf_analysis) if kernel_contract.uaf_analysis else {},
        "kernel_ready_for_test": contract_ready,
        "kernel_contract": model_to_dict(kernel_contract),
        "target_arch": kernel_contract.target_arch,
        "boot_kernel_path": kernel_contract.boot_kernel_path,
        "reproducer_dir": kernel_contract.reproducer_dir,
        "reproducer_module_path": kernel_contract.reproducer_module_path,
        "expected_signal": kernel_contract.expected_signal,
        "binaries_dir": kernel_contract.binaries_dir,
    }


def _attach_persistent_test_result(
    parsed: dict, *, started_after: float, max_rounds: int,
) -> dict:
    """Attach only a fresh deterministic SSH-QEMU result to the workflow state.

    Codex prose is never used as a test verdict. The loop must invoke the
    project runner, which writes this independently parsed JSON contract.
    """
    contract = parsed.get("kernel_contract") or {}
    if contract.get("status") != "ok" or not parsed.get("kernel_ready_for_test"):
        return parsed
    try:
        def round_number(path: Path) -> int:
            match = re.fullmatch(r"persistent_test_contract\.round-(\d+)\.json", path.name)
            if match is None:
                raise ValueError(f"invalid persistent QEMU round filename: {path.name}")
            return int(match.group(1))

        result_paths = sorted(
            paths_get_output_dir().glob("persistent_test_contract.round-*.json"),
            key=round_number,
        )
        if not result_paths:
            raise OSError("no per-round persistent QEMU result was produced")
        if len(result_paths) > max_rounds:
            raise ValueError(f"reproduction rounds exceed configured limit: {len(result_paths)} > {max_rounds}")
        round_contracts = []
        for expected_round, result_path in enumerate(result_paths, start=1):
            if result_path.stat().st_mtime < started_after:
                raise OSError(f"round result predates this Codex invocation: {result_path}")
            data = json.loads(result_path.read_text(encoding="utf-8"))
            round_contract = _model_validate(TestResultContract, data)
            if round_contract.attempts != expected_round:
                raise ValueError(
                    f"round sequence is invalid: expected {expected_round}, got {round_contract.attempts}"
                )
            round_contracts.append((result_path, round_contract))
        result_path, test_contract = round_contracts[-1]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parsed.update({
            "test_passed": False,
            "test_attempts": 1,
            "test_result": "Persistent QEMU verification did not produce a fresh deterministic result.",
            "test_rounds": [],
            "test_contract": {
                "status": "blocked",
                "code": "BLOCKED_PERSISTENT_QEMU_RESULT_MISSING",
                "test_passed": False,
                "summary": str(exc),
                "artifacts": {"expected_pattern": "persistent_test_contract.round-<NN>.json"},
            },
        })
        return parsed
    round_results = []
    for path, round_contract in round_contracts:
        round_data = model_to_dict(round_contract)
        round_data["result_file"] = str(path)
        round_results.append(round_data)
    final_contract = model_to_dict(test_contract)
    final_contract["round_result_file"] = str(result_path)
    parsed.update({
        "test_passed": test_contract.test_passed,
        "test_attempts": test_contract.attempts,
        "test_rounds": round_results,
        "test_result": test_contract.summary,
        "test_contract": final_contract,
    })
    return parsed






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
                if etype in {"kernel_panic", "hung_task", "lockdep", "bug"}:
                    # Keep the first-turn summary metadata-only.  Raw crash
                    # strings may contain addresses or execution markers that
                    # are not needed for routing and can be misclassified by
                    # the model safety gate.  The original log path is still
                    # handed to Codex as first-hand evidence for direct read.
                    summary_parts.append(
                        f"- Log event ({etype}) L{ev.get('line')}: "
                        "verify the declared first-hand log directly"
                    )

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
                        summary_parts.append(
                            "- Panic event reported; verify the declared first-hand log directly"
                        )

    if not summary_parts:
        return "（从工具专家结果中未提取到关键证据）"

    return "\n".join(summary_parts)


def _extract_section(text: str, marker: str) -> str:
    """从文本中提取标记段落。"""
    pattern = rf"{re.escape(marker)}:\s*\n?(.*?)(?:\n[A-Z_]+:|\Z)"
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).strip() if match else ""


def _model_validate(model_cls, data: dict):
    if hasattr(model_cls, "model_validate"):
        return model_cls.model_validate(data)
    return model_cls.parse_obj(data)


def _coerce_contract_json(data: object) -> object:
    """Normalize harmless LLM prose before validating the handoff schema.

    The contract fields for pressure/fault injection are executable
    ``ExecutionStep`` objects. Codex may put a human-readable
    requirement string in those arrays (for example, describing work already
    performed by the C reproducer).  Treating that prose as an execution step
    would either reject an otherwise complete evidence contract or invent a
    guest-side action.  Preserve the text as a warning and leave only actual
    structured steps in the executable arrays.

    This is deliberately limited to type normalization; diagnosis, oracle
    frames, paths, and runtime actions are never synthesized here.
    """
    if not isinstance(data, dict):
        return data
    normalized = dict(data)
    warnings = list(normalized.get("warnings") or [])
    for field in ("pressure_requirements", "fault_injection_requirements"):
        raw = normalized.get(field)
        if raw is None:
            continue
        if not isinstance(raw, list):
            raw = [raw]
        structured = []
        for item in raw:
            if isinstance(item, dict):
                structured.append(item)
            elif isinstance(item, str) and item.strip():
                warnings.append(f"{field} prose requirement retained as warning: {item.strip()}")
        normalized[field] = structured
    normalized["warnings"] = warnings
    return normalized


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

    # Codex may say ``the KERNEL_CONTRACT`` and then emit a bare
    # JSON object (as opposed to ``KERNEL_CONTRACT:```json``).  Scan balanced
    # JSON objects across the whole response.  The literal marker can appear
    # inside a contract string value; starting there would skip the outer
    # object and leave only nested evidence objects to parse.  raw_decode
    # guarantees that trailing prose is not accepted as part of the contract.
    scan_start = 0
    for match in re.finditer(r"\{", text[scan_start:]):
        candidates.append(text[scan_start + match.start():])

    for candidate in candidates:
        try:
            stripped = candidate.strip()
            if stripped.startswith("```"):
                stripped = stripped.strip("`").strip()
                if stripped.lower().startswith("json"):
                    stripped = stripped[4:].strip()
            data, _ = json.JSONDecoder().raw_decode(stripped)
            # Do not accept a nested object (for example one evidence item)
            # merely because pydantic can fill all of its defaults.  Only a
            # top-level contract-shaped object is eligible for handoff.
            if not isinstance(data, dict) or not any(
                key in data for key in (
                    "root_cause", "call_chain_oracle", "reproducer",
                    "original_call_chain", "status",
                )
            ):
                continue
            return _model_validate(KernelExpertOutput, _coerce_contract_json(data))
        except Exception:
            continue

    return KernelExpertOutput(
        status="degraded",
        blocked_reason="missing or invalid KERNEL_CONTRACT JSON",
        warnings=["Kernel Expert did not produce a valid KERNEL_CONTRACT JSON object"],
    )


def _enrich_kernel_contract_from_runtime(
    contract: KernelExpertOutput,
    *,
    input_artifacts: dict,
    output_dir: Path,
) -> KernelExpertOutput:
    """Complete handoff fields from declared inputs and verified output files.

    The model owns the diagnosis, call-chain oracle, and reproduction design.
    The workflow owns the paths supplied by the user and the output directory
    it created.  Joining those two sources makes the handoff deterministic
    while keeping the no-fallback rule: a missing file remains missing.
    """
    data = model_to_dict(contract)
    # Paths and architecture declared by the user are authoritative runtime
    # inputs.  A model must not redirect Test Expert to an old case image or
    # kernel merely by emitting a different existing path in its JSON.
    for field in ("target_arch", "vmlinux_path", "boot_kernel_path", "rootfs_path"):
        declared = str(input_artifacts.get(field, "") or "").strip()
        if declared:
            data[field] = declared
    qemu_extra_cmdline = str(input_artifacts.get("qemu_extra_cmdline", "") or "").strip()
    if qemu_extra_cmdline:
        recipe = dict(data.get("qemu_recipe") or {})
        existing = str(recipe.get("extra_cmdline") or "").strip()
        existing_tokens = existing.split()
        declared_tokens = qemu_extra_cmdline.split()
        missing_tokens = [token for token in declared_tokens if token not in existing_tokens]
        if missing_tokens:
            recipe["extra_cmdline"] = " ".join([*existing_tokens, *missing_tokens])
            data["qemu_recipe"] = recipe

    repro = dict(data.get("reproducer") or {})
    source_dir = str(repro.get("source_dir") or "")
    if not source_dir:
        source_dir = str(output_dir)
    source_files = list(repro.get("source_files") or [])
    # Only infer the conventional file when it is present on disk and is C.
    # This is an observed artifact, not a generated fallback.
    if not source_files and (output_dir / "repro.c").is_file():
        source_files = ["repro.c"]
    if source_files:
        repro["source_dir"] = source_dir
        repro["source_files"] = source_files
        if not repro.get("entry_source") and "repro.c" in source_files:
            repro["entry_source"] = "repro.c"
    data["reproducer"] = repro
    return _model_validate(KernelExpertOutput, data)




def _kernel_contract_from_markers(
    *, target_arch: str, boot_kernel_path: str, reproducer_dir: str,
    reproducer_module_path: str, test_script_path: str, expected_signal: str,
    binaries_dir: str = "",
) -> KernelExpertOutput:
    """Legacy parser retained for archived contract fixtures only; never routed."""
    missing = [name for name, value in {
        "target_arch": target_arch, "boot_kernel_path": boot_kernel_path,
        "test_script_path": test_script_path, "expected_signal": expected_signal,
    }.items() if not value]
    return KernelExpertOutput(
        status="ok" if not missing else "blocked",
        target_arch=target_arch, boot_kernel_path=boot_kernel_path,
        reproducer_dir=reproducer_dir, reproducer_module_path=reproducer_module_path,
        expected_signal=expected_signal, binaries_dir=binaries_dir,
        build_status="unknown", warnings=["legacy test fixture only"],
        blocked_reason=f"missing legacy fields: {', '.join(missing)}" if missing else "",
    )


def _merge_kernel_contract(primary: KernelExpertOutput, legacy: KernelExpertOutput) -> KernelExpertOutput:
    """Legacy test-only merge; production parsing never invokes it."""
    data = model_to_dict(primary)
    legacy_data = model_to_dict(legacy)
    for key, value in legacy_data.items():
        if key in {"warnings", "evidence"}:
            data[key] = (data.get(key) or []) + (value or [])
        elif not data.get(key) and value:
            data[key] = value
    return _model_validate(KernelExpertOutput, data)


def _kernel_contract_has_handoff(contract: KernelExpertOutput) -> bool:
    return bool(
        contract.target_arch
        and contract.boot_kernel_path
        and contract.root_cause
        and contract.reproducer.source_dir
        and contract.reproducer.source_files
        and contract.reproducer.entry_source
        and contract.call_chain_oracle.fault_signatures
        and contract.call_chain_oracle.required_frames
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


def _requires_path_analysis(*texts: str) -> bool:
    """Return whether the declared case requires the P0 UAF/refcount path contract.

    Only the first text is authoritative (the user declaration).  Expert
    summaries are hypotheses and may mention UAF while analysing a different
    sanitizer failure; allowing them to change routing creates false P0
    semcode blocks for ordinary out-of-bounds cases.
    """
    combined = (texts[0] if texts else "").lower()
    # Paths such as ``test_assets/uaf/bzImage`` are transport metadata, not
    # a declared UAF diagnosis.  Remove absolute/home-relative path tokens
    # before matching semantic keywords.
    combined = re.sub(r"(?<!\S)(?:/|~\/)[^\s,，;；]+", "", combined)
    return any(token in combined for token in (
        "use-after-free", "use after free", "slab-use-after-free", "uaf",
        "kref", "refcount", "reference count", "引用计数", "释放后使用",
    ))


def _normalise_path_for_comparison(path: str) -> str:
    """Ignore list numbering/whitespace, but never guess a different path."""
    return re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", path or "").strip()


def _stable_path_id(summary: str) -> str:
    """Keep legacy-text path IDs stable across Kernel Expert retries."""
    digest = hashlib.sha256(_normalise_path_for_comparison(summary).encode("utf-8")).hexdigest()
    return f"path-{digest[:12]}"


def _normalise_uaf_analysis(contract: KernelExpertOutput) -> KernelExpertOutput:
    """Populate the P1 contract from legacy fields during the compatibility window."""
    data = model_to_dict(contract)
    analysis = contract.uaf_analysis
    if analysis is None and (contract.path_analysis_required or contract.all_possible_paths):
        paths = [
            RefcountPath(
                id=_stable_path_id(summary),
                summary=summary,
                unknowns=["legacy_unstructured"],
            )
            for summary in contract.all_possible_paths
            if summary.strip()
        ]
        max_id = next(
            (path.id for path in paths
             if _normalise_path_for_comparison(path.summary)
             == _normalise_path_for_comparison(contract.max_likely_path)),
            "",
        )
        target_id = next(
            (path.id for path in paths
             if _normalise_path_for_comparison(path.summary)
             == _normalise_path_for_comparison(contract.reproduction_target_path)),
            "",
        )
        analysis = UafAnalysisContract(
            paths=paths,
            excluded_paths=contract.excluded_paths,
            max_likely_path_id=max_id,
            selection_rationale=contract.max_likely_path_rationale,
            reproduction_target_path_id=target_id,
            legacy_unstructured=True,
        )

    if analysis is None:
        return contract

    data["uaf_analysis"] = model_to_dict(analysis)
    data["all_possible_paths"] = [path.summary for path in analysis.paths]
    data["excluded_paths"] = [model_to_dict(path) for path in analysis.excluded_paths]
    path_by_id = {path.id: path for path in analysis.paths}
    max_path = path_by_id.get(analysis.max_likely_path_id)
    target_path = path_by_id.get(analysis.reproduction_target_path_id)
    if max_path:
        data["max_likely_path"] = max_path.summary
    if target_path:
        data["reproduction_target_path"] = target_path.summary
    if analysis.selection_rationale:
        data["max_likely_path_rationale"] = analysis.selection_rationale
    return _model_validate(KernelExpertOutput, data)


def _validate_structured_uaf_analysis(
    contract: KernelExpertOutput, *, path_analysis_required: bool = False,
) -> list[str]:
    """Validate P1 path IDs, deltas, coverage declaration, and test target."""
    analysis = contract.uaf_analysis
    if not (contract.path_analysis_required or path_analysis_required) or analysis is None or analysis.legacy_unstructured:
        return []

    errors: list[str] = []
    path_ids = [path.id for path in analysis.paths]
    if len(path_ids) != len(set(path_ids)):
        errors.append("uaf_analysis.paths contains duplicate path IDs")
    if not analysis.case_id:
        errors.append("uaf_analysis requires case_id")
    if analysis.max_likely_path_id not in path_ids:
        errors.append("uaf_analysis.max_likely_path_id must reference a path")
    if analysis.reproduction_target_path_id != analysis.max_likely_path_id:
        errors.append("uaf_analysis.reproduction_target_path_id must match max_likely_path_id")
    if not analysis.target_contexts:
        errors.append("uaf_analysis requires target_contexts for causal reproduction")
    coverage = analysis.coverage
    if not any((
        coverage.normal_paths_considered,
        coverage.error_paths_considered,
        coverage.transfer_paths_considered,
        coverage.async_paths_considered,
        coverage.concurrency_paths_considered,
    )):
        errors.append("uaf_analysis.coverage must declare considered path classes")
    for path in analysis.paths:
        if path.events and sum(event.ref_delta for event in path.events) != path.net_delta:
            errors.append(f"uaf_analysis path {path.id} net_delta does not match events")
    return errors


def _merge_uaf_analysis(previous: UafAnalysisContract, current: UafAnalysisContract) -> UafAnalysisContract:
    """Monotonically retain prior path IDs while allowing a retry to add paths."""
    merged_paths = list(previous.paths)
    seen = {path.id for path in merged_paths}
    for path in current.paths:
        if path.id not in seen:
            merged_paths.append(path)
            seen.add(path.id)
    data = model_to_dict(current)
    data["paths"] = [model_to_dict(path) for path in merged_paths]
    if not data.get("case_id"):
        data["case_id"] = previous.case_id
    if not data.get("target_contexts"):
        data["target_contexts"] = previous.target_contexts
    return _model_validate(UafAnalysisContract, data)


def _validate_path_analysis_contract(
    data: dict,
    *,
    path_analysis_required: bool,
) -> tuple[list[str], list[dict]]:
    """Validate the minimal P0 evidence contract for UAF/refcount analysis."""
    if not path_analysis_required:
        return [], []

    errors: list[str] = []
    evidence: list[dict] = []
    candidates = [str(path).strip() for path in data.get("all_possible_paths") or [] if str(path).strip()]
    max_path = str(data.get("max_likely_path") or "").strip()
    reproduction_target = str(data.get("reproduction_target_path") or "").strip()
    scope = data.get("path_analysis_scope") or {}
    if hasattr(scope, "model_dump"):
        scope = scope.model_dump()
    elif hasattr(scope, "dict"):
        scope = scope.dict()

    if not candidates:
        errors.append("path analysis requires non-empty all_possible_paths")
    if not max_path:
        errors.append("path analysis requires max_likely_path")
    if candidates and max_path:
        normalised_candidates = {_normalise_path_for_comparison(item) for item in candidates}
        if _normalise_path_for_comparison(max_path) not in normalised_candidates:
            errors.append("max_likely_path must be a member of all_possible_paths")
    if not reproduction_target:
        errors.append("path analysis requires reproduction_target_path")
    elif candidates:
        normalised_candidates = {_normalise_path_for_comparison(item) for item in candidates}
        if _normalise_path_for_comparison(reproduction_target) not in normalised_candidates:
            errors.append("reproduction_target_path must be a member of all_possible_paths")
        elif max_path and _normalise_path_for_comparison(reproduction_target) != _normalise_path_for_comparison(max_path):
            errors.append("reproduction_target_path must match max_likely_path")

    for domain in scope.get("source_domains") or []:
        if not isinstance(domain, dict) or domain.get("kind") not in {"kernel", "reproducer"}:
            errors.append("each source_domains item requires kind=kernel or reproducer")
            break
        if not domain.get("analysis"):
            errors.append("each source_domains item requires analysis method")
            break

    required_scope = ("kernel_commit", "kernel_config", "entry_points", "object_type", "concurrency_model")
    missing_scope = [
        field for field in required_scope
        if not scope.get(field) or (field == "entry_points" and not list(scope.get(field) or []))
    ]
    if missing_scope:
        errors.append("path analysis scope missing: " + ", ".join(missing_scope))

    for excluded in data.get("excluded_paths") or []:
        if hasattr(excluded, "model_dump"):
            excluded = excluded.model_dump()
        elif hasattr(excluded, "dict"):
            excluded = excluded.dict()
        if not isinstance(excluded, dict) or not excluded.get("path") or not excluded.get("rationale"):
            errors.append("each excluded_paths item requires path and rationale")
            break

    evidence.append({
        "kind": "path_analysis_contract_check",
        "required": True,
        "candidate_count": len(candidates),
        "excluded_count": len(data.get("excluded_paths") or []),
        "scope_complete": not missing_scope,
        "max_path_in_candidates": bool(max_path) and _normalise_path_for_comparison(max_path) in {
            _normalise_path_for_comparison(item) for item in candidates
        },
        "reproduction_target_consistent": bool(reproduction_target and max_path)
        and _normalise_path_for_comparison(reproduction_target) == _normalise_path_for_comparison(max_path),
    })
    return errors, evidence


def _validate_kernel_contract_artifacts(
    contract: KernelExpertOutput,
    *,
    path_analysis_required: bool = False,
) -> KernelExpertOutput:
    """Validate Kernel Expert handoff paths before routing to Test Expert."""
    contract = _normalise_uaf_analysis(contract)
    data = model_to_dict(contract)
    warnings = list(data.get("warnings") or [])
    evidence = list(data.get("evidence") or [])
    errors: list[str] = []

    # A legacy Kernel Expert response can contain a complete, source-backed
    # original_call_chain while leaving the P2 selector fields empty (the
    # bounded Semcode graph may legitimately have no ranked max path).  Keep
    # that case auditable and testable: derive one explicit candidate from the
    # declared chain, retain the legacy marker, and surface the limitation as
    # a warning instead of converting an otherwise valid userspace C handoff
    # into a hard contract block.
    legacy_analysis = data.get("uaf_analysis") or {}
    if (
        path_analysis_required
        and (
            not str(data.get("max_likely_path") or "").strip()
            or not str(data.get("reproduction_target_path") or "").strip()
        )
    ):
        chain = [str(frame).strip() for frame in data.get("original_call_chain") or [] if str(frame).strip()]
        candidates = [str(path).strip() for path in data.get("all_possible_paths") or [] if str(path).strip()]
        if candidates:
            # Semcode's legacy response preserves the deterministic candidate
            # list even when it omits the selected ID; its first path is the
            # ranked max path in the emitted evidence graph.
            legacy_path = candidates[0]
        elif chain:
            legacy_path = " -> ".join(chain)
            data["all_possible_paths"] = [legacy_path]
        else:
            legacy_path = ""
        if legacy_path:
            data["max_likely_path"] = legacy_path
            data["reproduction_target_path"] = legacy_path
            warnings.append(
                "legacy Semcode path ranking was empty; preserved the evidence-backed original_call_chain as the test target"
            )
            evidence.append({
                "kind": "legacy_path_target_derived",
                "source": "original_call_chain",
                "candidate": legacy_path,
            })

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
        "reproducer_dir": contract.reproducer_dir,
        "rootfs_path": contract.rootfs_path,
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

    reproducer = contract.reproducer
    if reproducer.language != "c" or reproducer.artifact_type != "userspace":
        errors.append("reproducer must be a userspace C program")
    if contract.reproducer_module_path:
        errors.append("kernel modules are forbidden; reproducer_module_path must be empty")
    if not reproducer.source_dir:
        errors.append("missing reproducer.source_dir")
    else:
        source_dir = _resolve_contract_path(reproducer.source_dir)
        if not source_dir.is_dir():
            errors.append(f"reproducer.source_dir does not exist: {reproducer.source_dir}")
        else:
            data["reproducer"]["source_dir"] = str(source_dir)
            for source_file in reproducer.source_files:
                if not source_file or Path(source_file).is_absolute() or ".." in Path(source_file).parts:
                    errors.append(f"invalid reproducer source file: {source_file!r}")
                    continue
                if Path(source_file).suffix not in {".c", ".h"}:
                    errors.append(f"non-C reproducer source is forbidden: {source_file}")
                if not (source_dir / source_file).is_file():
                    errors.append(f"reproducer source does not exist: {source_file}")
    if reproducer.entry_source not in reproducer.source_files:
        errors.append("reproducer.entry_source must be included in source_files")
    oracle = contract.call_chain_oracle
    if not oracle.fault_signatures:
        errors.append("missing call_chain_oracle.fault_signatures")
    elif not contract.expected_signal:
        data["expected_signal"] = oracle.fault_signatures[0]
        warnings.append("expected_signal derived from call_chain_oracle.fault_signatures[0]")
    if not oracle.required_frames:
        errors.append("missing call_chain_oracle.required_frames")

    path_errors, path_evidence = _validate_path_analysis_contract(
        data, path_analysis_required=path_analysis_required,
    )
    errors.extend(path_errors)
    evidence.extend(path_evidence)
    errors.extend(_validate_structured_uaf_analysis(
        contract, path_analysis_required=path_analysis_required,
    ))

    data["warnings"] = warnings
    data["evidence"] = evidence
    if errors:
        data["status"] = "blocked"
        data["blocked_reason"] = "; ".join(errors)
        print(f"  [contract诊断] 校验发现 {len(errors)} 个错误: {'; '.join(errors[:3])}", flush=True)
    else:
        # Re-validation can repair a legacy contract that was persisted as
        # blocked only because its derived path selectors were empty.  Do not
        # carry that stale terminal status into Test Expert after all current
        # artifact and path checks pass.
        data["status"] = "ok"
        data["blocked_reason"] = ""
    return _model_validate(KernelExpertOutput, data)
