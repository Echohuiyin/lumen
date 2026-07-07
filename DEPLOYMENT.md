# Lumen Deployment Guide

End-to-end instructions for deploying the Lumen kernel maintenance workflow system
on a fresh Ubuntu/Debian machine.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [System Dependencies](#2-system-dependencies)
3. [Clone & Submodule](#3-clone--submodule)
4. [LLM & Claude Code Setup](#4-llm--claude-code-setup)
5. [Kernel Source & Crash Setup](#5-kernel-source--crash-setup)
6. [QEMU & Busybox Setup](#6-qemu--busybox-setup)
7. [semcode MCP (Optional)](#7-semcode-mcp-optional)
8. [RAG / Knowledge Base (Optional)](#8-rag--knowledge-base-optional)
9. [Deploy Script](#9-deploy-script)
10. [First Run & Validation](#10-first-run--validation)
11. [Proxy & Air-Gapped Environments](#11-proxy--air-gapped-environments)
12. [Cross-Architecture (arm64) Analysis](#12-cross-architecture-arm64-analysis)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Prerequisites

### Hardware

| Requirement | Minimum | Recommended |
|-------------|---------|-------------|
| RAM | 8 GB | 32 GB |
| CPU | 4 cores | 8+ cores |
| Disk | 20 GB free | 100 GB+ (kernel source trees + test assets are large) |
| KVM | Required for QEMU tests | `kvm-ok` should pass |

Check KVM availability:

```bash
sudo apt install cpu-checker
kvm-ok
```

### Software

- **OS**: Ubuntu 22.04+ or Debian 12+
- **LLM API key**: Any OpenAI-compatible or Anthropic API endpoint
- **Kernel source tree** (for crash analysis): Clone of your target kernel

### Estimated storage for test assets

If you plan to use the built-in test cases:

```
test_assets/
  deadlock/          ~950 MB
  uaf/               ~1.1 GB
  syzbot_btrfs/      ~4.1 GB
  syzbot_kvm_x86/    ~4.1 GB
```

Total: ~10 GB for all test cases.

---

## 2. System Dependencies

```bash
sudo apt update

# Core
sudo apt install -y \
  python3 python3-venv python3-pip \
  git curl wget

# QEMU (x86_64 kernel testing)
sudo apt install -y qemu-system-x86

# Initramfs creation
sudo apt install -y cpio gzip busybox-static

# Crash dump analysis
sudo apt install -y crash

# Optional: faster text search in kernel source
sudo apt install -y ripgrep

# Optional: cross-architecture QEMU
sudo apt install -y qemu-system-arm

# Optional: crash source build (if distro crash is too old)
# sudo apt install -y build-essential gcc g++ libncurses-dev \
#   zlib1g-dev liblzo2-dev libsnappy-dev libzstd-dev bison flex
```

### Verify system dependencies

```bash
# Each of these should print a path or version
command -v python3       && python3 --version
command -v qemu-system-x86_64 && qemu-system-x86_64 --version | head -1
command -v busybox       && busybox --list | head -3
command -v crash         && crash --version | head -1
command -v cpio          && cpio --version | head -1
```

Expected: Python 3.10+, QEMU 7+, busybox 1.36+, crash 8.0+.

---

## 3. Clone & Submodule

```bash
# Clone with submodules
git clone git@github.com:Echohuiyin/Analysis-SKILL.git lumen
cd lumen
git submodule update --init --recursive

# Or if already cloned without --recursive:
# git submodule update --init
```

Verify submodule:

```bash
ls -d Analysis-SKILL/CLAUDE.md Analysis-SKILL/src/aicrasher Analysis-SKILL/skills
```

If the submodule is empty, check SSH keys and GitHub access:

```bash
ssh -T git@github.com
```

---

## 4. LLM & Claude Code Setup

### 4.1 LLM API Endpoint

Lumen supports multiple backends:

| Backend | Config `backend` | Typical Use |
|---------|-----------------|-------------|
| Anthropic | `anthropic` | DeepSeek Anthropic-compatible API, or direct Anthropic API |
| OpenAI | `openai` | OpenAI-compatible API |
| Claude Code | `claude_code` | Claude Code CLI (used by kernel_expert) |

The default config uses **DeepSeek via Anthropic-compatible API** for chat agents
and **Claude Code CLI** for the kernel_expert (the most complex agent).

You need:

1. An API key for the chat backend
2. Claude Code CLI installed and authenticated

### 4.2 Claude Code CLI

```bash
# Install Node.js (if not present)
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs

# Install Claude Code CLI
npm install -g @anthropic-ai/claude-code

# Authenticate (runs interactive login)
claude
# Follow the browser login flow. After successful login, exit with Ctrl+C.
```

Verify:

```bash
claude --version
claude --output-format json --permission-mode bypassPermissions \
  -p 'say "hello" and nothing else' 2>/dev/null
```

> **Proxy note**: If behind a corporate proxy, see [Section 11](#11-proxy--air-gapped-environments).

### 4.3 API Key for Chat Backend

The chat agents (PM, validator, test_expert, knowledge_base, tool experts) use
an OpenAI-compatible or Anthropic API endpoint.

Get your API key from one of:

- **DeepSeek**: https://platform.deepseek.com/api_keys (Anthropic-compatible mode)
- **OpenAI**: https://platform.openai.com/api-keys
- **Anthropic**: https://console.anthropic.com/settings/keys

---

## 5. Kernel Source & Crash Setup

### 5.1 Kernel Source Tree

Kernel source is required for:

1. **Crash analysis**: symbol resolution in vmcore dumps
2. **Module compilation**: if the test needs a kernel module
3. **semcode MCP (optional)**: semantic code search indexing

```bash
# Clone your target kernel (example: linux-next)
git clone git://git.kernel.org/pub/scm/linux/kernel/git/next/linux-next.git \
  ~/linux-next

# Or OLK-6.6 (used by deadlock/uaf test cases):
# git clone https://github.com/openanolis/cloud-kernel.git ~/code/OLK-6.6
```

Set the environment variable:

```bash
export KERNEL_SOURCE_DIR="${HOME}/code/OLK-6.6"
```

> The deadlock and uaf test cases reference a specific commit in OLK-6.6. For
> those cases to compile kernel modules correctly, you need the matching tree.

### 5.2 Crash Utility

The `crash` utility is already installed via apt in Section 2. Verify:

```bash
crash --version
# Expected: crash 8.0.4+
```

If the system crash is too old for your vmlinux, build from source:

```bash
cd Analysis-SKILL/tools/crash-vmcore
./scripts/build_crash.sh
# Installs to: Analysis-SKILL/tools/crash-vmcore/bin/crash
```

Or download a newer version:

```bash
wget https://github.com/crash-utility/crash/releases/download/8.0.4/crash-8.0.4.tar.gz
tar xzf crash-8.0.4.tar.gz
cd crash-8.0.4
make
sudo cp crash /usr/local/bin/
```

---

## 6. QEMU & Busybox Setup

### 6.1 QEMU

QEMU is already installed in Section 2. Verify for your target architecture:

```bash
# x86_64 (most common)
qemu-system-x86_64 --version

# For kernel testing, KVM must be available:
ls -la /dev/kvm
```

### 6.2 Busybox

Busybox is used by `create_initramfs.sh` to build the initramfs for QEMU boot.

```bash
# Check system busybox (from apt install busybox-static in Section 2)
busybox --help | head -1

# It should be a static binary
file $(command -v busybox)
# Expected: "ELF 64-bit LSB executable, ... statically linked"
```

If system busybox is dynamically linked (not static), it will need libraries
copied into initramfs. This works but is fragile — prefer rebuilding:

```bash
# Build busybox from the project's build script
cd tools
./build_busybox.sh --arch x86_64 --clean
```

---

## 7. semcode MCP (Optional)

semcode-mcp provides semantic code search to the kernel_expert agent, enabling
it to find kernel functions, callers, callees, and types across the kernel
source tree.

### 7.1 Build semcode

Requires the Rust toolchain:

```bash
# Install Rust
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"

# Clone and build semcode
git clone https://github.com/anthropics/semcode ~/semcode
cd ~/semcode
cargo build --release
```

The binary will be at `~/semcode/target/release/semcode-mcp`.

### 7.2 Index kernel source

```bash
# Index your kernel source tree (this takes 5-30 minutes)
~/semcode/target/release/semcode-mcp -d ~/code/OLK-6.6/.semcode.db \
  --index ~/code/OLK-6.6
```

### 7.3 Verify

```bash
~/semcode/target/release/semcode-mcp --version
# Expected: prints version, exits 0

# Check DB exists
ls -lh ~/code/OLK-6.6/.semcode.db
```

---

## 8. RAG / Knowledge Base (Optional)

The RAG system enables semantic search over archived analysis reports during
future workflow runs. It uses ChromaDB + Ollama.

```bash
# Install Ollama (embedding service)
curl -fsSL https://ollama.com/install.sh | sh

# Pull embedding model
ollama pull nomic-embed-text

# Install Analysis-SKILL with RAG extras
cd ~/lumen/Analysis-SKILL
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[rag]
deactivate
```

Verify:

```bash
curl http://localhost:11434/api/tags
# Should show "nomic-embed-text" in the response
```

---

## 9. Deploy Script

Now run the main deploy script. This creates the Python virtual environment,
installs dependencies, generates config files, and verifies everything.

```bash
cd ~/lumen
bash deploy.sh
```

### What deploy.sh does

1. **Python check**: verifies Python 3.10+
2. **External dependency check**: looks for qemu, busybox, crash, claude, cpio, gzip, git, semcode-mcp
3. **Virtual environment**: creates `venv/` and activates it
4. **Python deps**: `pip install -r requirements.txt`
5. **Env setup**: creates `.env` template if it doesn't exist (edit this!)
6. **Config**: generates `config.json` from `config.json.template` if not present
7. **Directory init**: creates `knowledge_base/` and `outputs/`
8. **Submodule init**: `git submodule update --init`
9. **Verification**: imports langgraph, langchain, langchain_openai to confirm deps work

### Post-deploy: configure .env

Edit `.env` with your actual values:

```bash
# Edit the generated .env
vim .env
```

Required settings:

```bash
# Your LLM API key (from Section 4.3)
export ANTHROPIC_API_KEY="sk-xxxxxxxxxxxxxxxx"

# Path to kernel source tree (from Section 5.1)
export KERNEL_SOURCE_DIR="${HOME}/code/OLK-6.6"
```

Optional settings (defaults are usually fine):

```bash
# API endpoint (DeepSeek Anthropic-compatible, or use OpenAI/Anthropic)
export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"

# Model
export ANTHROPIC_MODEL="deepseek-v4-flash"

# semcode (if built in Section 7)
export SEMCODE_MCP_BIN="${HOME}/semcode/target/release/semcode-mcp"
export SEMCODE_DB_DIR="${KERNEL_SOURCE_DIR}/.semcode.db"
```

After editing, reload and verify:

```bash
set -a; source .env; set +a
echo "API Key: ${ANTHROPIC_API_KEY:0:8}..."
echo "Kernel: $KERNEL_SOURCE_DIR"
```

---

## 10. First Run & Validation

### 10.1 Activate environment

```bash
cd ~/lumen
source venv/bin/activate
```

### 10.2 Run deadlock test case

This is the simplest built-in test case (~950 MB, expected to run through the
full workflow and reach knowledge base generation).

```bash
python3 main.py test_assets/deadlock/input.txt --config config.json
```

### 10.3 Expected output

The workflow takes 5-20 minutes depending on LLM speed and hardware:

```
============================================================
  Kernel Debuger Workflow
  ────────────────────────────────────────────────────────
  Bug Promote: 内核发生 Mutex ABBA 死锁导致 hung_task panic。...
  vmcore: ${PROJECT_ROOT}/test_assets/deadlock/vmcore.elf
  vmlinux: ${PROJECT_ROOT}/test_assets/deadlock/vmlinux
  boot_kernel: ${PROJECT_ROOT}/test_assets/deadlock/bzImage
  kernel_source: ${KERNEL_SOURCE_DIR}
  ────────────────────────────────────────────────────────
  Model: deepseek-v4-flash
  Input: test_assets/deadlock/input.txt
  Config: config.json
  Session: 20260707_120000_abcd12
============================================================
```

Agents run in order:

| Agent | What to expect | Time |
|-------|---------------|------|
| Validator | Instant | <1s |
| PM | Analyzes input, selects experts | 5-15s |
| ToolExpert (crash) | Runs crash commands on vmcore | 10-60s |
| ToolExpert (knowledge) | Searches knowledge base | 5-10s |
| KernelExpert | Claude Code: writes reproducer | 2-10 min |
| TestExpert | Boots kernel in QEMU, checks signal | 1-5 min |
| KnowledgeBase | Writes final report | 10-30s |

### 10.4 Validation points

After a successful run, verify these outputs:

```bash
# 1. Session directory was created
ls -la sessions/*/

# 2. Key agent outputs exist
ls sessions/*/kernel_expert.txt sessions/*/test_expert.txt

# 3. Final response was printed (look for "Final Response" in stdout)

# 4. Knowledge base entry was created
ls -lt knowledge_base/*.md | head -3
```

### 10.5 Common first-run failures

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `API key not found` | Missing `ANTHROPIC_API_KEY` in `.env` | Edit `.env` and source it |
| `Config file not found` | `config.json` not generated | `cp config.json.template config.json` |
| `QEMU not found` | `qemu-system-x86_64` not installed | `sudo apt install qemu-system-x86` |
| `claude: command not found` | Claude Code CLI not installed | `npm install -g @anthropic-ai/claude-code` |
| `Permission denied` for `/dev/kvm` | User not in `kvm` group | `sudo usermod -aG kvm $USER && newgrp kvm` |
| `crash: not found` or crash sigabort | `crash` not installed or too old | `sudo apt install crash` or build from source |
| Python import error | Virtual environment not activated | `source venv/bin/activate` |
| Submodule errors | `Analysis-SKILL` not initialized | `git submodule update --init` |

---

## 11. Proxy & Air-Gapped Environments

### 11.1 HTTP/HTTPS Proxy

For corporate environments behind a proxy:

```bash
# Set proxy for all tools
export http_proxy=http://proxy.company.com:8080
export https_proxy=http://proxy.company.com:8080

# Git proxy
git config --global http.proxy http://proxy.company.com:8080
git config --global https.proxy http://proxy.company.com:8080

# npm proxy
npm config set proxy http://proxy.company.com:8080
npm config set https-proxy http://proxy.company.com:8080
```

### 11.2 Air-Gapped (No Internet)

For a machine with no internet access, pre-download:

1. **Python packages** (from `requirements.txt`):
   ```bash
   pip download -r requirements.txt -d /tmp/lumen-deps
   # Copy /tmp/lumen-deps to target, then:
   pip install --no-index -f /tmp/lumen-deps -r requirements.txt
   ```

2. **NPM packages**:
   ```bash
   npm pack @anthropic-ai/claude-code
   # Copy .tgz to target, then:
   npm install -g ./anthropic-ai-claude-code-*.tgz
   ```

3. **Rust/semcode**: Pre-build on a networked machine, copy the binary.

4. **Kernel source tree**: Clone once, `git bundle` to transfer.

5. **LLM API**: Must still be reachable (internal endpoint or local model).

---

## 12. Cross-Architecture (arm64) Analysis

Lumen supports analyzing arm64 vmcores and reproducing arm64 kernel bugs from
an x86_64 deployment. Crash analysis is cross-arch natively (a single x86
crash binary can analyze arm64 vmcores when given an arm64 vmlinux); the
QEMU reproduction path uses `qemu-system-aarch64` with TCG emulation.

### 12.1 Install arm64 cross-arch dependencies

```bash
# QEMU arm64 emulator (also pulls qemu-system-arm for arm32)
sudo apt install -y qemu-system-arm

# Cross-compiler for arm64 kernel modules and userspace reproducers
sudo apt install -y gcc-aarch64-linux-gnu

# Optional: arm32 cross-compiler
sudo apt install -y gcc-arm-linux-gnueabi
```

Verify:

```bash
command -v qemu-system-aarch64 && qemu-system-aarch64 --version | head -1
command -v aarch64-linux-gnu-gcc && aarch64-linux-gnu-gcc --version | head -1
file Analysis-SKILL/tools/busybox/prebuilt/busybox_arm64
# Expected: "ELF 64-bit LSB executable, ARM aarch64, ... statically linked"
```

### 12.2 Prepare arm64 kernel source and assets

```bash
# Clone an arm64-capable kernel source tree
git clone git://git.kernel.org/pub/scm/linux/kernel/git/next/linux-next.git \
  ~/linux-next-arm64

# Build arm64 kernel + modules (cross-compile from x86 host)
cd ~/linux-next-arm64
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- defconfig
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- -j$(nproc) Image modules
# Outputs:
#   arch/arm64/boot/Image       (QEMU-bootable)
#   vmlinux                      (with debug symbols, for crash)
```

For arm64 vmcores, collect them on a real arm64 machine or via QEMU with
`qemu-system-aarch64 -machine virt,gic-version=3 -cpu cortex-a57 ...`.

### 12.3 Run arm64 analysis from x86 deployment

Create an `input.txt` for the arm64 case:

```text
Bug Promote: arm64 内核 [...bug description...]
vmcore: /path/to/arm64/vmcore.elf
vmlinux: /path/to/arm64/vmlinux
boot_kernel: /path/to/arm64/Image
kernel_source: $ARM64_KERNEL_DIR
```

Set env vars and run:

```bash
export ARM64_KERNEL_DIR="${HOME}/linux-next-arm64"
source venv/bin/activate
python3 main.py arm64_input.txt --config config.json
```

### 12.4 How cross-arch dispatch works

The workflow auto-detects target arch by sniffing the ELF `e_machine` field
from `vmlinux`. If the LLM omits `target_arch`, the ELF sniff result is used
instead of falling back to host uname (which would be wrong on an x86 host
analyzing arm64 inputs).

QEMU command construction in `boot_kernel()` picks the correct binary and
machine config per arch:

- `qemu-system-aarch64 -machine virt -cpu cortex-a57 -serial file:...`
- `console=ttyAMA0` (PL011 UART, not ttyS0)
- TCG acceleration (KVM unavailable when host arch != target arch)
- Cross-compiled arm64 modules (via `compile_module(arch="arm64")`)
- Arch-filtered binaries_dir (x86 ELF binaries skipped for arm64 target)

### 12.5 Performance notes

- arm64 QEMU with TCG is 5-10x slower than x86 KVM
- Set `qemu_recipe.timeout_sec` to 900+ for arm64 reproductions
- Boot alone takes 30-90s (vs 5-10s for x86 KVM)
- Memory pressure tests that need 1G+ allocations may OOM with default 2G

### 12.6 Limitations

- **No bundled arm64 test case** in `test_assets/`. Bring your own arm64
  Image + vmlinux + vmcore for end-to-end validation.
- **No KVM acceleration** for arm64 on x86 host (TCG only). If you have an
  arm64 host available, deploy Lumen there for faster arm64 testing.
- **Image.gz (compressed arm64)** not auto-decompressed — boot with raw
  `Image` only. Decompress with `gunzip Image.gz` if needed.


## 13. Troubleshooting

### QEMU: "Could not access /dev/kvm"

```bash
sudo usermod -aG kvm $USER
# Log out and back in, or:
newgrp kvm
```

### Claude Code: "Login required"

```bash
claude
# Follow the interactive login flow
```

### Claude Code: Exit code 127 / command not found

```bash
which claude || npm list -g @anthropic-ai/claude-code
# If not found, reinstall: npm install -g @anthropic-ai/claude-code
```

### "busybox applet not found" in initramfs

The system busybox may be a stub. Install the full version:

```bash
sudo apt install --reinstall busybox-static
file /bin/busybox  # Should say "statically linked"
```

### Crash: "vmlinux: no debugging data available"

```bash
file test_assets/deadlock/vmlinux
# Expected: "ELF 64-bit LSB executable, ... not stripped"
# If stripped, you need a vmlinux with debug symbols (CONFIG_DEBUG_INFO=y)
```

### Crash: segfault or SIGABRT on startup

```bash
# Check crash version
crash --version
# Try building from source (Section 5.2) if system version is too old
```

### LLM: "401 Unauthorized" or "Insufficient Balance"

```bash
# Verify your API key works
curl -H "Authorization: Bearer $ANTHROPIC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}]}' \
  $ANTHROPIC_BASE_URL/chat/completions
```

### ChromaDB: "sqlite3 version too old"

```bash
# Install pysqlite3-binary
pip install pysqlite3-binary
# Or upgrade system sqlite3
```

### "Kernel panic - not syncing: Attempted to kill init!" in QEMU

This means the test script (PID 1) exited. Common causes:
- Initramfs missing required binaries or device nodes
- test.sh has a syntax error (check with `bash -n test.sh`)
- reproducer binary not found or crashed

---

## Appendix A: Environment Variable Reference

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `ANTHROPIC_API_KEY` | **Yes** | — | LLM API key for chat agents |
| `ANTHROPIC_BASE_URL` | No | `https://api.deepseek.com/anthropic` | LLM API endpoint |
| `ANTHROPIC_MODEL` | No | `deepseek-v4-flash` | Chat model name |
| `KERNEL_SOURCE_DIR` | For crash analysis | `${HOME}/code/OLK-6.6` | Kernel source tree path |
| `SEMCODE_MCP_BIN` | No | `${HOME}/semcode/target/release/semcode-mcp` | semcode binary path |
| `SEMCODE_DB_DIR` | No | `${KERNEL_SOURCE_DIR}/.semcode.db` | semcode index database |
| `CRASH_BINARY` | No | auto-detected | Path to crash utility |
| `EMBEDDING_BASE_URL` | No | `http://localhost:11434/v1` | Ollama endpoint for RAG |
| `EMBEDDING_MODEL` | No | `nomic-embed-text` | Embedding model for RAG |

---

## Appendix B: File & Directory Reference

| Path | Purpose | Created by |
|------|---------|------------|
| `config.json` | Workflow configuration | `deploy.sh` from `config.json.template` |
| `.env` | Environment variables (API keys, paths) | `deploy.sh` template, user edits |
| `venv/` | Python virtual environment | `deploy.sh` |
| `knowledge_base/` | Archived analysis reports | `deploy.sh` |
| `outputs/` | Reproducer artifacts | `deploy.sh` |
| `sessions/` | Per-run logs and agent output | Workflow at runtime |
| `Analysis-SKILL/` | Submodule: MCP tools, skills, scripts | `git submodule update --init` |
| `~/.claude/skills/` | Installed Claude Code skills | `Analysis-SKILL/scripts/install.sh` |
| `~/.cache/lumen/` | Caches (ikconfig, crash output) | Workflow at runtime |
