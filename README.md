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
  libsnappy-dev libzstd-dev libgmp-dev libmpfr-dev cpio gzip git wget python3 python3-venv

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
| gcc-aarch64-linux-gnu | Build arm64 BusyBox | `apt install gcc-aarch64-linux-gnu` |
| Build tools | Build crash and BusyBox | `apt install build-essential bison flex patch texinfo file` |
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

## Input Format

```text
Bug Promote: 内核发生 Mutex ABBA 死锁导致 hung_task panic。...
vmcore: ./vmcore.elf
vmlinux: ./vmlinux
boot_kernel: ./bzImage
kernel_source: /path/to/linux
```

`Bug Promote` and `kernel_source` are required. Add whichever other artifacts
you have; `vmcore` and `vmlinux` enable crash analysis, while `boot_kernel`
enables QEMU verification.

## Workflow

```
Input → Validator → PM → ToolExperts (fan-out) → KernelExpert → TestExpert
                         │                                  ↑          │
                         └──────── expert_results ──────────┘          │
                                    └──── failed test (≤ 3) ───────────┘
                                              ↓
                                        KnowledgeBase → Result
```

The LangGraph state carries the original input, parsed artifacts, expert
results, reproduction contract, and test result through the workflow. PM selects
the applicable tool experts; their results are accumulated in `expert_results`
before KernelExpert receives them. A testable reproduction contract proceeds to
TestExpert. Failed tests return to KernelExpert until `max_test_attempts`
(default: 3); successful, exhausted, or non-testable cases are archived.

| Node | Responsibility | Output to the next stage |
|------|----------------|--------------------------|
| Validator | Checks that the problem is actionable; parses artifact paths, logs, and target architecture. | Validation and input-artifact contracts. |
| PM | Classifies the issue and chooses tool experts; always includes historical-case search. | Expert routing plan and issue ID. |
| ToolExperts | Run independently: `lock_analysis` diagnoses lock/hung-task issues; `crash_analysis` inspects vmcore and stack evidence; `kernel_log_analysis` extracts a failure timeline; `knowledge_search` finds similar cases. | Structured evidence and analysis summaries. |
| KernelExpert | Combines input and expert evidence, analyzes source code, and creates a reproducer and diagnosis. Claude Code is the default backend. | Reproduction contract: architecture, boot image, test script, and expected signal. |
| TestExpert | Boots the target kernel with QEMU and executes the reproducer for x86_64 or arm64. | Test contract, logs, and pass/fail result. |
| KnowledgeBase | Summarizes the evidence, reproducer, and test outcome; archives the case and optionally imports it into Chroma. | Knowledge-base document and final response. |

Each run creates `sessions/<session-id>/` with agent transcripts, metadata, and
generated reproducer files. Use `--session-id` to supply your own ID.

Lumen selects `crash_x86_64` or `crash_arm64` from
`Analysis-SKILL/tools/crash/` according to the target `vmlinux`. `deploy.sh`
builds both from crash source, and also builds static BusyBox binaries for both
architectures under `Analysis-SKILL/`.

## Project Structure

```
├── main.py                 # Entry point
├── deploy.sh               # One-click deployment
├── config.json.template    # LLM/workflow config template
├── agents/                 # LangGraph agent nodes
│   ├── backends.py         # LLM backend abstraction
│   ├── kernel_expert.py    # Kernel analysis & reproduction plan
│   ├── test_expert.py      # QEMU test execution
│   ├── qemu_tools.py       # QEMU tool wrappers
│   └── ...
├── graph/                  # LangGraph workflow graph
├── prompts/                # Agent system prompts
├── Analysis-SKILL/         # Git submodule: kernel analysis skills and tools
├── scripts/                # Project helper scripts
├── dev/tests/              # Unit and contract tests
├── sessions/               # Workflow output logs
└── knowledge_base/         # Archived reproduction cases
```
