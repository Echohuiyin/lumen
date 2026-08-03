# Lumen — Linux Kernel Maintenance Workflow

LangGraph multi-agent workflow for x86_64 and arm64 Linux kernel bug analysis,
reproduction, validation, and knowledge-base archiving.

> **New to Lumen?** Start with the [Step-by-step Deployment Guide](DEPLOYMENT.md)
> for a thorough walkthrough from bare metal to first analysis run.

## Quick Start

```bash
# 1. Fetch the bundled kernel-analysis skills
git submodule update --init --recursive

# 2. Install system dependencies (Ubuntu/Debian)
sudo apt install qemu-system-x86 qemu-system-arm gcc-aarch64-linux-gnu \
  build-essential bison flex patch texinfo file libncurses-dev zlib1g-dev liblzo2-dev \
  libsnappy-dev libzstd-dev libgmp-dev libmpfr-dev cpio gzip git wget python3 python3-venv \
  debootstrap qemu-user-static binfmt-support openssh-client

# 3. Deploy (creates venv, installs deps, and generates config)
bash deploy.sh

# 4. Edit .env with your LLM and embedding settings, then prepare the input
source venv/bin/activate
source .env
cp input.txt.template input.txt
# Edit input.txt, then run:
python3 main.py input.txt --config config.json
```

## Requirements

| Dependency | Purpose | Install |
|-----------|---------|---------|
| Python 3.10+ | Runtime | `apt install python3 python3-venv` |
| QEMU (x86_64 / arm64) | Kernel boot and reproduction testing | `apt install qemu-system-x86 qemu-system-arm` |
| SSH / debootstrap / qemu-user-static | Build and access persistent Debian QEMU guests | `apt install openssh-client debootstrap qemu-user-static binfmt-support` |
| gcc-aarch64-linux-gnu | Build arm64 BusyBox | `apt install gcc-aarch64-linux-gnu` |
| Build tools | Build crash and BusyBox | `apt install build-essential bison flex patch texinfo file` |
| e2fsprogs | Build ext4 QEMU rootfs images | `apt install e2fsprogs` |
| crash build libraries | Build crash for both targets | `apt install libncurses-dev zlib1g-dev liblzo2-dev libsnappy-dev libzstd-dev libgmp-dev libmpfr-dev` |
| cpio / gzip | Initramfs packaging | `apt install cpio gzip` |
| git / wget | Fetch sources during deployment | `apt install git wget` |
| Codex CLI + bubblewrap | KernelExpert agent-loop backend and sandbox | `npm install -g @openai/codex`; `apt install bubblewrap` |
| Embedding endpoint | Full RAG historical-case retrieval and Chroma import | Any configured OpenAI-compatible `/v1/embeddings` service |
| semcode-mcp | Semantic code search | Built by `deploy.sh` under `Analysis-SKILL/tools/semcode/` |
| Analysis-SKILL | Kernel tools and skills | `git submodule update --init --recursive` |

## Configuration

1. Edit `.env` with your provider settings, then run `source .env` in each new
   shell. API keys, endpoints, models, and embedding settings are deployment
   inputs; the repository supplies no provider-specific defaults.

```bash
export ANTHROPIC_API_KEY="<provider-api-key>"
export ANTHROPIC_BASE_URL="<provider-anthropic-compatible-endpoint>"
export ANTHROPIC_MODEL="<provider-model>"
export EMBEDDING_BASE_URL="<embedding-endpoint>"
export EMBEDDING_MODEL="<embedding-model>"
export EMBEDDING_API_KEY="<embedding-api-key>"
```

`knowledge_search` and `knowledge_base` use the configured embedding endpoint for
full RAG retrieval/import. If it is missing or unavailable, configuration is
reported explicitly; Lumen does not substitute another provider or account.

2. `config.json` is generated from `config.json.template`. The default chat
backend is Anthropic-compatible; `kernel_expert` is fixed to the project-
isolated Codex backend. Other backends remain available to non-Kernel-Expert
roles only; there is no Kernel Expert backend, account, or model fallback.

