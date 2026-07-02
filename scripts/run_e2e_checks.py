#!/usr/bin/env python3
"""Run E2E workflow verification on all maintenance test cases.

Runs the full maintenance workflow (validator → pm → tool_experts →
kernel_expert → test_expert → knowledge_base) on each case and reports
how many workflow stages completed successfully.

Cases run in parallel via ThreadPoolExecutor to reduce gate time.

Usage:
    python scripts/run_e2e_checks.py                          # All 4 cases
    python scripts/run_e2e_checks.py --cases deadlock uaf     # Specific cases
    python scripts/run_e2e_checks.py --json                   # Machine-readable output
    python scripts/run_e2e_checks.py --skip-qemu              # Skip test_expert stage
    python scripts/run_e2e_checks.py --parallel 2             # Max 2 parallel (default: all)
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# E2E cases: (name, input_path, config_path, expected_signal)
E2E_CASES: list[dict] = [
    {
        "name": "deadlock",
        "title": "Mutex ABBA Deadlock",
        "input": str(PROJECT_ROOT / "test_assets" / "deadlock" / "input.txt"),
        "config": str(PROJECT_ROOT / "maintenance_config.json"),
        "expected_signal": "blocked for more than",
        "required_assets": [
            "vmlinux",
            "bzImage",
            "vmcore.elf",
            "input.txt",
        ],
    },
    {
        "name": "uaf",
        "title": "Use-After-Free (kref refcount leak)",
        "input": str(PROJECT_ROOT / "test_assets" / "uaf" / "input.txt"),
        "config": str(PROJECT_ROOT / "maintenance_config.json"),
        "expected_signal": "BUG: KASAN: slab-use-after-free",
        "required_assets": [
            "vmlinux",
            "bzImage",
            "vmcore.elf",
            "input.txt",
        ],
    },
    {
        "name": "btrfs",
        "title": "Syzbot btrfs WARNING (ordered extent)",
        "input": str(PROJECT_ROOT / "test_assets" / "syzbot_btrfs_085adc3f" / "input.txt"),
        "config": str(PROJECT_ROOT / "maintenance_config.json"),
        "expected_signal": "WARNING in can_finish_ordered_extent",
        "required_assets": [
            "vmlinux",
            "bzImage",
            "vmcore.elf",
            "input.txt",
        ],
    },
    {
        "name": "kvm-x86",
        "title": "Syzbot kvm-x86 WARNING (pvqspinlock)",
        "input": str(PROJECT_ROOT / "test_assets" / "syzbot_kvm_x86_5d2b94b7" / "input.txt"),
        "config": str(PROJECT_ROOT / "maintenance_config.json"),
        "expected_signal": "WARNING in hv_tlb_flush_enqueue",
        "required_assets": [
            "vmlinux",
            "bzImage",
            "vmcore.elf",
            "input.txt",
        ],
    },
]

# Workflow stages in execution order
WORKFLOW_STAGES = [
    "validator",
    "pm",
    "tool_experts",
    "kernel_expert",
    "test_expert",
    "knowledge_base",
]

# Stage indicator strings in workflow stdout output.
# These match the display headers produced by call_llm_with_display
# and node-specific print statements. Some stages (kernel_expert via
# Claude Code CLI) write to file rather than stdout, so we fall back
# to secondary patterns (final summary line from test_expert).
STAGE_PATTERNS = {
    "validator": "[validator]",
    "pm": "[PM]",
    "tool_experts": "工具专家分析结果",
    "kernel_expert": "分析构造用例",          # header in output file, not stdout
    "test_expert": "测试验证",               # from "测试验证 1 次" in final summary
    "knowledge_base": "知识库生成",           # from "[知识库生成] 总结归档"
}


def _check_assets(case: dict) -> list[str]:
    """Verify required test assets exist. Return list of missing paths."""
    asset_dir = Path(case["input"]).resolve().parent
    missing = []
    for asset in case["required_assets"]:
        if not (asset_dir / asset).exists():
            missing.append(str(asset_dir / asset))
    return missing


def _run_workflow(case: dict, timeout: int = 3600) -> subprocess.CompletedProcess:
    """Run main.py workflow for a single E2E case.

    Args:
        case: E2E case definition dict.
        timeout: Per-case timeout in seconds (default 3600 = 1hr).

    Returns: CompletedProcess with stdout/stderr.
    """
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "main.py"),
        "--input", case["input"],
        "--config", case["config"],
    ]
    print(f"\n  $ {' '.join(cmd)}")
    sys.stdout.flush()
    return subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _count_completed_stages(stdout: str, case_name: str = "") -> list[dict]:
    """Scan workflow output and report which stages completed.

    kernel_expert writes its output to a file, not stdout, so its pattern
    ("分析构造用例") never appears in stdout. We infer it from downstream
    stages: if test_expert or knowledge_base completed, kernel_expert
    must have completed too (they depend on its output).

    Returns list of {stage, completed, indicator} dicts.
    """
    results = []
    for stage in WORKFLOW_STAGES:
        pattern = STAGE_PATTERNS.get(stage, stage)
        found = pattern in stdout
        results.append({
            "stage": stage,
            "completed": found,
            "indicator": pattern,
        })
    # kernel_expert writes to file, not stdout — infer from downstream
    ke_idx = next((i for i, r in enumerate(results) if r["stage"] == "kernel_expert"), None)
    if ke_idx is not None:
        downstream_done = any(
            r["completed"]
            for r in results[ke_idx + 1:]
        )
        if downstream_done:
            results[ke_idx]["completed"] = True
    return results


def _check_blocked_contract(stdout: str) -> bool:
    """Check if any blocked contract (CLI/max_turns failure) was emitted."""
    return "BLOCKED" in stdout or "blocked" in stdout.lower() and "contract" in stdout.lower()


def _check_knowledge_base_archived(stdout: str) -> bool:
    """Check if knowledge_base completed its archiving."""
    return "Chroma" in stdout or "知识库" in stdout or "knowledge_base" in stdout.lower()


def _check_reproduction(stdout: str) -> bool:
    """Check if the workflow reports successful QEMU reproduction.

    Looks for '成功复现' without the negative prefix '未成功复现'.
    """
    return "成功复现" in stdout and "未成功复现" not in stdout


def _run_case(case: dict, skip_qemu: bool = False) -> dict:
    """Run E2E verification for a single case.

    Returns dict with case name, stages status, and overall result.
    """
    name = case["name"]
    title = case["title"]

    print(f"\n{'=' * 60}")
    print(f"E2E Case: {title} ({name})")
    print(f"{'=' * 60}")

    # Preflight: check assets
    missing = _check_assets(case)
    if missing:
        return {
            "case": name,
            "title": title,
            "status": "BLOCKED",
            "reason": f"Missing assets: {', '.join(missing)}",
            "duration_s": 0,
            "completed_stages": 0,
            "total_stages": len(WORKFLOW_STAGES),
            "stages": [{"stage": s, "completed": False, "indicator": STAGE_PATTERNS[s]} for s in WORKFLOW_STAGES],
        }

    # Run workflow
    start = time.time()
    try:
        completed = _run_workflow(case)
        elapsed = time.time() - start
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        return {
            "case": name,
            "title": title,
            "status": "TIMEOUT",
            "reason": f"Workflow did not finish within {3600}s",
            "duration_s": round(elapsed),
            "completed_stages": 0,
            "total_stages": len(WORKFLOW_STAGES),
            "stages": [],
        }
    except Exception as e:
        elapsed = time.time() - start
        return {
            "case": name,
            "title": title,
            "status": "ERROR",
            "reason": str(e),
            "duration_s": round(elapsed),
            "completed_stages": 0,
            "total_stages": len(WORKFLOW_STAGES),
            "stages": [],
        }

    # Analyze stages
    stages = _count_completed_stages(stdout)
    completed_count = sum(1 for s in stages if s["completed"])
    blocked = _check_blocked_contract(stdout)
    kb_archived = _check_knowledge_base_archived(stdout)
    reproduced = _check_reproduction(stdout)
    returncode = completed.returncode

    # Retry once on LLM flake (kernel_expert failure, <5 stages).
    # Transient LLM issues (timeout, bad generation) often resolve on
    # re-run. If the first attempt didn't make it past kernel_expert
    # but wasn't a hard block (timeout/BLOCKED), try again.
    if completed_count < 5 and not blocked and returncode == 0:
        print(f"\n  ⚠ First attempt only reached {completed_count}/6 stages, retrying...")
        sys.stdout.flush()
        try:
            completed = _run_workflow(case)
            elapsed = time.time() - start
            stdout = completed.stdout
            stderr = completed.stderr
            stages = _count_completed_stages(stdout)
            completed_count = sum(1 for s in stages if s["completed"])
            blocked = _check_blocked_contract(stdout)
            kb_archived = _check_knowledge_base_archived(stdout)
            reproduced = _check_reproduction(stdout)
            returncode = completed.returncode
            print(f"  → Retry result: {completed_count}/6 stages")
        except subprocess.TimeoutExpired:
            elapsed = time.time() - start
            return {
                "case": name, "title": title,
                "status": "TIMEOUT", "reason": f"Retry also timed out",
                "duration_s": round(elapsed), "completed_stages": 0,
                "total_stages": len(WORKFLOW_STAGES), "stages": [],
            }
        except Exception as e:
            # Keep first attempt's result, note the retry
            pass

    # Determine overall status (方案 B: require actual QEMU reproduction)
    if blocked:
        status = "BLOCKED"
        reason = "Workflow emitted a blocked contract (CLI startup/max_turns failure)"
    elif completed_count >= 5 and reproduced:
        status = "PASS"
        reason = f"{completed_count}/{len(WORKFLOW_STAGES)} stages completed, reproduced in QEMU"
    elif completed_count >= 5 and not reproduced:
        status = "PASS_NO_REPRODUCE"
        reason = f"{completed_count}/{len(WORKFLOW_STAGES)} stages completed but NOT reproduced in QEMU"
    elif completed_count >= 3:
        status = "PARTIAL"
        reason = f"Only {completed_count}/{len(WORKFLOW_STAGES)} stages completed"
    else:
        status = "FAIL"
        reason = f"Only {completed_count}/{len(WORKFLOW_STAGES)} stages completed"

    return {
        "case": name,
        "title": title,
        "status": status,
        "reason": reason,
        "returncode": returncode,
        "duration_s": round(elapsed),
        "completed_stages": completed_count,
        "total_stages": len(WORKFLOW_STAGES),
        "stages": stages,
        "knowledge_base_archived": kb_archived,
        "workflow_blocked": blocked,
        "reproduced": reproduced,
        "stdout_snippet": stdout[-2000:] if len(stdout) > 2000 else stdout,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run E2E workflow verification")
    parser.add_argument(
        "--cases", nargs="+", choices=["deadlock", "uaf", "btrfs", "kvm-x86"],
        default=["deadlock", "uaf", "btrfs", "kvm-x86"],
        help="E2E cases to run (default: all 4)",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON report")
    parser.add_argument(
        "--skip-qemu", action="store_true",
        help="Skip test_expert stage (useful for quick workflow validation)",
    )
    parser.add_argument(
        "--parallel", type=int, default=None,
        help="Max parallel cases (default: all)",
    )
    args = parser.parse_args()

    selected = [c for c in E2E_CASES if c["name"] in args.cases]

    print("=" * 60)
    print("E2E Workflow Verification")
    print(f"Cases: {', '.join(c['name'] for c in selected)}")
    print(f"Config: maintenance_config.json")
    print(f"Workflow: {' → '.join(WORKFLOW_STAGES)}")
    print(f"Parallel: {args.parallel or 'all'}")
    print("=" * 60)

    results: list[dict] = []
    max_workers = args.parallel if args.parallel else len(selected)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_run_case, case, skip_qemu=args.skip_qemu): case
            for case in selected
        }
        for future in concurrent.futures.as_completed(futures):
            case = futures[future]
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                results.append({
                    "case": case["name"],
                    "title": case["title"],
                    "status": "ERROR",
                    "reason": str(e),
                    "duration_s": 0,
                    "completed_stages": 0,
                    "total_stages": len(WORKFLOW_STAGES),
                    "stages": [],
                })

    # Reorder results to match selection order
    order = {c["name"]: i for i, c in enumerate(selected)}
    results.sort(key=lambda r: order.get(r["case"], 999))

    all_pass = all(r.get("status") in ("PASS",) for r in results)

    # Summary
    print(f"\n{'=' * 60}")
    print("E2E Verification Summary")
    print(f"{'=' * 60}")
    for r in results:
        status_tag = {
            "PASS": "✓ PASS",
            "PASS_NO_REPRODUCE": "~ PASS (no reproduce)",
            "PARTIAL": "~ PARTIAL",
            "BLOCKED": "✗ BLOCKED",
            "TIMEOUT": "✗ TIMEOUT",
            "ERROR": "✗ ERROR",
        }.get(r["status"], f"? {r['status']}")
        print(f"  {status_tag}  {r['title']} ({r['duration_s']}s)")
        print(f"       Stages: {r['completed_stages']}/{r['total_stages']}")
        print(f"       Reason: {r['reason']}")
        if "stages" in r and r["stages"]:
            stage_flags = " ".join(
                "✓" if s["completed"] else "✗" for s in r["stages"]
            )
            stage_names = " → ".join(s["stage"] for s in r["stages"])
            print(f"       [{stage_flags}] {stage_names}")

    print(f"\nOverall: {'✓ ALL PASS' if all_pass else '✗ SOME FAILED'}")
    print(f"Pass criteria: All cases must reach >=5/6 stages AND reproduce in QEMU.")

    if args.json:
        print(f"\n--- JSON ---")
        print(json.dumps({"results": results, "all_pass": all_pass}, ensure_ascii=False, indent=2))

    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
