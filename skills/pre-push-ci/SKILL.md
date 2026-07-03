---
name: pre-push-ci
description: Run pre-push CI gating checks for the Lumen project — static analysis, agent contract validation, unit tests, full pytest suite, and E2E workflow verification (mutex deadlock + UAF). Use this skill when the user mentions 'push', 'commit', '门禁', 'CI', 'pre-push', 'gating', or wants to verify code before pushing.
---

# Pre-Push CI Gating Skill

Run automated checks before pushing code to ensure the maintenance workflow agents, contracts, tools, and end-to-end pipeline are not broken.

## When to Use

Trigger this skill when user asks to:
- Run pre-push CI gating
- Check if code is ready to push
- Run tests before committing
- Verify agent contracts and capabilities
- E2E 测试验证（mutex 死锁 + UAF）
- "跑一下门禁", "提交前检查", "跑测试"

## Quick Usage

```
/pre-push-ci [--full] [--online] [--e2e]
```

- **Default** (no flags): Static checks + offline unit tests (~30s)
- `--full`: All of the above + full pytest suite (~2min)
- `--online`: Include online tests that call LLM APIs (~5min, may cost credits)
- `--e2e`: Run full E2E workflow on all 4 cases (~20-40min, requires LLM + QEMU)
- `--all`: Everything (static → unit → full pytest → online → e2e)

## Check Stages

### Stage 1: Static Analysis

```bash
# Python syntax check
python -m compileall -q agents graph scripts tests/

# Agent contract & capability validation
python scripts/check_agent_contracts.py
```

Checks:
- [ ] All Python files compile (syntax check)
- [ ] Agent capabilities match runtime tool factories
- [ ] Prompt files reference correct tools and terms
- [ ] Config.json tool_experts match agent_capabilities.json
- [ ] No stale tool claims in prompts

### Stage 2: Offline Unit Tests

```bash
# Fast contract and rule tests (no QEMU, no LLM)
python -m pytest tests/test_agent_contracts.py -v
python -m pytest tests/test_validator_rules.py -v
python -m pytest tests/test_pm_rules.py -v
python -m pytest tests/test_kernel_contract.py -v
python -m pytest tests/test_test_runner_contract.py -v
python -m pytest tests/test_tool_evidence.py -v
```

Or run all at once:

```bash
# Static checks + 6 offline test modules
python scripts/run_static_checks.py
```

Checks:
- [ ] Agent contracts produce valid field schemas
- [ ] Validator rules correctly classify kernel issues
- [ ] PM router selects correct expert paths
- [ ] Kernel contract validation (artifacts, fields)
- [ ] Test runner contract handles all result codes
- [ ] Tool evidence format is parseable

### Stage 3: Full Pytest Suite (--full)

```bash
# All offline + agent capability tests
python -m pytest tests/ -v --ignore=tests/test_tool_expert_mcp.py --run-online -x
```

Additional tests:
- `test_agent_capabilities.py` — Agent capability matrix (逐一调用真实 LLM + vmcore 验证)
- `test_agent_tool_calling.py` — Tool dispatch correctness
- `test_expert_io_format.py` — Expert I/O format contracts
- `test_kernel_expert.py` — Kernel expert state machine
- `test_test_expert.py` — Test expert routing
- `test_qemu_tools.py` — QEMU tool mocks
- `test_tool_experts.py` — Tool expert orchestration

**注意**：`test_agent_capabilities.py` 会调用真实 LLM API + 加载 vmcore，耗时约 20-30 分钟。`--full` 会包含它，如需跳过用无 flag 默认门禁即可。

### Stage 4: Online Tests (--online)

```bash
# Tests that call live LLM APIs or external session
python -m pytest tests/ -m online -v
```

Coverage:
- LLM-based tool calling
- MCP server connectivity (semcode, aicrasher)
- Real crash session creation

### Stage 5: E2E Workflow Verification (--e2e)

Verify the full LangGraph pipeline end-to-end on **4 test cases running in parallel**. Each run goes through all 6 workflow stages: **validator → pm → tool_experts → kernel_expert → test_expert → knowledge_base**.

```bash
# E2E test script (handles all 4 cases, checks results)
python scripts/run_e2e_checks.py
```

Or run individually:

```bash
# Mutex ABBA deadlock E2E
python main.py --input test_assets/deadlock/input.txt --config maintenance_config.json

# Use-after-free (kref refcount leak) E2E
python main.py --input test_assets/uaf/input.txt --config maintenance_config.json
```

#### E2E Test Cases

##### Case 1: Mutex ABBA Deadlock