Backend 说明：

| Backend | 作用与适用场景 |
|---------|----------------|
| `anthropic` | 普通 Anthropic-compatible 聊天 API，适合 validator、PM、工具专家和知识库总结；端点和模型由部署配置提供。 |
| `codex` | 项目隔离的 Codex CLI agent loop；Kernel Expert 的唯一 backend，负责读取源码与日志、使用 Semcode 并生成用户态 C reproducer。 |

Kernel Expert 必须使用 `codex`；`anthropic`、`claude_code`、`opencode` 或其他
provider/backend/model 均不得作为该角色的 fallback。

Kernel Expert uses `codex exec` with an ignored project runtime HOME under
`runtime/codex-home`. `deploy.sh` copies an existing Codex `auth.json` with
mode 600 (or accepts invocation-scoped `CODEX_API_KEY`) and exposes repository
skills through `.agents/skills`. User config/rules and plugin/app/multi-agent
extensions are disabled. Semcode MCP is required, and the kernel source is
readable but is never added as a writable Codex workspace. The deploy script
records the detected Codex executable in `.env`; source `.env` before running
Lumen so user-level npm installations remain portable across login shells.

With a local Codex installation, run the online backend smoke test with:

```bash
venv/bin/pytest -m online --run-online dev/tests/test_codex_online.py
```

The test requires isolated Codex authentication and `LUMEN_KERNEL_SOURCE`,
checks the read-only-source/session-output contract, and does not run QEMU.

## Input Format

```text
Bug Promote: 内核发生 Mutex ABBA 死锁导致 hung_task panic。...
vmcore: ./vmcore.elf
vmlinux: ./vmlinux
log: ./kernel.log
boot_kernel: ./bzImage
kernel_source: /path/to/linux
```

`Bug Promote` and `kernel_source` are required. Add whichever other artifacts
you have; at least one readable `vmcore` or `log` is required. When `log` is
absent, Lumen extracts it from `vmcore` plus `vmlinux` into the session and
passes that generated log path to KernelExpert. `boot_kernel` enables QEMU
verification.

QEMU deployment inputs are configurable and are not tied to a developer's
machine:

- `LUMEN_QEMU_IMAGE_ROOT`: persistent image/key root (default:
  `<project>/runtime/qemu-ssh`).
- `LUMEN_QEMU_RUNTIME_ROOT`: per-session writable scratch root (use tmpfs or
  NVMe when the project filesystem is constrained).
- `LUMEN_QEMU_SSH_USER`: guest account matching the provisioned key (default:
  `root`).
- `LUMEN_QEMU_GUEST_WORKDIR`: guest-side POC directory (default:
  `/tmp/lumen-poc`).
- `LUMEN_QEMU_DEFAULT_SMP`: deployment default vCPU count used only when the
  contract leaves `qemu_recipe.smp` empty (default: `2`; explicit contract
  values always win). This is useful for older kernels that cannot boot
  reliably with secondary CPUs under QEMU.
- `LUMEN_SESSION_ROOT`: durable workflow session-artifact root (default:
  `<project>/sessions`). Set it to a writable scratch filesystem when the
  project filesystem is constrained; the session ID layout is preserved.
- `LUMEN_OUTPUT_DIR`: non-session expert-output root (default:
  `/tmp/lumen_outputs`). This is useful for tests and one-off diagnostics on
  hosts where `/tmp` is backed by a full project volume.

The runner validates these values before starting QEMU and keeps the original
userspace-C and call-chain contracts unchanged.

## Workflow

```
Input → Validator → PM → ToolExperts (fan-out) → KernelExpert Codex loop → KnowledgeBase → Result
                         │                         (analyse → PoC → persistent SSH QEMU)
                         └────────────────── expert results + raw user log ─────────────────────┘
```

