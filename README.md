# Lumen — Linux Kernel Maintenance Workflow

LangGraph multi-agent workflow for Linux kernel bug analysis, reproduction, and
knowledge base archiving.

> **New to Lumen?** Start with the [Step-by-step Deployment Guide](DEPLOYMENT.md)
> for a thorough walkthrough from bare metal to first analysis run.

## Quick Start

```bash
# 1. Install system dependencies (Ubuntu/Debian)
sudo apt install qemu-system-x86 busybox-static crash cpio git python3 python3-venv

# 2. Deploy (creates venv, installs deps, generates config)
bash deploy.sh

# 3. Edit .env with your API key and kernel source path, then:
source venv/bin/activate
python3 main.py test_assets/deadlock/input.txt --config config.json
```

## Requirements

| Dependency | Purpose | Install |
|-----------|---------|---------|
| Python 3.10+ | Runtime | `apt install python3 python3-venv` |
| QEMU (x86_64) | Kernel boot testing | `apt install qemu-system-x86` |
| busybox-static | Initramfs creation | `apt install busybox-static` |
| crash | Vmcore analysis | `apt install crash` |
| cpio / gzip | Initramfs packaging | `apt install cpio gzip` |
| claude CLI | Claude Code backend | `npm install -g @anthropic-ai/claude-code` |
| semcode-mcp | Semantic code search (optional) | Build from [semcode](https://github.com/anthropics/semcode) |
| Kernel source | Crash symbol resolution | Clone your kernel tree, set `KERNEL_SOURCE_DIR` |

## Configuration

1. Edit `.env` with your API key and kernel source path:

```bash
export ANTHROPIC_API_KEY="sk-..."
export KERNEL_SOURCE_DIR="${HOME}/code/linux-mainline"
```

2. Or edit `config.json` directly after it's generated from template.

## Input Format

```text
Bug Promote: 内核发生 Mutex ABBA 死锁导致 hung_task panic。...
vmcore: ./vmcore.elf
vmlinux: ./vmlinux
boot_kernel: ./bzImage
kernel_source: $KERNEL_SOURCE_DIR
```

Only `Bug Promote` is required. Paths support `${VAR}` and `$VAR` env vars.

## Workflow

```
Validator → PM → ToolExpert (parallel) → KernelExpert → TestExpert → KnowledgeBase
```

- **Validator**: validates input is complete
- **PM**: plans analysis, selects tool experts
- **ToolExpert**: crash analysis, kernel log analysis, lock analysis, knowledge search
- **KernelExpert**: constructs kernel reproduction plan (uses Claude Code CLI)
- **TestExpert**: runs QEMU kernel test, verifies expected signal
- **KnowledgeBase**: archives reproduction case

## Project Structure

```
├── main.py                 # Entry point
├── deploy.sh               # One-click deployment
├── config.json             # LLM/workflow config (generated from template)
├── agents/                 # LangGraph agent nodes
│   ├── backends.py         # LLM backend abstraction (OpenAI/Anthropic/ClaudeCode)
│   ├── kernel_expert.py    # Kernel analysis & reproduction plan
│   ├── test_expert.py      # QEMU test execution
│   ├── qemu_tools.py       # QEMU tool wrappers
│   └── ...
├── graph/                  # LangGraph workflow graph
├── prompts/                # Agent system prompts
├── tools/                  # Busybox build scripts, etc.
├── test_assets/            # Built-in E2E test cases
├── sessions/               # Workflow output logs
└── knowledge_base/         # Archived reproduction cases
```