| Field | Value |
|-------|-------|
| **Input** | `test_assets/deadlock/input.txt` |
| **Fault type** | deadlock (mutex ABBA) |
| **Kernel module** | `mutex_abba_deadlock.ko` |
| **Expected signal** | `blocked for more than` → `Kernel panic` |
| **Config** | `maintenance_config.json` |

The deadlock case tests the workflow's ability to:
1. Parse a vmcore with two threads blocked on mutex ABBA deadlock
2. Route to lock_analysis + crash_analysis tool experts
3. Generate a reproducer (test.sh + kernel module or shell script)
4. Boot in QEMU and verify the deadlock triggers via hung_task panic

##### Case 2: Use-After-Free (kref leak)

| Field | Value |
|-------|-------|
| **Input** | `test_assets/uaf/input.txt` |
| **Fault type** | uaf (kref refcount leak) |
| **Kernel module** | `crash_uaf.ko` |
| **User trigger** | `uaf_trigger` (static binary) |
| **Expected signal** | `BUG: KASAN: slab-use-after-free` |
| **Config** | `maintenance_config.json` |

The UAF case tests the workflow's ability to:
1. Parse a vmcore with KASAN use-after-free report
2. Route to crash_analysis tool expert
3. Trace the kref refcount leak through ioctl sequences
4. Generate a reproducer and verify in QEMU

##### Case 3: Syzbot btrfs WARNING (ordered extent)

| Field | Value |
|-------|-------|
| **Input** | `test_assets/syzbot_btrfs_085adc3f/input.txt` |
| **Fault type** | warning |
| **Kernel module** | C reproducer (syzbot PoC) |
| **Expected signal** | `WARNING in can_finish_ordered_extent` |
| **Config** | `maintenance_config.json` |

The btrfs case tests the workflow's ability to:
1. Parse a WARNING vmcore without CONFIG_IKCONFIG
2. Route through kernel_log_analysis + crash_analysis
3. Compile and run a syzbot C reproducer
4. Boot in QEMU and confirm the ordered-data WARNING triggers

##### Case 4: Syzbot kvm-x86 WARNING (pvqspinlock)

| Field | Value |
|-------|-------|
| **Input** | `test_assets/syzbot_kvm_x86_5d2b94b7/input.txt` |
| **Fault type** | warning |
| **Kernel module** | C reproducer (syzbot PoC, multi-threaded) |
| **Expected signal** | `WARNING in hv_tlb_flush_enqueue` |
| **Config** | `maintenance_config.json` |

The kvm-x86 case tests the workflow's ability to:
1. Parse a pvqspinlock corruption WARNING from nested KVM+HyperV
2. Route to crash_analysis + kernel_log_analysis
3. Generate a multi-threaded syzbot reproducer
4. Boot in QEMU with smp≥2 and trigger the race via KVM hypercalls

#### E2E Pass Criteria

All cases must meet both criteria:

| Criterion | Requirement |
|-----------|------------|
| **Pipeline** | >=5/6 workflow stages execute successfully |
| **Reproduction** | main.py stdout contains `成功复现` (i.e. `final_response` says "问题分析已完成（成功复现）。") |

A case that completes the pipeline but fails to reproduce gets `PASS_NO_REPRODUCE` status and **blocks the gate**.

| Status | Meaning | Gate |
|--------|---------|------|
| `PASS` | Pipeline complete + QEMU reproduced | ✅ Pass |
| `PASS_NO_REPRODUCE` | Pipeline complete but NOT reproduced | ❌ Block |
| `BLOCKED` | Blocked contract (CLI failure) | ❌ Block |
| `PARTIAL` / `FAIL` | <5 stages | ❌ Block |

#### Result Verification

After running, check for these indicators in the output:

```bash
# Check that knowledge_base archived (terminal node reached)
grep -r "知识库\|knowledge_base\|archived\|Chroma" outputs/latest/

# Check for blocked contracts (CLI failures)
grep -r "blocked\|BLOCKED" outputs/latest/

# Check the final result from main.py
# Expected: "工作流已完成" with knowledge_base output
```

## E2E Automation Script

The project provides a convenience script at `scripts/run_e2e_checks.py` that runs all 4 E2E cases in parallel and reports results:

```bash
# Run all 4 cases in parallel (default)
python scripts/run_e2e_checks.py

# Select specific cases
python scripts/run_e2e_checks.py --cases deadlock uaf
python scripts/run_e2e_checks.py --cases btrfs kvm-x86

# Control parallelism
python scripts/run_e2e_checks.py --parallel 2     # Max 2 at a time

# Output detailed results as JSON
python scripts/run_e2e_checks.py --json
```