The LangGraph state carries the original input, parsed artifacts, expert
results, reproduction contract, and test result through the workflow. PM selects
the applicable tool experts; their results are accumulated in `expert_results`
before KernelExpert receives them. The original `log:` path and each tool
expert's persisted result-file path are passed independently; the Codex loop
reads the original evidence on demand rather than receiving copied summaries.
One Codex loop then analyses, creates a PoC, and invokes the deterministic
persistent-QEMU SSH runner. Every pass, failure, and blocked result is archived.

| Node | Responsibility | Output to the next stage |
|------|----------------|--------------------------|
| Validator | Checks that the problem is actionable; parses artifact paths, logs, and target architecture. | Validation and input-artifact contracts. |
| PM | Deterministically routes by available evidence: always searches historical cases; log evidence adds `kernel_log_analysis`; crash evidence adds `crash_analysis`; lockup/RCU/hung-task evidence uses `lock_analysis` instead of duplicate crash analysis. | Expert routing plan and issue ID. |
| ToolExperts | Run independently: `lock_analysis` diagnoses lock/hung-task issues; `crash_analysis` inspects vmcore and stack evidence; `kernel_log_analysis` extracts a failure timeline; `knowledge_search` finds similar cases. | Structured evidence and analysis summaries. |
| KernelExpert | In one project-isolated Codex loop, combines raw logs and expert evidence, analyzes source through required Semcode MCP, and creates a userspace C PoC. If the final structured contract is empty or malformed, it retries once; otherwise it blocks. | PoC contract plus the runner-owned test contract. |
| Persistent QEMU runner | Reuses only a guest with the same kernel/rootfs/architecture/recipe identity; uploads the PoC through loopback SSH and evaluates host serial evidence. | SSH output, serial log, and pass/fail/blocked evidence. |
| KnowledgeBase | Summarizes the evidence, reproducer, and test outcome; archives the case and optionally imports it into Chroma. | Knowledge-base document and final response. |

Each run creates `sessions/<session-id>/` with agent transcripts, metadata, and
generated reproducer files. Verification uses structured `execution_steps`; no
user-provided `test.sh` is executed. Use `--session-id` to supply your own ID.

Lumen selects `crash_x86_64` or `crash_arm64` from
`Analysis-SKILL/tools/crash/` according to the target `vmlinux`. `deploy.sh`
builds both from crash source, and also builds static BusyBox binaries for both
architectures under `Analysis-SKILL/`. `deploy.sh` also creates ignored Debian
SSH images under `runtime/qemu-ssh/` for x86_64 and arm64; QEMU remains alive
across PoC iterations for the same immutable guest identity. The guest images
include SSH, basic networking, common diagnostics, and the compiler runtime
used by PoC validation (`curl`, `iproute2`, `net-tools`, `strace`, `gcc`,
`libc6-dev`, `make`, and related base packages). Git and GDB are not installed
in the guest by default: the runner does not clone sources or perform
interactive debugging inside the VM.


On hosts where the project filesystem is also receiving large benchmark assets,
set `LUMEN_QEMU_RUNTIME_ROOT` to a writable scratch filesystem before running
E2E cases. It relocates only per-try-out writable images and QEMU runtime logs;
the source-controlled project paths and the immutable base images remain
configurable and unchanged. Leave it unset to keep the default `sessions/`
layout.

## Project Structure

```
├── main.py                 # Entry point
├── deploy.sh               # One-click deployment
├── config.json.template    # LLM/workflow config template
├── agents/                 # LangGraph agent nodes
│   ├── backends.py         # LLM backend abstraction
│   ├── kernel_expert.py    # Codex analysis → PoC → verification loop
│   ├── persistent_qemu.py  # Persistent QEMU lifecycle and SSH execution
│   ├── qemu_tools.py       # Legacy one-shot QEMU wrappers
│   └── ...
├── graph/                  # LangGraph workflow graph
├── prompts/                # Agent system prompts
├── Analysis-SKILL/         # Git submodule: kernel analysis skills and tools
├── scripts/                # Project helper scripts
├── dev/tests/              # Unit and contract tests
├── sessions/               # Workflow output logs
└── knowledge_base/         # Archived reproduction cases
```
