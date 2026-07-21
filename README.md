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
| Claude Code CLI | Default KernelExpert backend | `npm install -g @anthropic-ai/claude-code` |
| Embedding endpoint | Full RAG historical-case retrieval and Chroma import | OpenAI-compatible `/v1/embeddings`, e.g. Ollama |
| semcode-mcp | Semantic code search | Built by `deploy.sh` under `Analysis-SKILL/tools/semcode/` |
| Analysis-SKILL | Kernel tools and skills | `git submodule update --init --recursive` |

## Configuration

1. Edit `.env` with your LLM settings, then run `source .env` in each new
   shell. The template includes default RAG embedding settings; adjust them if
   you use a non-default embedding service.

```bash
export ANTHROPIC_API_KEY="sk-..."
export EMBEDDING_BASE_URL="http://localhost:11434/v1"
export EMBEDDING_MODEL="bge-large-zh"
export EMBEDDING_API_KEY="not-required"
```

`knowledge_search` and `knowledge_base` use the embedding endpoint for full RAG
retrieval/import. If it is unavailable, the workflow continues with degraded
historical-case search and Chroma import.

2. `config.json` is generated from `config.json.template`. The default chat
backend is Anthropic-compatible; `kernel_expert` uses Claude Code. OpenAI,
HTTP, and OpenCode backends are also configurable there.

Backend 说明：

| Backend | 作用与适用场景 |
|---------|----------------|
| `anthropic` | 普通 Anthropic-compatible 聊天 API，适合 validator、PM、工具专家和知识库总结；默认可连接 DeepSeek Anthropic 兼容接口。 |
| `claude_code` | Claude Code CLI agent loop，提供文件读写、编译、Shell 和多轮工具调用；Kernel Expert 的默认 backend。 |
| `opencode` | 可替代 Claude Code 的 CLI agent loop，适用于需要独立 provider 或 API 配置的 Kernel Expert 部署。 |

Kernel Expert 必须使用 `claude_code` 或 `opencode`；普通 `anthropic` backend
不支持其 `workdir`/`add_dirs` 文件操作接口。

KernelExpert's Claude Code settings file is configurable through
`agents.kernel_expert.settings_file` (default: `~/.claude/settings.json`). Set
an independent settings file for a separate API-key/profile when running
parallel analyses; the backend passes it to Claude Code with `--settings`.
KernelExpert must use the `claude_code` or `opencode` agent-loop backend:
the workflow passes `workdir` and `add_dirs` to `invoke()` so the agent can
construct and validate PoC artifacts. The plain `anthropic` backend is not
compatible with this interface.

With a local Claude Code installation, run the online backend smoke test with:

```bash
venv/bin/pytest -m online --run-online dev/tests/test_claude_code_online.py
```

The test uses `LUMEN_CLAUDE_SETTINGS_FILE` (default:
`~/.claude/settings.json`) and `LUMEN_KERNEL_SOURCE` (default:
`~/linux-next`), checks the local `workdir`/`add_dirs` agent-loop contract, and
does not run a kernel or QEMU case.

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

## Workflow

```
Input → Validator → PM → ToolExperts (fan-out) → KernelExpert Claude loop → KnowledgeBase → Result
                         │                         (analyse → PoC → persistent SSH QEMU)
                         └────────────────── expert results + raw user log ─────────────────────┘
```

The LangGraph state carries the original input, parsed artifacts, expert
results, reproduction contract, and test result through the workflow. PM selects
the applicable tool experts; their results are accumulated in `expert_results`
before KernelExpert receives them. The original `log:` path and each tool
expert's persisted result-file path are passed independently; the Claude loop
reads the original evidence on demand rather than receiving copied summaries.
One Claude Code loop then analyses, creates a PoC, and invokes the deterministic
persistent-QEMU SSH runner. Every pass, failure, and blocked result is archived.

| Node | Responsibility | Output to the next stage |
|------|----------------|--------------------------|
| Validator | Checks that the problem is actionable; parses artifact paths, logs, and target architecture. | Validation and input-artifact contracts. |
| PM | Deterministically routes by available evidence: always searches historical cases; log evidence adds `kernel_log_analysis`; crash evidence adds `crash_analysis`; lockup/RCU/hung-task evidence uses `lock_analysis` instead of duplicate crash analysis. | Expert routing plan and issue ID. |
| ToolExperts | Run independently: `lock_analysis` diagnoses lock/hung-task issues; `crash_analysis` inspects vmcore and stack evidence; `kernel_log_analysis` extracts a failure timeline; `knowledge_search` finds similar cases. | Structured evidence and analysis summaries. |
| KernelExpert | In one Claude Code loop, combines raw logs and expert evidence, analyzes source, creates a PoC, then requests deterministic verification. If the final structured contract is empty or malformed, it retries once inside the same loop; otherwise it blocks. | PoC contract plus the runner-owned test contract. |
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

## Project Structure

```
├── main.py                 # Entry point
├── deploy.sh               # One-click deployment
├── config.json.template    # LLM/workflow config template
├── agents/                 # LangGraph agent nodes
│   ├── backends.py         # LLM backend abstraction
│   ├── kernel_expert.py    # Claude analysis → PoC → verification loop
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