## Test Infrastructure Reference

### Project Structure

```
lumen/
├── agents/                   # Workflow agent implementations
│   ├── backends.py           # LLM backend (Claude Code, OpenAI)
│   ├── contracts.py          # Agent input/output contracts
│   ├── kernel_expert.py      # Kernel expert node
│   ├── kernel_tools.py       # Kernel tools (write_file, compile_module)
│   ├── knowledge_base.py     # Knowledge base archiving
│   ├── qemu_tools.py         # QEMU boot/log analysis tools
│   ├── test_runner.py        # QEMU test plan executor
│   └── test_expert.py        # Test expert node
├── scripts/
│   ├── run_static_checks.py  # Static check orchestrator
│   ├── check_agent_contracts.py  # Agent capability validator
│   └── run_e2e_checks.py     # E2E workflow verifier
├── tests/                    # 15 pytest test modules
├── test_assets/              # E2E test case assets
│   ├── deadlock/             # Mutex ABBA deadlock case
│   ├── uaf/                  # Use-after-free case
│   ├── syzbot_btrfs_085adc3f/   # BTRFS case (WIP)
│   └── syzbot_kvm_x86_5d2b94b7  # KVM-x86 case
├── config.json               # Workflow configuration
├── maintenance_config.json   # E2E workflow configuration
├── agent_capabilities.json   # Agent capability matrix
└── main.py                   # E2E workflow entry point
```

### Test Execution Time Estimates

| Stage | Duration | Dependencies |
|-------|----------|-------------|
| Static analysis | ~10s | Python ≥3.10 |
| Offline unit tests | ~20s | pytest |
| Full pytest suite | ~2min | pytest |
| Online tests | ~5min | LLM API key, MCP servers |
| E2E (deadlock) | ~10-20min | LLM API key, Claude Code CLI, QEMU |
| E2E (UAF) | ~10-20min | LLM API key, Claude Code CLI, QEMU |

### Quick Smoke Test

For the fastest feedback loop (before every push):

```bash
python scripts/run_static_checks.py
```

This runs all Stage 1 + Stage 2 checks in ~30 seconds. If it passes, the code is safe to push.

For a full pre-merge gate:

```bash
python scripts/run_static_checks.py && python scripts/run_e2e_checks.py
```

## Error Handling

### Stage Failed — What to Check

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `compileall` errors | Syntax error or missing import | `python -c "import <module>"` to isolate |
| Agent contract mismatch | Prompt/agent_capabilities.json out of sync | Update `agent_capabilities.json` or fix prompt |
| Pytest assertion failure | Contract/rule behavior changed | Check test for outdated expectation |
| `ModuleNotFoundError` | Missing dependency | `pip install -r requirements.txt` |
| E2E workflow blocked | CLI config, MCP, or API key issue | Check logs in `outputs/<case>/` |
| E2E kernel_expert empty | LLM timeout or max_turns | Increase cli_timeout/max_turns in config |
| E2E test_expert QEMU stuck | Missing bzImage or wrong kernel config | Verify test_assets kernel paths exist |
| E2E Chroma import fail | Chroma/sqlite3 version mismatch | Check `python -c "import chromadb"` |

### Known Flaky Tests

None currently. Online tests (`-m online`) and E2E tests may fail without API key, MCP server, or QEMU.

## E2E Preflight Checklist

Before running `--e2e`, verify:

- [ ] `maintenance_config.json` exists and has correct paths
- [ ] `test_assets/deadlock/bzImage` and `test_assets/deadlock/vmlinux` exist
- [ ] `test_assets/uaf/bzImage` and `test_assets/uaf/vmlinux` exist
- [ ] QEMU is installed (`which qemu-system-x86_64`)
- [ ] Claude Code CLI is available (`which claude`)
- [ ] LLM API key is configured (in settings.json)
- [ ] `~/.claude/settings.json` has correct permission mode

## Output Format

The skill produces a CI summary like:

```
=== Pre-Push CI Results ===

Stage 1 — Static Analysis:              PASS
Stage 2 — Offline Unit Tests:           PASS (6/6)
Stage 3 — Full Pytest Suite:            PASS (12/12)
Stage 4 — Online Tests:                 SKIPPED (use --online)
Stage 5 — E2E (deadlock):               ✓ PASS (reproduced)
Stage 5 — E2E (uaf):                    ~ PASS (no reproduce)
Stage 5 — E2E (btrfs):                  ✓ PASS (reproduced)
Stage 5 — E2E (kvm-x86):                ✓ PASS (reproduced)

=== All checks passed. Ready to push. ===
```
