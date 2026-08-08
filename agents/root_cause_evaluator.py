"""Deterministic root-cause and tool-evidence scoring.

This evaluator aligns the Kernel Expert contract with first-hand report/log
text and the declared source tree.  It never reads a user-supplied reproducer,
never turns a QEMU miss into a diagnosis failure, and does not call an LLM.
Human review remains authoritative for semantic accuracy.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any
from agents.semcode_path_analysis import _read_attested_source_snapshot


SCHEMA_VERSION = 1
_MAX_ARTIFACT_BYTES = 5 * 1024 * 1024
_GENERIC_FRAMES = {
    "show_stack", "__dump_stack", "dump_stack_lvl", "print_report",
    "kasan_report", "lock_acquire", "do_sys_open", "vfs_open",
    "do_dentry_open",
}


def evaluate_root_cause(state: dict[str, Any]) -> dict[str, Any]:
    """Build one auditable scorecard from the current workflow state."""
    contract = dict(state.get("kernel_contract") or {})
    artifacts = dict(state.get("input_artifacts_contract") or {})
    user_input = str(state.get("user_input") or "")
    report = _read_declared_text(artifacts.get("crash_report_path"))
    log = _read_declared_text(artifacts.get("log_path"))
    first_hand = "\n".join(part for part in (report, log) if part)
    declared_source_root = _path(artifacts.get("kernel_source_path"))
    expected_commit = str(artifacts.get("expected_kernel_commit") or "")
    source_root = _select_verified_source_root(
        state, declared_source_root, expected_commit,
    )
    attested_source_commit = ""
    manifest = artifacts.get("source_snapshot_manifest_path")
    if manifest:
        try:
            snapshot = _read_attested_source_snapshot(
                str(manifest), expected_kernel_commit=expected_commit,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        else:
            source_root = Path(str(snapshot["tree"]))
            attested_source_commit = str(snapshot["expected_commit"])
    root_cause = str(contract.get("root_cause") or "").strip()
    evidence = _dicts(contract.get("root_cause_evidence"))
    if not evidence:
        # Versioned Codex contracts may retain the source-backed list under
        # the additive ``evidence`` field after the workflow has flattened
        # ``root_cause``.  Reuse only structured entries; no source claim is
        # synthesized here.
        evidence = _dicts(contract.get("evidence"))
    oracle = dict(contract.get("call_chain_oracle") or {})
    producer_frontier = _normalise_producer_frontier(contract)

    observed = _observed_facts(
        user_input=user_input, report_text=first_hand,
        contract=contract, oracle=oracle,
    )
    source_audit = _audit_source_evidence(
        source_root, evidence, str(artifacts.get("expected_kernel_commit") or ""),
    )
    if attested_source_commit:
        source_audit["source_commit_matches"] = True
        source_audit["source_attested"] = True
    fix_audit = _audit_fix_evidence(artifacts, source_root)
    dimensions = _score_dimensions(
        root_cause=root_cause, observed=observed,
        source_audit=source_audit, fix_audit=fix_audit,
        first_hand_text=first_hand,
    )
    score = _normalised_score(dimensions)
    root_status = _root_status(
        score, dimensions, source_audit, observed, producer_frontier,
    )
    diagnosis_status = _diagnosis_status(root_status, producer_frontier)
    public_fix_audit = dict(fix_audit)
    patch_text = str(public_fix_audit.pop("patch_text", "") or "")
    public_fix_audit["patch_bytes"] = len(patch_text.encode("utf-8"))
    return {
        "schema_version": SCHEMA_VERSION,
        "method": "deterministic evidence alignment; human review remains authoritative",
        "root_cause": {
            "status": root_status,
            "accuracy_score": score,
            "dimensions": dimensions,
            "conclusion": root_cause,
            "diagnosis_status": diagnosis_status,
            "limitations": _root_limitations(
                first_hand_text=first_hand, source_root=source_root,
                fix_audit=fix_audit, root_cause=root_cause,
            ),
        },
        "case_evidence": {
            "report_path": str(artifacts.get("crash_report_path") or ""),
            "log_path": str(artifacts.get("log_path") or ""),
            "source_path": str(source_root or artifacts.get("kernel_source_path") or ""),
            "declared_source_path": str(declared_source_root or artifacts.get("kernel_source_path") or ""),
            "expected_kernel_commit": expected_commit,
            "source_head": source_audit.get("source_head", ""),
            "source_commit_matches": source_audit.get("source_commit_matches"),
            "observed": observed,
            "producer_frontier": producer_frontier,
            "producer_gate": {
                "status": producer_frontier["status"],
                "passed": producer_frontier["gate_passed"],
                "source_evidence_count": len(producer_frontier["source_evidence"]),
                "runtime_evidence_count": len(producer_frontier["runtime_evidence"]),
                "unresolved_prerequisites": producer_frontier["unresolved_prerequisites"],
            },
            "source_audit": source_audit,
            "fix_audit": public_fix_audit,
        },
        "tool_experts": _score_tool_experts(
            state.get("expert_results") or [],
            root_cause=root_cause, observed=observed,
            source_audit=source_audit,
        ),
        "reproduction": {
            "test_passed": bool(state.get("test_passed", False)),
            "call_chain_consistent": bool(state.get("call_chain_consistent", False)),
            "attempts": int(state.get("test_attempts") or 0),
            "status": (state.get("test_contract") or {}).get("status") or (
                "reproduced" if state.get("test_passed") else "not_reproduced"
            ),
            "code": (state.get("test_contract") or {}).get("code", ""),
        },
        "diagnosis_status": diagnosis_status,
    }


def _path(value: Any) -> Path | None:
    if not value:
        return None
    try:
        return Path(str(value)).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _select_verified_source_root(
    state: dict[str, Any], declared_source_root: Path | None,
    expected_commit: str,
) -> Path | None:
    """Use the exact detached source scope proven by Semcode when available.

    The input path may be a shared checkout whose HEAD is intentionally newer
    than the benchmark commit. Kernel Expert records the pinned worktree in
    ``semcode_path_analysis.scope.source_domains``; using that root for
    evidence verification avoids reporting a false source mismatch while
    retaining the declared path for audit.
    """
    expected = str(expected_commit or "").strip().lower()
    if not expected:
        return declared_source_root
    candidate_roots: list[Path] = []
    analysis = state.get("semcode_path_analysis") or {}
    scope = analysis.get("scope") if isinstance(analysis, dict) else None
    domains = scope.get("source_domains") if isinstance(scope, dict) else None
    if isinstance(domains, list):
        for domain in domains:
            if isinstance(domain, dict):
                candidate = _path(domain.get("root"))
                if candidate is not None:
                    candidate_roots.append(candidate)

    # P0 cases do not require the optional path-analysis contract, but the
    # deterministic Semcode adapter still archives its exact pinned checkout.
    # Read only that durable identity; source content is verified below by the
    # normal file/commit audit and never inferred from the JSON.
    session_dir = _path(state.get("session_dir"))
    if session_dir is not None:
        for evidence_path in (
            session_dir / "semcode-evidence.json",
            session_dir / "evidence" / "semcode-evidence.json",
        ):
            try:
                payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if payload.get("status") != "ok" or payload.get("failures"):
                continue
            candidate = _path(payload.get("kernel_source"))
            if candidate is not None:
                candidate_roots.append(candidate)

    seen: set[Path] = set()
    for candidate in candidate_roots:
        if candidate in seen or not candidate.is_dir():
            continue
        seen.add(candidate)
        head = _git_head(candidate).lower()
        if head and (head == expected or head.startswith(expected)):
            return candidate
    return declared_source_root


def _read_declared_text(value: Any) -> str:
    """Read an explicitly declared report/log only."""
    path = _path(value)
    if path is None or not path.is_file():
        return ""
    try:
        with path.open("rb") as handle:
            return handle.read(_MAX_ARTIFACT_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in (value or []) if isinstance(item, dict)]


def _label(text: str, name: str) -> str:
    match = re.search(rf"(?im)^\s*{re.escape(name)}\s*:\s*(.+?)\s*$", text or "")
    return match.group(1).strip().strip("'\"") if match else ""


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item or "").strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _normalise_producer_frontier(contract: dict[str, Any]) -> dict[str, Any]:
    """Normalize the producer gate without inferring a missing producer.

    Contracts written before the producer-frontier field are explicitly marked
    ``not_applicable`` for compatibility.  New contracts must opt into a
    verified/partial/missing/blocked state and provide evidence for a verified
    claim; the evaluator never reads a user-provided reproducer to fill this
    boundary.
    """
    if "producer_frontier" not in contract:
        return {
            "status": "not_applicable", "target_function": "", "ingress": "",
            "source_evidence": [], "runtime_evidence": [], "required_setup": [],
            "unresolved_prerequisites": [], "rationale": "legacy contract without producer gate",
            "gate_passed": True,
        }
    raw = contract.get("producer_frontier")
    raw = raw if isinstance(raw, dict) else {}
    allowed = {"verified", "partial", "missing", "blocked", "not_applicable"}
    status = str(raw.get("status") or "missing").strip().lower()
    if status not in allowed:
        status = "blocked"
    source_evidence = _dicts(raw.get("source_evidence"))
    runtime_evidence = _dicts(raw.get("runtime_evidence"))
    gate_passed = status in {"verified", "not_applicable"} and (
        status == "not_applicable" or bool(source_evidence or runtime_evidence)
    )
    if status == "verified" and not gate_passed:
        status = "partial"
    return {
        "status": status,
        "target_function": str(raw.get("target_function") or ""),
        "ingress": str(raw.get("ingress") or ""),
        "source_evidence": source_evidence,
        "runtime_evidence": runtime_evidence,
        "required_setup": [str(item) for item in (raw.get("required_setup") or []) if str(item).strip()],
        "unresolved_prerequisites": [
            str(item) for item in (raw.get("unresolved_prerequisites") or []) if str(item).strip()
        ],
        "rationale": str(raw.get("rationale") or ""),
        "gate_passed": gate_passed,
    }


def _observed_facts(
    *, user_input: str, report_text: str,
    contract: dict[str, Any], oracle: dict[str, Any],
) -> dict[str, Any]:
    entry = next(
        (
            _label(user_input, field)
            for field in ("entry_point", "fault_function", "function", "target_function")
            if _label(user_input, field)
        ),
        "",
    )
    if not entry:
        match = re.search(
            r"(?i)\b(?:fault|crash|oops|warning)\s+(?:in|at)\s+"
            r"([A-Za-z_][A-Za-z0-9_]*)",
            user_input,
        )
        entry = match.group(1) if match else ""
    if not entry:
        match = re.search(
            r"(?i)\b(?:read|write|access)\s+(?:in|at)\s+"
            r"([A-Za-z_][A-Za-z0-9_]*)",
            user_input,
        )
        entry = match.group(1) if match else ""
    top_frames = [
        str(item) for item in (
            oracle.get("required_top_frames")
            or oracle.get("required_frames")
            or contract.get("original_call_chain")
            or []
        ) if str(item).strip()
    ]
    evidence_functions = [
        str(item.get("function") or "")
        for item in _dicts(contract.get("root_cause_evidence"))
        if item.get("function")
    ]
    fault_functions = _dedupe([
        entry,
        *[name for name in evidence_functions
          if name not in _GENERIC_FRAMES and "inline" not in name],
    ])
    signatures = [
        str(item) for item in (oracle.get("fault_signatures") or [])
        if str(item).strip()
    ]
    signals = _dedupe([
        *re.findall(
            r"(?i)\b(?:KASAN|UBSAN|WARNING|BUG|Oops|panic|general protection)\b[^\n:]*",
            report_text,
        ),
        *re.findall(
            r"(?i)\b(?:use-after-free|slab-use-after-free|NULL pointer|paging request)\b",
            report_text,
        ),
    ])
    lowered = report_text.lower()
    return {
        "entry_point": entry,
        "fault_functions": fault_functions,
        "top_frames": _dedupe(top_frames),
        "fault_signatures": signatures,
        "signals": signals,
        "report_present": bool(report_text.strip()),
        "function_hits_in_report": {
            name: bool(re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
                report_text,
            ))
            for name in fault_functions
        },
        # KASAN is a sanitizer, not a diagnosis: it also reports wild
        # pointers, out-of-bounds accesses, and invalid frees. Treat a case
        # as UAF only when the first-hand log explicitly identifies a freed
        # object/lifetime violation; otherwise pointer cases would be scored
        # against the wrong mechanism vocabulary.
        "uaf_signal": bool(re.search(
            r"use[- ]after[- ]free|slab-use-after-free|freed\s+(?:object|allocation|stack)|\bUAF\b",
            lowered,
        )),
        # Ignore SSH/transport chatter such as "Warning: Permanently added";
        # only a kernel-style WARNING line or an explicit assertion selects
        # the warning mechanism vocabulary.
        "warning_signal": bool(
            re.search(r"(?m)^\s*WARNING\b", report_text)
            or re.search(r"\bwarn_on\b|\bassert(?:ion)?\b", lowered)
        ),
        "pointer_signal": bool(re.search(
            r"general protection|paging request|null pointer|invalid address", lowered,
        )),
        "oob_signal": bool(re.search(
            r"out[- ]of[- ]bounds|slab[- ]out[- ]of[- ]bounds", lowered,
        )),
    }


def _git_head(source_root: Path | None) -> str:
    if source_root is None or not source_root.is_dir():
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            check=False, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _audit_source_evidence(
    source_root: Path | None, evidence: list[dict[str, Any]],
    expected_commit: str,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    verified = 0
    for item in evidence:
        rel = str(item.get("file") or "").strip()
        function = str(item.get("function") or "").strip()
        # Evidence lists also carry first-hand log references and runtime
        # artifact paths.  They are audit records, not source claims; counting
        # them in the denominator made a correct source-grounded diagnosis
        # look unsupported.  Only declared source-domain files are scored.
        source_domain = str(item.get("source_domain") or "").strip().lower()
        if not rel or rel.startswith(("evidence/", "tryouts/")) or (
            source_domain and source_domain not in {"kernel", "source"}
        ):
            continue
        line = item.get("line")
        check: dict[str, Any] = {
            "function": function, "file": rel, "line": line, "verified": False,
        }
        if source_root is not None and rel:
            declared_path = Path(rel).expanduser()
            # External attested snapshots may record absolute source paths.
            # Accept them only when they resolve inside the already selected
            # verified source tree; tree-external paths remain unscored.
            candidate = (
                declared_path.resolve()
                if declared_path.is_absolute()
                else (source_root / declared_path).resolve()
            )
            source_root = source_root.resolve()
            try:
                inside_tree = candidate.is_relative_to(source_root)
            except AttributeError:
                inside_tree = str(candidate).startswith(str(source_root))
            if inside_tree and candidate.is_file():
                try:
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                    line_ok = not isinstance(line, int) or 1 <= line <= len(text.splitlines())
                    if not function:
                        function_ok = True
                    elif function.startswith("struct "):
                        function_ok = function in text
                    elif candidate.suffix.lower() in {".rst", ".md"}:
                        function_ok = function in text
                    else:
                        function_ok = bool(re.search(
                            rf"(?<![A-Za-z0-9_]){re.escape(function)}\s*\(", text,
                        ))
                    check.update({
                        "path": str(candidate), "line_in_file": line_ok,
                        "function_in_file": function_ok,
                        "verified": bool(line_ok and function_ok),
                    })
                except OSError as exc:
                    check["error"] = str(exc)
        if check["verified"]:
            verified += 1
        checks.append(check)
    head = _git_head(source_root)
    expected = expected_commit.strip().lower()
    matches = None if not expected else bool(head and (
        head.lower() == expected or head.lower().startswith(expected)
    ))
    return {
        "source_exists": bool(source_root and source_root.is_dir()),
        "source_head": head,
        "expected_commit": expected_commit,
        "source_commit_matches": matches,
        "evidence_total": len(checks),
        "evidence_verified": verified,
        "checks": checks,
    }


def _walk_json(value: Any, path: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            yield child, str(key), item
            yield from _walk_json(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_json(item, f"{path}[{index}]")


def _audit_fix_evidence(
    artifacts: dict[str, Any], source_root: Path | None,
) -> dict[str, Any]:
    """Find explicit fix/patch metadata without treating source commit as fix."""
    report_path = _path(artifacts.get("crash_report_path"))
    case_dir = report_path.parent if report_path else None
    candidates: list[dict[str, Any]] = []
    declared_fix_commit = str(artifacts.get("fix_commit") or "").strip()
    if declared_fix_commit:
        candidates.append({
            "file": "input.txt", "field": "fix_commit",
            "value": declared_fix_commit,
        })
    declared_patch_path = str(artifacts.get("fix_patch_path") or "").strip()
    if declared_patch_path:
        candidates.append({
            "file": "input.txt", "field": "fix_patch_path",
            "value": declared_patch_path,
        })
    if case_dir and case_dir.is_dir():
        for metadata in sorted(case_dir.glob("*.json")):
            try:
                payload = json.loads(metadata.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for key_path, key, value in _walk_json(payload):
                key_lower = key.lower()
                explicit_fix_key = (
                    key_lower in {
                        "fix_commit", "fixed_commit", "patch_commit",
                        "upstream_fix", "fix_patch", "patch_path",
                        "patch_file", "fix_patch_path", "upstream_patch",
                    }
                    or key_lower.endswith(("_fix_commit", "_patch_commit", "_patch_path"))
                )
                if not explicit_fix_key:
                    continue
                if isinstance(value, (str, int, float)) and str(value).strip():
                    candidates.append({
                        "file": str(metadata), "field": key_path, "value": value,
                    })
    patch_text = ""
    fix_commits: list[str] = []
    for item in candidates:
        field = str(item.get("field") or "").lower()
        value = str(item.get("value") or "").strip()
        if "commit" in field and re.fullmatch(r"[0-9a-fA-F]{7,40}", value):
            fix_commits.append(value)
            if source_root is not None:
                try:
                    result = subprocess.run(
                        ["git", "-C", str(source_root), "show", "--format=", "--no-ext-diff", value],
                        check=False, capture_output=True, text=True, timeout=20,
                    )
                except (OSError, subprocess.SubprocessError):
                    result = None
                if result is not None and result.returncode == 0:
                    patch_text += result.stdout
        if "patch" in field:
            patch_path = _path(value)
            if patch_path is None and report_path is not None:
                patch_path = (report_path.parent / value).resolve()
            if patch_path is not None and patch_path.is_file():
                try:
                    patch_text += patch_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
    return {
        "available": bool(candidates),
        "candidates": candidates,
        "fix_commits": fix_commits,
        "patch_text": patch_text,
        "patch_files": [
            item for item in candidates
            if isinstance(item.get("value"), str)
            and (
                str(item["value"]).endswith((".patch", ".diff"))
                or "patch" in str(item["field"]).lower()
            )
        ],
        "source_commit_used_as_fix": False,
        "note": (
            "No explicit fix commit/patch was declared; expected_kernel_commit "
            "is treated only as the source snapshot."
            if not candidates else
            "Fix/patch metadata was found and is listed for human review."
        ),
    }


def _contains_any(text: str, terms: list[str]) -> int:
    lowered = (text or "").lower()
    return sum(1 for term in terms if term.lower() in lowered)


def _score_dimensions(
    *, root_cause: str, observed: dict[str, Any],
    source_audit: dict[str, Any], fix_audit: dict[str, Any],
    first_hand_text: str,
) -> list[dict[str, Any]]:
    lowered = root_cause.lower()
    entry = str(observed.get("entry_point") or "")
    fault_functions = list(observed.get("fault_functions") or [])
    fault_hits = sum(
        1 for name in fault_functions
        if name and re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
            root_cause,
        )
    )
    fault_score = 25 if entry and entry.lower() in lowered else min(25, fault_hits * 8)
    if fault_score and not observed.get("report_present"):
        fault_score = min(fault_score, 15)

    if observed.get("uaf_signal"):
        terms = [
            "free", "release", "ref", "lifetime", "race", "lock", "kfree",
            "uaf", "stale", "ownership",
        ]
    elif observed.get("oob_signal"):
        terms = [
            "buffer", "bounds", "length", "size", "index", "sentinel",
            "terminator", "read", "write", "response",
        ]
    elif observed.get("warning_signal"):
        terms = ["warning", "assert", "condition", "lock", "invariant", "check", "race"]
    elif observed.get("pointer_signal"):
        terms = [
            "pointer", "dereference", "null", "invalid", "address",
            "memory", "register", "fault",
        ]
    else:
        terms = [
            "free", "release", "ref", "lifetime", "race", "lock",
            "invariant", "assert", "warning", "condition", "pointer",
        ]
    mechanism_hits = _contains_any(lowered, terms)
    evidence_total = int(source_audit.get("evidence_total") or 0)
    evidence_verified = int(source_audit.get("evidence_verified") or 0)
    phenomenon_terms = _dedupe([
        *[str(item) for item in (observed.get("signals") or [])],
        *[str(item) for item in (observed.get("top_frames") or []) if len(str(item)) > 4],
    ])
    phenomenon_hits = _contains_any(lowered, phenomenon_terms)
    # Contract frames commonly include offsets (``foo+0x10/0x20``), while a
    # root-cause sentence names the source symbol (``foo()``).  Compare the
    # normalized symbols as well so the call-chain mapping measures semantic
    # coverage rather than punctuation.
    normalized_frame_hits = 0
    for frame in observed.get("top_frames") or []:
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", str(frame))
        if match and match.group(1).lower() in lowered:
            normalized_frame_hits += 1
    phenomenon_hits += normalized_frame_hits
    if first_hand_text and any(
        name.lower() in first_hand_text.lower() and name.lower() in lowered
        for name in fault_functions
    ):
        phenomenon_hits += 1
    dimensions = [
        {
            "id": "fault_site", "label": "故障点与原始入口",
            "score": fault_score, "max_score": 25,
            "reason": f"原始入口/关键函数命中 {fault_hits}/{max(1, len(fault_functions))}。",
        },
        {
            "id": "mechanism", "label": "根因机制",
            "score": min(30, mechanism_hits * 6), "max_score": 30,
            "reason": f"与现象类型匹配的机制词命中 {mechanism_hits} 个。",
        },
        {
            "id": "source_grounding", "label": "源码证据",
            "score": round(25 * evidence_verified / evidence_total) if evidence_total else 0,
            "max_score": 25,
            "reason": f"源码证据校验通过 {evidence_verified}/{evidence_total}。",
        },
        {
            "id": "phenomenon_mapping", "label": "现象/调用链对应",
            "score": min(20, phenomenon_hits * 4), "max_score": 20,
            "reason": f"原始报告信号或调用链命中 {phenomenon_hits} 个。",
        },
    ]
    if fix_audit.get("available"):
        patch_text = str(fix_audit.get("patch_text") or "")
        patch_hits = _contains_any(
            patch_text.lower(),
            [
                str(item) for item in (observed.get("fault_functions") or [])
                if str(item).strip()
            ] + [
                term for term in (
                    "free", "release", "ref", "lifetime", "race",
                    "lock", "ownership", "invariant", "assert",
                ) if term in lowered
            ],
        )
        dimensions.append({
            "id": "fix_alignment", "label": "修复补丁对应",
            "score": min(10, patch_hits * 2) if patch_text else 0,
            "max_score": 10,
            "reason": (
                f"修复/补丁文本与故障函数/机制词命中 {patch_hits} 个。"
                if patch_text else
                "发现修复/补丁元数据，但未能读取补丁内容，需人工核对。"
            ),
        })
    else:
        dimensions.append({
            "id": "fix_alignment", "label": "修复补丁对应",
            "score": None, "max_score": 0,
            "reason": "没有显式修复提交或补丁，本维度不计分。",
        })
    return dimensions


def _normalised_score(dimensions: list[dict[str, Any]]) -> int:
    scored = [
        item for item in dimensions
        if item.get("max_score", 0) and item.get("score") is not None
    ]
    maximum = sum(int(item["max_score"]) for item in scored)
    total = sum(int(item["score"]) for item in scored)
    return round(100 * total / maximum) if maximum else 0


def _root_status(
    score: int, dimensions: list[dict[str, Any]],
    source_audit: dict[str, Any], observed: dict[str, Any],
    producer_frontier: dict[str, Any],
) -> str:
    source_ok = bool(source_audit.get("evidence_verified"))
    fault_ok = any(
        item.get("id") == "fault_site" and (item.get("score") or 0) >= 15
        for item in dimensions
    )
    if (
        score >= 80 and source_ok and fault_ok and observed.get("report_present")
        and producer_frontier.get("gate_passed", False)
    ):
        return "supported"
    if score >= 50 or (source_ok and fault_ok and observed.get("report_present")):
        return "partially_supported"
    return "insufficient_evidence"


def _diagnosis_status(root_status: str, producer_frontier: dict[str, Any]) -> str:
    """Expose whether diagnosis is closed independently of reproduction."""
    producer_status = str(producer_frontier.get("status") or "missing")
    if producer_status in {"missing", "blocked", "partial"}:
        return "root_cause_analyzed_producer_unresolved"
    if root_status == "supported":
        return "root_cause_supported"
    if root_status == "partially_supported":
        return "root_cause_partially_supported"
    return "root_cause_insufficient_evidence"


def _root_limitations(
    *, first_hand_text: str, source_root: Path | None,
    fix_audit: dict[str, Any], root_cause: str,
) -> list[str]:
    limitations: list[str] = []
    if not first_hand_text:
        limitations.append("没有可读取的原始报告或日志，无法核对现象。")
    if source_root is None or not source_root.is_dir():
        limitations.append("没有可读取的隔离内核源码，无法完成源码证据校验。")
    if not fix_audit.get("available"):
        limitations.append(
            "没有显式修复提交/补丁；expected_kernel_commit 仅作为源码快照处理。"
        )
    if not root_cause:
        limitations.append("Kernel Expert 未给出结构化根因结论。")
    return limitations


def _score_tool_experts(
    results: list[Any], *, root_cause: str, observed: dict[str, Any],
    source_audit: dict[str, Any],
) -> list[dict[str, Any]]:
    scores: list[dict[str, Any]] = []
    key_terms = _dedupe([
        str(observed.get("entry_point") or ""),
        *[str(item) for item in (observed.get("top_frames") or [])],
        *[str(item) for item in (observed.get("fault_functions") or [])],
    ])
    root_terms = [
        term for term in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", root_cause)
        if term.lower() not in {"this", "that", "before", "after", "exact"}
    ]
    for result in results:
        item = result if isinstance(result, dict) else {}
        structured = item.get("structured_output") or {}
        if not isinstance(structured, dict):
            structured = {}
        text = "\n".join([
            str(item.get("analysis_output") or ""),
            str(structured.get("summary") or ""),
            json.dumps(structured.get("evidence") or [], ensure_ascii=False),
        ])
        lowered = text.lower()
        status = str(structured.get("status") or "degraded")
        evidence_hits = [term for term in key_terms if term and term.lower() in lowered]
        root_hits = [term for term in root_terms if term.lower() in lowered]
        conflict_hits, conflict_reasons = _conflict_count(text, observed, source_audit)
        if (
            status in {"failed", "blocked"}
            or "调用失败" in text
            or "validationerror" in lowered
        ):
            accuracy = min(15, len(evidence_hits) * 3)
            role = "unavailable"
        else:
            accuracy = min(
                100,
                20
                + min(35, len(evidence_hits) * 7)
                + min(30, len(root_hits) * 5)
                - min(45, conflict_hits * 15),
            )
            role = "supporting" if conflict_hits == 0 else "contradictory"
        contribution = max(
            0,
            min(100, len(evidence_hits) * 8 + len(root_hits) * 6 - conflict_hits * 25),
        )
        scores.append({
            "expert_type": str(item.get("expert_type") or structured.get("expert_type") or ""),
            "expert_name": str(item.get("expert_name") or structured.get("expert_name") or ""),
            "status": status,
            "accuracy_score": accuracy,
            "root_cause_contribution_score": contribution,
            "role": role,
            "evidence_hits": evidence_hits,
            "root_cause_term_hits": root_hits,
            "conflict_hits": conflict_hits,
            "conflict_reasons": conflict_reasons,
            "method_note": (
                "分数表示与一手证据/Kernel Expert 结论的文本对齐程度，"
                "不替代人工语义复核。"
            ),
        })
    return scores


def _conflict_count(
    text: str, observed: dict[str, Any], source_audit: dict[str, Any],
) -> tuple[int, list[str]]:
    lowered = text.lower()
    entry = str(observed.get("entry_point") or "").lower()
    count = 0
    reasons: list[str] = []
    if entry and entry not in lowered and any(
        marker in lowered for marker in ("analysis", "根因", "root cause", "可能")
    ):
        count += 1
        reasons.append("declared entry point is absent from the expert analysis")
    # Generic source-domain check: when an expert names a concrete source
    # path for the declared entry but omits the exact source file recorded by
    # the Kernel Expert evidence, retain that as a contradiction signal.
    expected_files = {
        str(item.get("file") or "").lower()
        for item in (source_audit.get("checks") or [])
        if item.get("file")
    }
    source_paths = re.findall(
        r"\b(?:drivers|fs|net|kernel|mm|include|arch)/[A-Za-z0-9_./-]+\.c\b",
        lowered,
    )
    if entry and entry in lowered and source_paths and expected_files:
        expected_names = {Path(item).name for item in expected_files}
        named_names = {Path(item).name for item in source_paths}
        if expected_names.isdisjoint(named_names):
            count += 1
            reasons.append("named source path does not match the exact-source evidence")
    return count, reasons
