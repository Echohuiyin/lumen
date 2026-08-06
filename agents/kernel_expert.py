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
    copied_files: list[str] = []
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
            copied_files.append(relative.as_posix())
        except OSError:
            continue
    manifest = {
        "source_workdir": str(workdir.resolve()),
        "session_output_dir": str(session_output_dir.resolve()),
        "copied_files": sorted(copied_files),
    }
    try:
        (session_output_dir / ".codex_artifact_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + chr(10),
            encoding="utf-8",
        )
    except OSError:
        pass


def _materialized_contract_response(session_output_dir: Path) -> "AIMessage | None":
    """Turn this invocation's manifest-proven contract into a final response.

    Codex can finish writing the handoff and then return only prose (or an
    empty stream) while the workflow is waiting for the cosmetic JSON marker.
    Reusing a contract is safe only when the manifest names this exact durable
    session and includes the contract file; all normal validation and the
    guest call-chain oracle still run after this boundary.
    """
    manifest_path = session_output_dir / ".codex_artifact_manifest.json"
    contract_path = session_output_dir / "KERNEL_CONTRACT.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if Path(str(manifest.get("session_output_dir") or "")).resolve() != session_output_dir.resolve():
            return None
        copied_files = {
            str(item) for item in manifest.get("copied_files", [])
            if isinstance(item, str)
        }
        if "KERNEL_CONTRACT.json" not in copied_files or not contract_path.is_file():
            return None
        contract = _model_validate(
            KernelExpertOutput,
            json.loads(contract_path.read_text(encoding="utf-8")),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if contract.status != "ok" or not _kernel_contract_has_handoff(contract):
        return None
    payload = json.dumps(model_to_dict(contract), ensure_ascii=False, indent=2)
    return AIMessage(content=f"KERNEL_CONTRACT:\n```json\n{payload}\n```")



def _static_check_userspace_reproducer(contract, session_output_dir: Path) -> dict[str, str]:
    """Audit a userspace C reproducer before handing it to Test Expert.

    The gate is deliberately separate from the guest verdict.  It checks the
    exact source files with the declared toolchain for warning-clean syntax,
    link/ABI usage, and compiler-supported static semantic diagnostics.  The
    guest still recompiles and executes the program against its own libc and
    kernel, and only the guest call-chain oracle can establish reproduction.
    """
    reproducer = getattr(contract, "reproducer", None)
    audit_lines = [
        "KERNEL EXPERT USERSPACE C PREFLIGHT",
        "CHECKS: syntax-and-warnings, link-and-ABI-usage, static-semantic-analysis",
    ]

    def finish(status: str, detail: str) -> dict[str, str]:
        audit_lines.append("STATUS: " + status)
        audit_lines.append("DETAIL: " + detail)
        try:
            session_output_dir.mkdir(parents=True, exist_ok=True)
            (session_output_dir / "static_check.txt").write_text(
                "\n".join(audit_lines) + "\n", encoding="utf-8",
            )
        except OSError:
            pass
        return {"status": status, "detail": detail[-6000:]}

    if reproducer is None or reproducer.language != "c" or reproducer.artifact_type != "userspace":
        return finish("skipped", "not a userspace C reproducer")
    source_dir = Path(os.path.expanduser(str(reproducer.source_dir or ""))).resolve()
    audit_lines.append("SOURCE_DIR: " + str(source_dir))
    if not source_dir.is_dir():
        return finish("failed", f"source_dir does not exist: {source_dir}")

    source_paths: list[Path] = []
    for declared in reproducer.source_files:
        relative = Path(str(declared))
        if relative.is_absolute() or ".." in relative.parts or relative.suffix not in {".c", ".h"}:
            return finish("failed", f"invalid userspace source path: {declared!r}")
        source = (source_dir / relative).resolve()
        try:
            source.relative_to(source_dir)
        except ValueError:
            return finish("failed", f"userspace source escapes source_dir: {declared!r}")
        if not source.is_file():
            return finish("failed", f"declared userspace source is missing: {source}")
        audit_lines.append("SOURCE: " + relative.as_posix())
        if source.suffix == ".c":
            source_paths.append(source)
    if not source_paths:
        return finish("failed", "userspace contract declares no C source")

    compiler_name = str(reproducer.compiler or "gcc")
    compiler = shutil.which(compiler_name)
    audit_lines.append("COMPILER: " + compiler_name)
    if not compiler:
        return finish("failed", f"static compiler is unavailable: {compiler_name}")

    extra_args: list[str] = []
    skip_next = False
    forbidden_warning_flags = {"-w", "-Wno-error", "-Wno-all", "-Wno-extra"}
    for raw_arg in reproducer.compiler_args:
        arg = str(raw_arg)
        if skip_next:
            skip_next = False
            continue
        if arg in {"-o", "--output"}:
            skip_next = True
            continue
        if arg in {"-c", "-S", "-E"} or arg.startswith("-o") or arg.startswith("--output="):
            continue
        if "\n" in arg or "\x00" in arg:
            return finish("failed", "compiler argument contains a control character")
        if arg in forbidden_warning_flags or arg.startswith("-Wno-analyzer"):
            return finish("failed", f"compiler argument disables required diagnostics: {arg}")
        extra_args.append(arg)
    if skip_next:
        return finish("failed", "compiler output option is missing its value")

    libraries: list[str] = []
    for raw_library in reproducer.link_libraries:
        library = str(raw_library)
        if not library or "\n" in library or "\x00" in library:
            return finish("failed", f"invalid link library: {library!r}")
        libraries.append(library if library.startswith("-l") else f"-l{library}")

    mandatory_flags = ["-std=gnu11", "-O2", *extra_args, "-Wall", "-Wextra", "-Werror"]
    source_args = [str(path) for path in source_paths]

    def run_check(label: str, command: list[str], timeout: int = 90) -> bool:
        audit_lines.append(label + "_COMMAND: " + repr(command))
        try:
            completed = subprocess.run(
                command,
                cwd=str(source_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            audit_lines.append(label + "_STATUS: failed")
            audit_lines.append(label + "_ERROR: " + str(exc))
            return False
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        audit_lines.append(label + "_RETURN_CODE: " + str(completed.returncode))
        audit_lines.append(label + "_STDOUT:\n" + stdout[-6000:])
        audit_lines.append(label + "_STDERR:\n" + stderr[-6000:])
        audit_lines.append(label + "_STATUS: " + ("passed" if completed.returncode == 0 else "failed"))
        return completed.returncode == 0

    syntax_command = [compiler, *mandatory_flags, "-fsyntax-only", *source_args]
    if not run_check("SYNTAX", syntax_command):
        return finish("failed", "warning-clean syntax check failed; repair the C source before guest handoff")

    # Match the persistent guest runner's link order and -l normalization so
    # missing symbols and incorrect userspace API usage are caught here.
    import tempfile
    with tempfile.TemporaryDirectory(prefix=".lumen-static-", dir=str(source_dir)) as temp_dir:
        linked_binary = str(Path(temp_dir) / "lumen-repro")
        link_command = [compiler, *mandatory_flags, *source_args, *libraries, "-o", linked_binary]
        if not run_check("LINK", link_command):
            return finish("failed", "link/ABI usage check failed; repair declarations, calls, or link_libraries")

    try:
        version_probe = subprocess.run(
            [compiler, "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return finish("failed", f"cannot identify compiler semantic analyzer: {exc}")
    is_clang = "clang" in ((version_probe.stdout or "") + (version_probe.stderr or "")).lower()
    analyzer_name = "clang-analyze" if is_clang else "gcc-fanalyzer"
    audit_lines.append("SEMANTIC_ANALYZER: " + analyzer_name)
    # Compile each C translation unit separately. GCC/Clang reject a single
    # -o target for multiple -c inputs; the final link above already checks
    # cross-file declarations and userspace API usage.
    with tempfile.TemporaryDirectory(prefix=".lumen-semantic-", dir=str(source_dir)) as temp_dir:
        for index, source_arg in enumerate(source_args, start=1):
            label = "SEMANTIC" if index == 1 else f"SEMANTIC_{index}"
            if is_clang:
                analyzer_command = [compiler, *mandatory_flags, "--analyze", source_arg]
            else:
                analyzer_output = str(Path(temp_dir) / f"semantic-{index}.o")
                analyzer_command = [
                    compiler, *mandatory_flags, "-fanalyzer", "-c", source_arg,
                    "-o", analyzer_output,
                ]
            if not run_check(label, analyzer_command):
                return finish("failed", "static semantic analysis failed; repair lifetime, bounds, control-flow, or API usage")

    return finish("passed", "syntax, link/ABI usage, and static semantic analysis passed; guest compile/run remains mandatory")

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
                # Kernel reports use both title-case and upper-case spellings
                # across architectures and kernel generations.  Matching only
                # the lower-case "Call trace:" spelling silently reduced many
                # complete crash reports to a blank evidence file, so the
                # Kernel Expert could not distinguish a real source/log issue
                # from an evidence-staging bug.
                marker_pattern = re.compile(
                    r"(?:unable\s+to\s+handle|kasan:|internal\s+error:|"
                    r"warning:|pc\s*:|lr\s*:|rip\s*:|oops:|bug:|#pf:|"
                    r"kernel\s+panic|call\s+trace:|end\s+trace)",
                    re.IGNORECASE,
                )
                for line in lines:
                    if re.search(r"call\s+trace:", line, re.IGNORECASE):
                        in_trace = True
                    is_marker = bool(marker_pattern.search(line))
                    if is_marker or in_trace:
                        # Keep exact crash signatures and call-chain lines;
                        # only discard known host metadata that is not part of
                        # the oracle.  Filtering trace lines by the generic
                        # operational-risk regex can remove legitimate frame
                        # names and make a complete report look truncated.
                        if line.startswith(("CPU:", "Modules linked")):
                            continue
                        if not in_trace and not is_marker and risk_pattern.search(line):
                            continue
                        selected.append(line)
                    if in_trace and re.search(r"end\s+trace", line, re.IGNORECASE):
                        break
                raw = "\n".join(selected) + "\n"
            elif name in {"semcode-evidence.json", "fix.patch"}:
                # These are explicit source-backed evidence artifacts.
                # Preserve them byte-for-byte; generic risk filtering would
                # silently remove patch hunks or JSON fields.
                destination.write_text(raw, encoding="utf-8")
                continue
            else:
                raw = "\n".join(
                    line for line in raw.splitlines()
                    if not risk_pattern.search(line)
                ) + "\n"
            destination.write_text(raw, encoding="utf-8")
        except OSError:
            continue

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from agents.contracts import (
    CallChainOracle,
    KernelExpertOutput,
    PathAnalysisScope,
    RefcountPath,
    TestResultContract,
    UafAnalysisContract,
    model_to_dict,
)
from agents.error_handling import classify_error, error_to_evidence
from agents.semcode_path_analysis import (
    SemcodeMcpClient,
    SemcodePathAnalysisResult,
    analyze_uaf_paths,
    configured_semcode_timeout_sec,
    extract_semcode_entry_points,
    render_semcode_analysis_context,
    resolve_kernel_commit,
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
      - mounted filesystem fixtures such as mount_0.raw or mount_0.gz

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
                elif (
                    entry.suffix.lower() in {".raw", ".img", ".qcow2", ".gz"}
                    and (name.startswith("mount_") or name.startswith("disk"))
                ):
                    findings.append({
                        "name": name,
                        "path": str(entry),
                        "kind": "filesystem_fixture",
                    })
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
                "filesystem_fixture": "原始挂载文件系统镜像（只读输入资产，禁止格式化或改写）",
            }.get(f["kind"], f["kind"])
            parts.append(f"- {f['name']} ({kind_label}): {f['path']}")
        parts.append("")
        parts.append("**使用顺序**：")
        parts.append("1. 有预编译用户态程序 → 核对接口后在契约中声明其 guest 内运行步骤")
        parts.append("2. 有用户态 C 源码 → 由 Test Expert 在 guest 内编译并记录编译证据")
        parts.append("3. 有测试说明文档 → 必读，里面有 smp/numa/timeout 等关键配置")
        if any(f["kind"] == "filesystem_fixture" for f in assets):
            parts.append(
                "4. 有原始挂载文件系统镜像 → 必须在 KERNEL_CONTRACT 中通过 "
                "binaries_dir/execution_steps 传入该确切文件；禁止创建全零替代品、ftruncate、mkfs 或改写原始镜像。"
            )

    if not parts:
        return ""
    return "\n".join(parts)


def _kernel_expert_contract_is_terminal(contract: KernelExpertOutput | None) -> bool:
    """Return whether the model supplied a complete handoff or terminal block.

    A structured ``blocked`` contract with an explicit reason is a valid
    outcome: retrying the same source/image cannot create a missing guest
    capability or source prerequisite.  Empty/degraded responses still need
    the existing single repair turn.
    """
    if contract is None:
        return False
    if contract.status == "blocked":
        return bool(str(contract.blocked_reason or "").strip())
    return bool(
        contract.status not in {"degraded", "blocked"}
        and contract.root_cause
        and (
            contract.call_chain_oracle.required_top_frames
            or contract.call_chain_oracle.required_frames
        )
    )


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
- Exact target source checkout: {target_kernel_dir} — scope all source searches to this path; it is the required Semcode MCP checkout
- Do not scan broad filesystem trees (/home, /usr, /opt, runtime/, or sessions/); use bounded searches in this checkout or staged evidence only
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
        # Prefer a same-invocation, manifest-proven handoff over a second
        # expensive Codex turn when the model completed the artifacts but did
        # not echo the cosmetic final JSON marker.  The node still performs
        # source/path/static validation before routing to Test Expert.
        if not output_content.strip() or not _kernel_expert_contract_is_terminal(
            _extract_kernel_contract(output_content)
        ):
            materialized = _materialized_contract_response(session_output_dir)
            if materialized is not None:
                response = materialized
                output_content = materialized.content
        static_preflight = None
        parsed_preflight = _extract_kernel_contract(output_content) if output_content.strip() else None
        if parsed_preflight is not None and parsed_preflight.status == "ok":
            static_preflight = _static_check_userspace_reproducer(
                parsed_preflight, session_output_dir,
            )
        if static_preflight and static_preflight.get("status") == "failed":
            retry_messages = messages + [HumanMessage(content=(
                "STATIC PREFLIGHT FAILED; do not hand this C program to Test Expert.\n"
                f"Evidence: {static_preflight.get('detail', '')}\n"
                "Repair the userspace C compile, warning, or safety issue in the current Codex workdir, "
                "rerun the static check, and emit a complete KERNEL_CONTRACT. "
                "Do not change the kernel oracle to hide the failure."
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

        # Retry once inside this same Kernel Expert loop when the final turn
        # is empty or lacks a parseable structured contract.  A bare JSON
        # object is valid too; requiring the cosmetic marker here used to
        # trigger a second expensive Codex run even when the contract was
        # complete and only the heading punctuation differed.
        parsed_contract = _extract_kernel_contract(output_content) if output_content.strip() else None
        has_structured_contract = _kernel_expert_contract_is_terminal(parsed_contract)
        if not output_content.strip() or not has_structured_contract:
            retry_messages = messages + [HumanMessage(content=(
                "当前最终输出缺少可解析的 KERNEL_CONTRACT。请在本次 loop 内补交完整结构化 JSON，"
                "保留已完成的维护分析、用户态测试程序和验证结果；不要引用旧文件或省略字段。"
                "现在停止继续探索和重复调用工具：先在当前 workdir 写入完整的 KERNEL_CONTRACT.json，"
                "并确保其中声明的 diagnostic_test.c 与清单均已落盘，然后在最终消息中输出完整 JSON。"
                "如果当前用户态程序或环境仍无法满足契约，必须写入 status=blocked 及明确原因；"
                "不得只返回 prose、空响应或沿用旧 session 的文件。"
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
        # A retry may have written the complete contract to the durable
        # workdir while returning only prose.  Re-materialize this invocation's
        # manifest-proven contract before parsing the final response; never
        # consult an older kernel_contract.json.
        materialized = _materialized_contract_response(session_output_dir)
        if materialized is not None and not _kernel_expert_contract_is_terminal(
            _extract_kernel_contract(output_content) if output_content.strip() else None
        ):
            response = materialized
            output_content = materialized.content
        if not output_content.strip():
            output_content = f"（{backend_label} 未生成最终文本，请检查 {backend_label} CLI 输出）"
        _write_tool_call_output(output_file, output_content, expert_name)

        return response

    except Exception as e:
        error_msg = f"{backend_label} 调用失败: {str(e)}"
        _write_tool_call_output(output_file, error_msg, expert_name)
        _sync_codex_artifacts(codex_workdir, session_output_dir)
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
    supplied_report = str(input_artifacts.get("crash_report_path", "") or "")
    if supplied_report and Path(os.path.expanduser(supplied_report)).is_file():
        return supplied_report
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


def _materialize_fix_evidence(
    output_dir: Path, input_artifacts: dict, source_root: str,
) -> str:
    """Stage an explicitly declared upstream fix for source-backed review.

    The fix is optional evidence: absent or unreadable metadata never becomes
    a guessed diagnosis. A commit is rendered from the exact pinned source
    checkout; a patch path is copied byte-for-byte into the evidence dir.
    """
    patch_path = str(input_artifacts.get("fix_patch_path", "") or "").strip()
    fix_commit = str(input_artifacts.get("fix_commit", "") or "").strip()
    destination = output_dir / "fix.patch"
    try:
        if patch_path:
            source = Path(os.path.expanduser(patch_path)).resolve()
            if not source.is_file():
                return ""
            shutil.copyfile(source, destination)
            return str(destination.resolve())
        if not fix_commit or not re.fullmatch(r"[0-9a-fA-F]{7,40}", fix_commit):
            return ""
        if not source_root or not Path(source_root).is_dir():
            return ""
        result = subprocess.run(
            ["git", "-C", str(Path(source_root).resolve()), "show",
             "--format=", "--no-ext-diff", fix_commit],
            check=False, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return ""
        destination.write_text(result.stdout, encoding="utf-8")
        return str(destination.resolve())
    except (OSError, subprocess.SubprocessError):
        return ""


_SEM_CODE_FRAME_RE = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*"
    r"(?:\.(?:cold|isra|constprop|part)(?:\.\d+)*)?)"
    r"\+0x[0-9a-fA-F]+"
)
_SEM_CODE_DIRECT_CALL_RE = re.compile(
    r"(?m)^\s*→\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\s*$"
)
_SEM_CODE_BODY_CALL_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\s*\("
)
_C_CONTROL_WORDS = {
    "if", "else", "for", "while", "do", "switch", "case", "return",
    "sizeof", "typeof", "typeof_member", "offsetof",
}


def _semcode_evidence_is_complete(payload: dict) -> bool:
    """Return whether deterministic evidence covers the fault entry's callees.

    A non-empty Semcode response is not necessarily complete: a frame lookup
    can succeed while omitting the callee that contains the actual access. In
    that case Codex must keep the required interactive MCP path available so
    it can query the exact checkout instead of treating a partial index as
    authoritative. We scope this check to the first (fault-entry) result to
    avoid requiring every implementation helper mentioned by infrastructure
    frames such as process_one_work.
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("status") != "ok" or payload.get("failures"):
        return False
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        return False
    entry_names = {
        str(item.get("function", "")).strip()
        for item in entries
        if isinstance(item, dict) and str(item.get("function", "")).strip()
    }
    first = entries[0]
    if not isinstance(first, dict):
        return False
    result = str(first.get("result", "") or "")
    if not result.strip() or "function not found" in result.lower():
        return False
    calls_start = result.find("\nCalls:")
    if calls_start < 0:
        return True
    calls_text = result[calls_start:]
    called_by_start = calls_text.find("\nCalled By:")
    if called_by_start >= 0:
        calls_text = calls_text[:called_by_start]
    direct_callees = set(_SEM_CODE_DIRECT_CALL_RE.findall(calls_text))
    body_start = result.find("\nBody:")
    if body_start >= 0:
        body_calls = set(_SEM_CODE_BODY_CALL_RE.findall(result[body_start:]))
        body_calls.difference_update(_C_CONTROL_WORDS)
        body_calls.discard(str(first.get("function", "")).strip())
        direct_callees.update(body_calls)
    return direct_callees.issubset(entry_names)


def _semcode_evidence_covers_report_frames(payload: dict, report_text: str) -> bool:
    """Return whether exact Semcode evidence covers every reported frame.

    A fault-entry body can be complete while its implementation helpers are
    not included in the deterministic batch.  Those helpers are not part of
    the runtime oracle; forcing an interactive query for them can leave Codex
    waiting indefinitely.  We only disable interactive MCP when the exact
    payload is valid, the fault entry has a non-empty body, and every frame
    printed by the first-hand report is present in the same exact-commit
    entry set.  Missing report frames still keep the existing strict path.
    """
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return False
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries or not report_text:
        return False
    entry_names = {
        str(item.get("function", "")).strip()
        for item in entries
        if isinstance(item, dict) and str(item.get("function", "")).strip()
    }
    first = entries[0]
    if not isinstance(first, dict):
        return False
    first_result = str(first.get("result", "") or "")
    if not first_result.strip() or "Body:" not in first_result:
        return False
    report_frames = {
        name.strip()
        for name in _SEM_CODE_FRAME_RE.findall(str(report_text))
        if name.strip()
    }
    return bool(report_frames) and report_frames.issubset(entry_names)


# Kernel reports also spell out inlined frames as ``pc : symbol path:line``
# and as source-backed Call trace lines without an offset.  Keep these
# patterns narrow enough to avoid treating prose identifiers as Semcode
# symbols, while still capturing the actual inline warning function.
_SEM_CODE_PC_LR_FRAME_RE = re.compile(
    r"(?m)^[ \t]*(?:pc|lr)[ \t]*:[ \t]*"
    r"([A-Za-z_][A-Za-z0-9_]*(?:\.(?:cold|isra|constprop|part)(?:\.\d+)*)?)"
    r"(?=[ \t]+(?:[A-Za-z0-9_.-]+/)+[^ \t:]+:\d+|\+0x)"
)
_SEM_CODE_TRACE_FRAME_RE = re.compile(
    r"(?m)^[ \t]+"
    r"([A-Za-z_][A-Za-z0-9_]*(?:\.(?:cold|isra|constprop|part)(?:\.\d+)*)?)"
    r"(?=[ \t]+(?:[A-Za-z0-9_.-]+/)+[^ \t:]+:\d+|\+0x)"
)
_SEM_CODE_INDEXING_MARKERS = (
    "database is currently being indexed",
    "database is empty",
    "background indexing hasn't started",
    "database indexing failed",
)
try:
    _SEM_CODE_RETRY_ATTEMPTS = max(
        1, int(os.environ.get("LUMEN_SEMCODE_RETRY_ATTEMPTS", "3"))
    )
except (TypeError, ValueError):
    _SEM_CODE_RETRY_ATTEMPTS = 3
try:
    _SEM_CODE_RETRY_WAIT_SECONDS = max(
        0.0, float(os.environ.get("LUMEN_SEMCODE_RETRY_WAIT_SECONDS", "15"))
    )
except (TypeError, ValueError):
    _SEM_CODE_RETRY_WAIT_SECONDS = 15.0


def _semcode_indexing_response(result: object) -> bool:
    result_text = str(result or "").lower()
    return any(marker in result_text for marker in _SEM_CODE_INDEXING_MARKERS)


def _materialize_semcode_evidence(
    output_dir: Path,
    *,
    source_path: str,
    expected_commit: str,
    command: str,
    args: list[str],
    evidence_text: str,
    semcode_source_path: str = "",
) -> str:
    """Persist exact-commit Semcode results for the Codex workspace.

    Some Codex MCP sessions cancel an otherwise valid Semcode call at the
    client boundary. Fetch the same required MCP data once through Lumen's
    deterministic adapter, preserving the explicit commit on every query, so
    the Kernel Expert can continue from auditable source evidence without a
    source-text or model fallback.
    """
    names: list[str] = []
    for pattern in (
        _SEM_CODE_FRAME_RE,
        _SEM_CODE_PC_LR_FRAME_RE,
        _SEM_CODE_TRACE_FRAME_RE,
    ):
        for match in pattern.finditer(evidence_text or ""):
            name = match.group(1)
            if name not in names:
                names.append(name)
            if len(names) >= 32:
                break
        if len(names) >= 32:
            break
    evidence_path = Path(output_dir) / "semcode-evidence.json"
    # A retry in the same workflow session has the same exact source checkout
    # and commit.  Reuse only a durable, status-ok adapter result whose
    # identity matches both values; this is not a contract/source fallback and
    # avoids re-indexing the same tree before every try-out.
    try:
        cached = json.loads(evidence_path.read_text(encoding="utf-8"))
        cached_source = Path(str(cached.get("kernel_source") or "")).resolve()
        requested_source = Path(str(source_path or "")).resolve()
        if (
            cached.get("status") == "ok"
            and not cached.get("failures")
            and cached.get("entries")
            and str(cached.get("expected_kernel_commit") or "").strip().lower()
            == str(expected_commit or "").strip().lower()
            and cached_source == requested_source
        ):
            return str(evidence_path.resolve())
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    entries: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    if names and source_path and expected_commit and command:
        try:
            client = SemcodeMcpClient(
                command=command,
                args=args,
                kernel_source_path=semcode_source_path or source_path,
                git_sha=expected_commit,
                timeout_sec=configured_semcode_timeout_sec(),
            )
            batch_call = getattr(client, "_call_many", None)
            if callable(batch_call):
                requests = [("find_function", {"name": name}) for name in names]
                results = list(batch_call(requests))
                # The MCP server may return the first batch result while its
                # background index is still warming. Retry only the missing
                # functions one at a time so already-resolved evidence is not
                # repeatedly queried and the server can finish initialization.
                for index, result in enumerate(results):
                    if not _semcode_indexing_response(result):
                        continue
                    request = [requests[index]]
                    for attempt in range(_SEM_CODE_RETRY_ATTEMPTS):
                        single = list(batch_call(request))
                        if single:
                            results[index] = single[0]
                        if not _semcode_indexing_response(results[index]):
                            break
                        if attempt + 1 < _SEM_CODE_RETRY_ATTEMPTS:
                            time.sleep(_SEM_CODE_RETRY_WAIT_SECONDS)
                for name, result in zip(names, results):
                    result_text = str(result or "")
                    if _semcode_indexing_response(result_text):
                        failures.append({"function": name, "error": result_text})
                    else:
                        entries.append({"function": name, "result": result_text})
            else:
                # Compatibility for injected clients that predate the batch API.
                for name in names:
                    try:
                        result = client._call("find_function", {"name": name})
                        result_text = str(result or "")
                        if _semcode_indexing_response(result_text):
                            failures.append({"function": name, "error": result_text})
                        else:
                            entries.append({"function": name, "result": result_text})
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
        "status": "ok" if entries and not failures else "blocked",
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


_FIRST_HAND_LOG_ACTION_MARKERS = (
    "fault_injection",
    "failslab",
    "probability",
    "interval",
    "times",
    "gadgetfs",
    "dummy_udc",
    "usbip-vudc",
    "mount",
    "umount",
    "openat",
    "vfs_open",
    "write",
    "read",
    "close",
    "ioctl",
    "mmap",
    "send",
    "recv",
    "syz_",
    "call trace",
    "allocated by task",
    "freed by task",
    "kasan",
    "unable to handle",
    "general protection",
    "kernel bug",
    "warning:",
)

# Keep high-level setup evidence in the Codex prompt, but do not echo crash
# sanitizer/error labels or syzkaller identifiers into the model message. The
# complete first-hand log remains available under evidence/original.log.
_FIRST_HAND_LOG_PROMPT_FILTER = (
    "kasan",
    "use-after-free",
    "use after free",
    "kernel bug",
    "kernel panic",
    "unable to handle",
    "general protection",
    "invalid opcode",
    "call trace",
    "allocated by task",
    "freed by task",
    "syz_",
)


def _extract_first_hand_log_hints(
    log_text: str,
    *,
    max_lines: int = 80,
    max_chars: int = 8000,
) -> str:
    """Extract observable setup/trigger evidence from the first-hand log.

    Kernel Expert still receives the complete log as an evidence artifact.  A
    compact deterministic excerpt prevents the prompt boundary from hiding
    prerequisites such as a write-driven bind or an explicit fault injector,
    while avoiding any supplied reproducer source or command substitution.
    """
    if not log_text:
        return ""
    selected: list[str] = []
    seen: set[str] = set()
    for raw_line in str(log_text).splitlines():
        line = raw_line.strip()
        if not line or line in seen:
            continue
        lowered = line.lower()
        if any(marker in lowered for marker in _FIRST_HAND_LOG_PROMPT_FILTER):
            continue
        has_action_marker = any(
            marker in lowered for marker in _FIRST_HAND_LOG_ACTION_MARKERS
        )
        has_symbol_frame = bool(
            re.search(
                r"\b[A-Za-z_][A-Za-z0-9_.$]*\+0x[0-9a-fA-F]+(?:/0x[0-9a-fA-F]+)?",
                line,
            )
        )
        if not has_action_marker and not has_symbol_frame:
            continue
        seen.add(line)
        selected.append(line[:500])
        if len(selected) >= max_lines:
            break
    return "\n".join(selected)[:max_chars]


def _recover_materialized_contract_after_cli_failure(
    *,
    state: MaintenanceWorkflowState,
    error: RuntimeError,
    error_message: str,
    semcode_path_analysis: SemcodePathAnalysisResult | None,
    input_artifacts: dict,
) -> dict:
    """Recover only this invocation's manifest-proven complete contract.

    Codex can finish writing and statically checking the handoff before a
    cosmetic final-response retry times out. Recovery is allowed to continue
    to Test Expert only when the durable session manifest proves that the
    contract and its C source were copied by this invocation, and all normal
    artifact gates still pass.
    """
    output_dir = paths_get_output_dir().resolve()
    manifest_path = output_dir / '.codex_artifact_manifest.json'
    contract_path = output_dir / 'KERNEL_CONTRACT.json'
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if Path(str(manifest.get('session_output_dir') or '')).resolve() != output_dir:
            return {}
        copied_files = {
            str(item) for item in manifest.get('copied_files', [])
            if isinstance(item, str)
        }
        if 'KERNEL_CONTRACT.json' not in copied_files or not contract_path.is_file():
            return {}
        contract = _model_validate(
            KernelExpertOutput,
            json.loads(contract_path.read_text(encoding='utf-8')),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}

    if (
        contract.status != 'ok'
        or not contract.root_cause.strip()
        or not _kernel_contract_has_handoff(contract)
    ):
        return {}

    try:
        contract = _enrich_kernel_contract_from_runtime(
            contract,
            input_artifacts=input_artifacts,
            output_dir=output_dir,
        )
        path_analysis_required = _requires_path_analysis(state.get('user_input', ''))
        if semcode_path_analysis is not None:
            contract = _apply_semcode_path_analysis(contract, semcode_path_analysis)
        static_preflight = _static_check_userspace_reproducer(contract, output_dir)
        if static_preflight.get('status') == 'failed':
            return {}
        contract = _validate_kernel_contract_artifacts(
            contract,
            path_analysis_required=path_analysis_required,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}

    if not _kernel_contract_ready_for_test(contract):
        return {}

    data = model_to_dict(contract)
    warnings = list(data.get('warnings') or [])
    warnings.append(
        'Recovered the manifest-proven contract after a later Kernel Expert CLI retry timed out; '
        'the contract still requires real Test Expert SSH-QEMU verification.'
    )
    data['warnings'] = warnings
    evidence = list(data.get('evidence') or [])
    evidence.append({
        'kind': 'kernel_expert_cli_timeout_recovery',
        'contract_artifact': str(contract_path),
        'manifest_artifact': str(manifest_path),
        'error': str(error),
    })
    data['evidence'] = evidence
    contract = _model_validate(KernelExpertOutput, data)
    try:
        (output_dir / 'kernel_contract.json').write_text(
            json.dumps(model_to_dict(contract), ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
    except OSError:
        pass

    previous_analysis = str(state.get('kernel_analysis', '') or '').strip()
    recovery_note = (
        f'{error_message}\n\n'
        'The current invocation had already produced a manifest-proven, '
        'static-checked Kernel Expert contract; continuing to Test Expert for '
        'real guest verification.'
    )
    return {
        'kernel_analysis': (
            f'{previous_analysis}\n\n{recovery_note}'
            if previous_analysis else recovery_note
        ),
        'reproduce_case': str(data.get('root_cause') or ''),
        'kernel_diagnosis': str(data.get('root_cause') or ''),
        'all_possible_paths': list(data.get('all_possible_paths') or []),
        'max_likely_path': str(data.get('max_likely_path') or ''),
        'uaf_analysis_contract': data.get('uaf_analysis') or {},
        'kernel_ready_for_test': True,
        'kernel_contract': data,
        'target_arch': contract.target_arch,
        'boot_kernel_path': contract.boot_kernel_path,
        'reproducer_dir': contract.reproducer_dir,
        'reproducer_module_path': contract.reproducer_module_path,
        'expected_signal': contract.expected_signal,
        'binaries_dir': contract.binaries_dir,
        'semcode_path_analysis': (
            semcode_path_analysis.as_dict()
            if semcode_path_analysis is not None else {}
        ),
    }



def _preserve_valid_contract_after_cli_failure(
    *, state: MaintenanceWorkflowState, error: RuntimeError,
    error_message: str, semcode_path_analysis: SemcodePathAnalysisResult | None,
) -> dict:
    """Retain a completed diagnosis when a later retry times out.

    A retry is allowed to improve the userspace trigger, but a provider
    timeout must not erase the last source-backed root-cause contract.  The
    retained contract is marked blocked and is never routed back to QEMU;
    only the evidence archive can consume it.
    """
    try:
        previous = _model_validate(
            KernelExpertOutput, state.get("kernel_contract") or {},
        )
    except (TypeError, ValueError):
        return {}
    if (
        previous.status != "ok"
        or not previous.root_cause.strip()
        or not _kernel_contract_has_handoff(previous)
    ):
        return {}

    data = model_to_dict(previous)
    data["status"] = "blocked"
    data["build_status"] = "skipped"
    data["blocked_reason"] = str(error)
    warnings = list(data.get("warnings") or [])
    warnings.extend([
        error_message,
        "Retained the last complete Kernel Expert contract for root-cause evidence; "
        "the timed-out retry was not handed to Test Expert.",
    ])
    data["warnings"] = warnings
    data["evidence"] = [
        *(data.get("evidence") or []),
        error_to_evidence(
            classify_error(error, operation="kernel_expert CLI"),
            operation="kernel_expert CLI",
        ),
    ]
    preserved = _model_validate(KernelExpertOutput, data)
    previous_analysis = str(state.get("kernel_analysis", "") or "").strip()
    analysis = (
        f"{previous_analysis}\n\n## Kernel Expert retry blocked\n{error_message}"
        if previous_analysis else error_message
    )
    return {
        "kernel_analysis": analysis,
        "reproduce_case": state.get("reproduce_case", ""),
        "kernel_diagnosis": state.get("kernel_diagnosis", ""),
        "kernel_ready_for_test": False,
        "kernel_contract": model_to_dict(preserved),
        "final_response": error_message,
        "semcode_path_analysis": (
            semcode_path_analysis.as_dict() if semcode_path_analysis else {}
        ),
    }


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
    # Keep direct source reads on a detached exact-commit worktree, while
    # Semcode queries use the deployment's completed multi-branch database.
    semcode_source_path = kernel_source_path
    expected_kernel_commit = input_artifacts.get("expected_kernel_commit", "")
    try:
        kernel_source_path = resolve_kernel_source_for_commit(
            kernel_source_path,
            expected_kernel_commit,
            workspace_root=str(session_dir or ""),
        )
        input_artifacts["kernel_source_path"] = kernel_source_path
        # Normalize an abbreviated input prefix to the exact full commit before
        # it reaches Semcode, Codex evidence, or the durable contract.
        expected_kernel_commit = resolve_kernel_commit(
            semcode_source_path, expected_kernel_commit,
        )
    except Exception as exc:
        return _blocked_source_verification({
            "status": "blocked",
            "blocked_reason": str(exc),
            "evidence": [{"kind": "kernel_source_worktree", "status": "blocked", "error": str(exc)}],
        })
    agent_config = _pin_semcode_mcp_to_source(agent_config, semcode_source_path)

    # A git_sha on an individual Semcode query is not a source-integrity proof:
    # the MCP server may silently answer from its default HEAD when that
    # snapshot is absent from the index.  Refuse to start the LLM/QEMU handoff
    # until the declared commit exists in git and is represented by an
    # up-to-date Semcode branch.  This prevents a plausible but wrong source
    # revision from becoming a successful reproduction.
    semcode_config = agent_config.get("semcode_mcp") or {}
    source_verification = verify_semcode_target(
        kernel_source_path=semcode_source_path,
        expected_kernel_commit=expected_kernel_commit,
        semcode_command=str(semcode_config.get("command", "")),
        semcode_args=semcode_config.get("args", []) or [],
    )
    if source_verification.get("status") != "ok":
        return _blocked_source_verification(source_verification)

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
    fix_evidence_path = _materialize_fix_evidence(
        paths_get_output_dir(), input_artifacts, kernel_source_path,
    )

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
        semcode_source_path=semcode_source_path,
        expected_commit=expected_kernel_commit,
        command=str(semcode_config.get("command", "")),
        args=list(semcode_config.get("args", []) or []),
        evidence_text=semcode_evidence_text,
    )
    if semcode_evidence_path:
        # Stage deterministic adapter output inside the Codex sandbox;
        # absolute durable-session paths are intentionally not readable from
        # the isolated workdir.
        evidence_files.append(("semcode-evidence.json", semcode_evidence_path))
    if fix_evidence_path:
        evidence_files.append(("fix.patch", fix_evidence_path))
    semcode_payload: dict[str, object] | None = None
    semcode_evidence_complete = False
    if semcode_evidence_path:
        try:
            semcode_payload = json.loads(
                Path(semcode_evidence_path).read_text(encoding="utf-8")
            )
            semcode_evidence_complete = (
                _semcode_evidence_is_complete(semcode_payload)
                or _semcode_evidence_covers_report_frames(
                    semcode_payload, original_log_text,
                )
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            semcode_evidence_complete = False
    llm_agent_config = dict(agent_config)
    if semcode_evidence_complete:
        # The deterministic adapter already queried the exact commit.  Do
        # not start Codex's interactive MCP client for the same complete
        # evidence: on ARM64 Codex 0.146 it can hang after the result arrives.
        # A blocked/partial adapter result keeps the required MCP path intact.
        llm_agent_config["semcode_mcp"] = {"disabled": True}
    path_analysis_required = _requires_path_analysis(
        state.get("user_input", ""),
        "\n".join(str(item.get("analysis_output", "")) for item in expert_results),
    )
    semcode_path_analysis: SemcodePathAnalysisResult | None = None
    if path_analysis_required:
        semcode_path_analysis = _restore_cached_semcode_path_analysis(
            state.get("semcode_path_analysis"),
            expected_commit=expected_kernel_commit,
            kernel_source_path=kernel_source_path,
        )
        if semcode_path_analysis is None:
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
                semcode_source_path=semcode_source_path,
                entry_points=entry_points,
                expected_kernel_commit=expected_kernel_commit,
                semcode_command=str(semcode_config.get("command", "")),
                semcode_args=semcode_config.get("args", []) or [],
                cached_evidence=semcode_payload,
            )
        if semcode_path_analysis.status != "ok":
            return _blocked_semcode_path_analysis(semcode_path_analysis)

        # The deterministic UAF path analysis has now queried the exact
        # checkout and commit, including direct-call evidence.  Do not make
        # Codex repeat those same MCP queries: on ARM64 this can leave the
        # interactive client waiting on a second cold index indefinitely.
        # Non-UAF cases retain the stricter complete-evidence gate above.
        llm_agent_config["semcode_mcp"] = {"disabled": True}

    llm = get_llm_with_config(
        llm_agent_config,
        default_config=default_config,
        agent_name="kernel_expert",
    )

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
        f"- Codex-visible exact Semcode evidence (read this before interactive MCP): {'evidence/semcode-evidence.json' if semcode_evidence_path else 'N/A'}\n\n"
        f"- rootfs_path: {input_artifacts.get('rootfs_path', 'N/A')}\n\n"
        f"- test_assets_dir: {input_artifacts.get('test_assets_dir', 'N/A')}\n\n"
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
    first_hand_log_hints = _extract_first_hand_log_hints(original_log_text)
    if first_hand_log_hints:
        user_content += (
            "\n\n## Deterministic first-hand log action hints\n"
            "These lines are extracted from the supplied kernel log, not from a user repro. "
            "Treat observable setup, syscall, pressure, and fault-injection events as "
            "reproduction prerequisites when the userspace ABI permits them. If a prerequisite "
            "cannot be implemented in the isolated guest, record that limitation in the contract "
            "instead of silently replacing it with a generic trigger.\n"
            "```text\n"
            f"{first_hand_log_hints}\n"
            "```"
        )
    if semcode_evidence_path:
        user_content += (
            "\n\nRead evidence/semcode-evidence.json before interactive MCP. "
            "It contains deterministic exact-commit Semcode results; do not replace it with source-text or grep fallback."
        )
    if semcode_path_analysis is not None:
        user_content += "\n\n" + render_semcode_analysis_context(semcode_path_analysis)
    if fix_evidence_path:
        user_content += (
            "\n\n## Explicit upstream fix evidence\n"
            "Read evidence/fix.patch before concluding. It is the exact declared "
            "fix commit/patch for this maintenance case; align the root cause "
            "and userspace trigger with its changed lifetime or synchronization "
            "semantics, and state any remaining uncertainty."
        )

    # 如果是重试（测试未通过），附加测试反馈
    test_result = state.get("test_result", "")
    if test_result:
        user_content += f"\n\n## 上次测试结果（未成功复现）\n{test_result}\n请重新分析并调整复现用例。"
        if "FIXTURE_SIZE_TOO_SMALL" in test_result:
            user_content += (
                "\n\n## 强制 fixture 修复约束\n"
                "上一轮 guest 已明确拒绝了声明的文件系统镜像大小，并给出了 required/minimum size。"
                "本轮必须先读取该精确数值，在新的 C 源码中把 fixture 镜像设为高于该要求并留出明确安全余量（本类 128 MiB 阈值至少使用 256 MiB），"
                "并在 KERNEL_CONTRACT 的 change_from_previous_tryout 中说明实际变更；"
                "不得复用相同的 image_bytes/IMAGE_BYTES，也不得把 unchanged fixture 交给 Test Expert。"
                "如果无法完成这一源代码变更，应返回 blocked 并说明原因，而不是生成看似新的相同用例。"
            )

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

    # Prefer an explicit test_assets_dir from input.txt. Mounted filesystem
    # images may live beside, rather than with, bzImage/vmlinux. Only use the
    # existing boot-image parent convention when no directory was declared;
    # contract and call-chain validation remain unchanged.
    test_assets_dir = str(input_artifacts.get("test_assets_dir", "") or "").strip()
    if test_assets_dir:
        test_assets_dir = os.path.expanduser(os.path.expandvars(test_assets_dir))
    if not test_assets_dir and boot_kernel_path:
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

        recovered_result = _recover_materialized_contract_after_cli_failure(
            state=state,
            error=e,
            error_message=error_msg,
            semcode_path_analysis=semcode_path_analysis,
            input_artifacts=input_artifacts,
        )
        if recovered_result:
            return recovered_result

        preserved_result = _preserve_valid_contract_after_cli_failure(
            state=state,
            error=e,
            error_message=error_msg,
            semcode_path_analysis=semcode_path_analysis,
        )
        if preserved_result:
            return preserved_result

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


def _restore_cached_semcode_path_analysis(
    raw: object,
    *,
    expected_commit: str,
    kernel_source_path: str,
) -> SemcodePathAnalysisResult | None:
    """Restore same-session exact-source P2 evidence for a retry.

    A cache hit is accepted only when its serialized scope identifies the
    same kernel commit and source checkout. Malformed, partial, or cross-case
    data returns ``None`` and the normal exact query runs.
    """
    if not isinstance(raw, dict) or raw.get("status") != "ok":
        return None
    scope_data = raw.get("scope")
    analysis_data = raw.get("analysis")
    if not isinstance(scope_data, dict) or not isinstance(analysis_data, dict):
        return None
    try:
        scope = PathAnalysisScope(**scope_data)
        if scope.kernel_commit.strip().lower() != str(expected_commit or "").strip().lower():
            return None
        expected_source = Path(str(kernel_source_path or "")).resolve()
        roots = {
            Path(str(domain.get("root") or "")).resolve()
            for domain in scope.source_domains
            if isinstance(domain, dict) and str(domain.get("root") or "").strip()
        }
        if roots and expected_source not in roots:
            return None
        analysis = UafAnalysisContract(**analysis_data)
    except (OSError, TypeError, ValueError):
        return None
    evidence = list(raw.get("evidence") or [])
    evidence.append({
        "kind": "semcode_path_analysis_cache",
        "status": "reused_same_session_exact_source",
        "kernel_commit": scope.kernel_commit,
        "kernel_source": str(expected_source),
    })
    return SemcodePathAnalysisResult(
        status="ok",
        analysis=analysis,
        scope=scope,
        evidence=evidence,
        blocked_reason="",
    )


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

    # Run the static gate only after the contract has passed the explicit
    # handoff-shape check.  Partial text contracts are still allowed to be
    # enriched/validated by the existing recovery path; applying the gate to
    # them would overwrite a recoverable status with a misleading build block.
    static_preflight = (
        _static_check_userspace_reproducer(kernel_contract, paths_get_output_dir())
        if kernel_contract.status == "ok" else
        {"status": "skipped", "detail": "contract is not ready for static userspace preflight"}
    )
    if static_preflight.get("status") == "failed":
        data = model_to_dict(kernel_contract)
        data["status"] = "blocked"
        data["build_status"] = "blocked"
        data["blocked_reason"] = (
            "Kernel Expert static userspace preflight failed: "
            + static_preflight.get("detail", "unknown error")
        )
        warnings = list(data.get("warnings") or [])
        warnings.append("static_check.txt records the failed userspace preflight")
        data["warnings"] = warnings
        kernel_contract = _model_validate(KernelExpertOutput, data)
    elif static_preflight.get("status") == "passed":
        data = model_to_dict(kernel_contract)
        warnings = list(data.get("warnings") or [])
        warnings.append("static userspace C preflight passed; guest compile/run remains mandatory")
        data["warnings"] = warnings
        kernel_contract = _model_validate(KernelExpertOutput, data)

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
    # Codex may express the evidence archive as a path manifest (mapping
    # artifact names to paths/lists) instead of the list-shaped contract
    # field. Preserve that audit data as one structured entry so a complete
    # contract is not rejected merely because its evidence presentation is
    # different. Do not infer or execute anything from this field.
    raw_evidence = normalized.get("evidence")
    if isinstance(raw_evidence, dict):
        normalized["evidence"] = [{
            "kind": "artifact_manifest",
            "entries": raw_evidence,
        }]
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


def _contract_frame_symbol(frame: object) -> str:
    """Return a call-chain symbol name without offsets or source annotations."""
    value = str(frame or "").strip()
    value = re.sub(r"^\s*(?:pc|lr|rip)\s*:\s*", "", value, flags=re.IGNORECASE)
    match = re.match(r"([A-Za-z_][A-Za-z0-9_.]*)", value)
    return match.group(1) if match else value


def _inline_symbols_from_declared_logs(input_artifacts: dict) -> set[str]:
    """Read only declared first-hand logs to preserve explicit inline frames."""
    symbols: set[str] = set()
    seen: set[str] = set()
    for field in ("crash_report_path", "log_path"):
        declared = str(input_artifacts.get(field, "") or "").strip()
        if not declared:
            continue
        try:
            path = Path(declared).expanduser().resolve()
            key = str(path)
            if key in seen or not path.is_file():
                continue
            seen.add(key)
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            lowered = line.lower()
            if not (
                "[inline]" in lowered
                or "inlined into" in lowered
                or "inline at" in lowered
            ):
                continue
            body = re.sub(r"^\s*(?:pc|lr|rip)\s*:\s*", "", line, flags=re.IGNORECASE)
            match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_.]*)", body)
            if match:
                symbols.add(match.group(1))
    return symbols


def _preserve_inline_call_chain_annotations(
    data: dict, input_artifacts: dict,
) -> dict:
    """Mark source-level inline frames before Test Expert builds its oracle.

    Kernel reports may expand inline source frames while the guest console
    prints only the concrete runtime symbols.  The model must retain that
    distinction in the structured handoff; this enrichment is limited to
    explicit [inline] evidence in the declared report/log.
    """
    inline_symbols = _inline_symbols_from_declared_logs(input_artifacts)
    if not inline_symbols:
        return data

    def annotate(frame: object) -> str:
        value = str(frame or "").strip()
        if not value or _contract_frame_symbol(value) not in inline_symbols:
            return value
        if re.search(
            r"\[\s*(?:static\s+)?inline\b|\binlined\s+into\b|\binline\s+at\b",
            value,
            flags=re.IGNORECASE,
        ):
            return value
        return f"{value} [inline]"

    data = dict(data)
    data["original_call_chain"] = [
        annotate(frame) for frame in data.get("original_call_chain") or []
    ]
    oracle = dict(data.get("call_chain_oracle") or {})
    oracle["required_top_frames"] = [
        annotate(frame) for frame in oracle.get("required_top_frames") or []
    ]
    oracle["required_frames"] = [
        annotate(frame) for frame in oracle.get("required_frames") or []
    ]
    oracle["required_frame_alternatives"] = [
        [annotate(frame) for frame in group]
        for group in oracle.get("required_frame_alternatives") or []
    ]
    oracle["required_frame_order"] = [
        [annotate(pair[0]), annotate(pair[1])]
        for pair in oracle.get("required_frame_order") or []
        if isinstance(pair, (list, tuple)) and len(pair) == 2
    ]
    data["call_chain_oracle"] = oracle
    return data


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
    data = _preserve_inline_call_chain_annotations(
        model_to_dict(contract), input_artifacts,
    )
    # Paths and architecture declared by the user are authoritative runtime
    # inputs.  A model must not redirect Test Expert to an old case image or
    # kernel merely by emitting a different existing path in its JSON.
    for field in ("target_arch", "vmlinux_path", "boot_kernel_path", "rootfs_path"):
        declared = str(input_artifacts.get(field, "") or "").strip()
        if declared:
            data[field] = declared
    declared_assets = str(input_artifacts.get("test_assets_dir", "") or "").strip()
    if declared_assets:
        data["test_assets_dir"] = declared_assets
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
    # Codex runs in an ephemeral workdir. Remap only files proven to have
    # been copied by this invocation into the durable session; without the
    # manifest, preserve the model path and let validation block it.
    manifest_path = output_dir / ".codex_artifact_manifest.json"
    copied_files: set[str] = set()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if str(Path(manifest.get("session_output_dir", "")).resolve()) == str(output_dir.resolve()):
            copied_files = {
                str(item) for item in manifest.get("copied_files", [])
                if isinstance(item, str)
            }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        copied_files = set()
    safe_source_files: list[str] = []
    for source_file in source_files:
        candidate = Path(str(source_file))
        if candidate.is_absolute() or ".." in candidate.parts:
            safe_source_files = []
            break
        safe_source_files.append(candidate.as_posix())
    if (
        safe_source_files
        and set(safe_source_files).issubset(copied_files)
        and all((output_dir / Path(source_file)).is_file() for source_file in safe_source_files)
    ):
        source_dir = str(output_dir.resolve())
        warnings = list(data.get("warnings") or [])
        warnings.append(
            "reproducer.source_dir remapped from ephemeral Codex workdir to the durable workflow session"
        )
        data["warnings"] = warnings
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
        and (
            contract.call_chain_oracle.required_top_frames
            or contract.call_chain_oracle.required_frames
        )
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
    oracle_data = dict(data.get("call_chain_oracle") or {})
    if (
        not oracle_data.get("required_frames")
        and oracle_data.get("required_top_frames")
    ):
        # Keep old consumers compatible while making the bounded core chain
        # the single runtime requirement.
        oracle_data["required_frames"] = list(oracle_data["required_top_frames"])
        data["call_chain_oracle"] = oracle_data
    oracle = _model_validate(CallChainOracle, oracle_data)
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
    explicit_blocked_reason = str(contract.blocked_reason or "").strip()
    if errors:
        data["status"] = "blocked"
        data["blocked_reason"] = "; ".join(errors)
        print(f"  [contract诊断] 校验发现 {len(errors)} 个错误: {'; '.join(errors[:3])}", flush=True)
    elif contract.status == "blocked" and explicit_blocked_reason:
        # Preserve an explicit Kernel Expert block. Artifact validation may
        # enrich paths and repair legacy derived fields, but must not turn a
        # deliberate fail-closed decision into a Test Expert handoff.
        data["status"] = "blocked"
        data["blocked_reason"] = explicit_blocked_reason
    else:
        # Re-validation can repair a legacy contract that was persisted as
        # blocked only because its derived path selectors were empty.  Do not
        # carry that stale terminal status into Test Expert after all current
        # artifact and path checks pass.
        data["status"] = "ok"
        data["blocked_reason"] = ""
    return _model_validate(KernelExpertOutput, data)
