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
7. [semcode MCP](#7-semcode-mcp)
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

The deployment host may be either `x86_64`/`amd64` or `arm64`/`aarch64`.
Run the same commands below on either host; the script builds host-native
crash executables for both vmcore targets and cross-builds the arm64 BusyBox
image when needed.

```bash
sudo apt update

# Core
sudo apt install -y \
  python3 python3-venv python3-pip \
  git curl wget

# QEMU (x86_64 / arm64 kernel testing) and persistent SSH guests
sudo apt install -y qemu-system-x86 qemu-system-arm qemu-utils \
  openssh-client debootstrap qemu-user-binfmt binfmt-support

# Some older distributions provide qemu-user-static instead of
# qemu-user-binfmt; install whichever package is available.  One of these
# packages must register qemu-x86_64 and qemu-aarch64 in binfmt_misc.

# Initramfs creation
sudo apt install -y cpio gzip

# Build crash and BusyBox from bundled/project-managed source
sudo apt install -y \
  build-essential gcc g++ gcc-x86-64-linux-gnu gcc-aarch64-linux-gnu \
  bison flex patch texinfo file e2fsprogs \
  libncurses-dev zlib1g-dev liblzo2-dev libsnappy-dev \
  libzstd-dev libgmp-dev libmpfr-dev util-linux

# Rust toolchain for semcode (use the project's rsproxy instructions below
# when the default Rust download endpoint is slow).
sudo apt install -y cargo rustc

# Optional: faster text search in kernel source
sudo apt install -y ripgrep
```

### Verify system dependencies

```bash
# Each of these should print a path or version
command -v python3       && python3 --version
command -v qemu-system-x86_64 && qemu-system-x86_64 --version | head -1
command -v qemu-system-aarch64 && qemu-system-aarch64 --version | head -1
command -v x86_64-linux-gnu-gcc && x86_64-linux-gnu-gcc --version | head -1
command -v aarch64-linux-gnu-gcc && aarch64-linux-gnu-gcc --version | head -1
command -v cargo && cargo --version
command -v rustc && rustc --version
command -v cpio          && cpio --version | head -1
```

Expected: Python 3.10+, QEMU 7+, and the build tools above available.

For mainland network environments, the deploy script accepts these optional
mirror variables:

```bash
export LUMEN_DEBIAN_MIRROR=https://mirrors.aliyun.com/debian
export LUMEN_GNU_MIRROR=https://mirrors.aliyun.com/gnu
export LUMEN_PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
```

---

## 3. Clone & Submodule

```bash
# Clone the Lumen project with submodules
git clone git@github.com:Echohuiyin/lumen.git lumen
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

The deployment config has no provider, endpoint, model, or account defaults.
Set these values explicitly in `.env` and in the project Claude settings file.

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

The chat agents (PM, validator, knowledge_base, tool experts) use
an OpenAI-compatible or Anthropic API endpoint.

Get your API key from one of:

- **DeepSeek**: https://platform.deepseek.com/api_keys (Anthropic-compatible mode)
- **OpenAI**: https://platform.openai.com/api-keys
- **Anthropic**: https://console.anthropic.com/settings/keys

### 4.4 Claude Code Settings File (kernel_expert)

Claude Code supports an explicit settings file. Lumen copies the selected
settings into the project-local file and invokes the CLI with both
--settings and --setting-sources project. This keeps kernel_expert on one known credential profile and limits skills/settings discovery to the project scope.

Prepare the file once on the deployment host:

    mkdir -p .claude
    cp $HOME/.claude/settings.json .claude/settings.json
    chmod 600 .claude/settings.json

The project configuration points agents.kernel_expert.settings_file at ${LUMEN_PROJECT_ROOT:-.}/.claude/settings.json. The source file should contain the Claude Code env values required by your provider, including ANTHROPIC_AUTH_TOKEN and ANTHROPIC_BASE_URL. If the file is absent or invalid, the workflow stops with an explicit configuration error. There is no settings file, account, model, or session fallback.


---

## 5. Kernel Source & Crash Setup

### 5.1 Kernel Source Tree

Kernel source is required for:

1. **Crash analysis**: symbol resolution in vmcore dumps
2. **Source validation**: evidence-backed source and symbol lookup for a userspace reproducer
3. **semcode MCP (optional)**: semantic code search indexing

```bash
# Clone the target kernel source using a deployment-provided URL.
git clone "${KERNEL_SOURCE_URL:?set KERNEL_SOURCE_URL}" \
  "${LUMEN_KERNEL_SOURCE_ROOT:?set LUMEN_KERNEL_SOURCE_ROOT}"
```

> The maintenance workflow creates only userspace C reproducers. If a legacy
> fixture references a matching kernel commit, use the configured source root
> for read-only evidence and symbol lookup; do not build or load a kernel module.

### 5.2 Crash Utility

The `crash` utility is required for vmcore analysis (lock_analysis,
crash_analysis, kernel_log_analysis experts). Lumen uses arch-suffixed crash
binaries built from the bundled crash source.

#### 5.2.1 Arch-specific binaries

**Crash is compiled with a single TARGET arch hardcoded.** An x86_64-targeted
crash CANNOT parse an arm64 vmcore — it errors with `machine type mismatch:
crash=X86_64 vmcore=ARM64` then `not a supported file format`.

Lumen auto-selects the right crash binary by sniffing the vmlinux's ELF
`e_machine` field. It looks for `crash_<arch>` at:
- `runtime/crash-source/crash_<arch>` (the `deploy.sh` default)
- `${CRASH_SOURCE_DIR}/crash_<arch>` (when `CRASH_SOURCE_DIR` is overridden)
- directories listed in `LUMEN_CRASH_BIN_DIRS` or executable `PATH`

The source is cloned into the ignored runtime directory and pinned by
`CRASH_REF` (default `9.0.2`). The executable itself is built for the host
machine, while `TARGET=X86_64` or `TARGET=ARM64` selects which vmcore format it
parses. This is why the same procedure works on both x86_64 and arm64 hosts.

```text
repo: https://github.com/crash-utility/crash.git
ref: 9.0.2
```

`deploy.sh` builds both binaries automatically. To override the source or use
a local mirror, set `CRASH_REPO` and run:

```bash
CRASH_REPO=https://github.com/crash-utility/crash.git bash deploy.sh
```

The generated binaries are host-native and both target decoders are produced.

Verify:

```bash
$ runtime/crash-source/crash_arm64 -v | head -1
crash_arm64 9.0.2

$ runtime/crash-source/crash_x86_64 -v | head -1
crash_x86_64 9.0.2

$ strings runtime/crash-source/crash_arm64 | grep -E '^(X86_64|ARM64)$' | head -1
ARM64

$ strings runtime/crash-source/crash_x86_64 | grep -E '^(X86_64|ARM64)$' | head -1
X86_64
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

BusyBox remains available for the bundled one-shot QEMU skill. Lumen's workflow
uses a separate Debian ext4 image with `sshd` for persistent SSH execution, so
it does not alter the existing Analysis-SKILL rootfs scripts.

```bash
bash Analysis-SKILL/tools/build_busybox.sh --arch x86_64 --clean
bash Analysis-SKILL/tools/build_busybox.sh --arch arm64 --clean

file Analysis-SKILL/tools/busybox/prebuilt/busybox_x86_64
file Analysis-SKILL/tools/busybox/prebuilt/busybox_arm64
```

`deploy.sh` builds both automatically when the prebuilt files are missing.

To create a standalone ext4 rootfs image:

```bash
bash Analysis-SKILL/skills/qemu-test/scripts/create_ext4_rootfs.sh \
  --arch x86_64 \
  --output /tmp/rootfs_x86_64.ext4
```

### 6.3 Persistent SSH guests

`deploy.sh` creates ignored images and keys under `runtime/qemu-ssh/` for both
architectures. The lifecycle is modeled on syzkaller: a Debian image, loopback
SSH forwarding, and a per-boot kernel identity. To build only these artifacts:

```bash
bash scripts/provision_qemu_ssh_image.sh --arch all
```

The runner reuses a guest only when its architecture, boot-kernel digest,
rootfs digest, and QEMU recipe match. It uploads each PoC to a fresh guest
directory over SSH and keeps serial/SSH evidence in the session artifacts.
For arm64 it follows syzkaller's `virt`/`ttyAMA0`/`root=/dev/vda` model, uses
PCI virtio (`virtio-blk-pci` and `virtio-net-pci`), adds early serial output,
and provisions `serial-getty@ttyAMA0` plus `haveged` for reliable SSH startup
under x86 TCG emulation. See [syzkaller arm64 QEMU setup](https://github.com/google/syzkaller/blob/master/docs/linux/setup_linux-host_qemu-vm_arm64-kernel.md).

**Kernel/rootfs compatibility (important)**: the generated ext4 image is a
Debian userspace rootfs; it does not contain the arbitrary kernel's modules,
initramfs, or a replacement kernel. The boot kernel must therefore have the
root filesystem and virtio storage/network drivers built in (`=y`), including
`CONFIG_EXT4_FS`, `CONFIG_VIRTIO_BLK`, and `CONFIG_VIRTIO_PCI` (plus the
architecture's virtio-net driver). If any of these are modules (`=m`), provide
an initramfs and matching `/lib/modules/<kernel-release>` tree and pass them
with the kernel. A panic such as `VFS: Cannot open root device` or
`unknown-block(0,0)` indicates this kernel/image contract is missing; it does
not by itself indicate that image packaging failed. Normal Debian boot mounts
`/proc` and `/sys` inside the guest once the kernel reaches userspace.

---

## 7. semcode MCP

semcode-mcp provides semantic code search to the kernel_expert agent, enabling
it to find kernel functions, callers, callees, and types across the kernel
source tree.

### 7.1 Build semcode

Requires the Rust toolchain:

```bash
# Install Rust
export RUSTUP_DIST_SERVER=https://rsproxy.cn
export RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup
curl --proto '=https' --tlsv1.2 -sSf https://rsproxy.cn/rustup-init.sh | sh
source "$HOME/.cargo/env"

# Optional Cargo registry mirror for semcode dependencies
mkdir -p "$HOME/.cargo"
cat > "$HOME/.cargo/config.toml" <<'EOF'
[source.crates-io]
replace-with = "rsproxy"
[source.rsproxy]
registry = "sparse+https://rsproxy.cn/index/"
[net]
git-fetch-with-cli = true
EOF

# Build semcode from the project-managed source
cd Analysis-SKILL/tools/semcode
cargo build --release
```

The binary will be at `Analysis-SKILL/tools/semcode/target/release/semcode-mcp`.
If the source directory is missing, `deploy.sh` clones it from
`https://github.com/facebookexperimental/semcode` and builds it there.

### 7.2 Index kernel source

```bash
# Index your kernel source tree (this takes 5-30 minutes)
Analysis-SKILL/tools/semcode/target/release/semcode-index \
  -s "${LUMEN_KERNEL_SOURCE_ROOT:?set LUMEN_KERNEL_SOURCE_ROOT}" \
  -d "${LUMEN_KERNEL_SOURCE_ROOT:?set LUMEN_KERNEL_SOURCE_ROOT}/.semcode.db"
```

### 7.3 Verify

```bash
Analysis-SKILL/tools/semcode/target/release/semcode-mcp --help
# Expected: prints usage, exits 0

# Check DB exists
ls -lh "${LUMEN_KERNEL_SOURCE_ROOT:?set LUMEN_KERNEL_SOURCE_ROOT}/.semcode.db"
```

---

## 8. RAG / Knowledge Base (Optional)

The RAG system enables semantic search over archived analysis reports during
future workflow runs. It uses ChromaDB and the embedding endpoint configured
by `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL`, and `EMBEDDING_API_KEY`.

```bash

# Install Analysis-SKILL with RAG extras
cd "${LUMEN_PROJECT_ROOT:?set LUMEN_PROJECT_ROOT}/Analysis-SKILL"
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[rag]
deactivate
```

Before running the workflow, source `.env` and verify that the deployment
environment supplies all three embedding variables. The endpoint and model
are provider-specific inputs; Lumen does not assume a local service.

---

## 9. Deploy Script

Now run the main deploy script. This creates the Python virtual environment,
installs dependencies, generates config files, and verifies everything.

```bash
cd "${LUMEN_PROJECT_ROOT:?set LUMEN_PROJECT_ROOT}"
bash deploy.sh
```

### What deploy.sh does

1. **Python check**: verifies Python 3.10+
2. **External dependency check**: looks for QEMU, build tools, cross compiler,
   Claude Code, cpio, gzip, git, wget, and project-managed tool outputs
3. **Arch-specific crash binary check**: verifies `crash_x86_64` and
   `crash_arm64` are present in the project crash directory, configured
   `LUMEN_CRASH_BIN_DIRS`, or executable `PATH` (required for cross-arch
   vmcore analysis — see Section 5.2.1)
4. **Virtual environment**: creates `venv/` and activates it
5. **Python deps**: `pip install -r requirements.txt`
6. **Env setup**: creates `.env` template if it doesn't exist (edit this!)
7. **Config**: generates `config.json` from `config.json.template` if not present
8. **Directory init**: creates `knowledge_base/` and `outputs/`
9. **Tool builds**: builds `crash_x86_64`, `crash_arm64`,
   `busybox_x86_64`, `busybox_arm64`, `semcode-mcp`, and the x86_64/arm64
   persistent SSH guest images when missing
10. **Verification**: imports langgraph, langchain, langchain_openai, and pytest
    to confirm dependencies work

### Post-deploy: configure .env

Edit `.env` with your actual values:

```bash
# Edit the generated .env
vim .env
```

Required settings:

```bash
# Your LLM API key (from Section 4.3)
export ANTHROPIC_API_KEY="<provider-api-key>"

# API endpoint selected by this deployment
export ANTHROPIC_BASE_URL="<provider-anthropic-compatible-endpoint>"

# Model
export ANTHROPIC_MODEL="<provider-model>"

# RAG embedding endpoint for full knowledge_search and Chroma import
export EMBEDDING_BASE_URL="<embedding-endpoint>"
export EMBEDDING_MODEL="<embedding-model>"
export EMBEDDING_API_KEY="<embedding-api-key>"
```

After editing, reload and verify:

```bash
set -a; source .env; set +a
echo "API Key: ${ANTHROPIC_API_KEY:0:8}..."
```

---

## 10. First Run & Validation

### 10.1 Activate environment

```bash
cd "${LUMEN_PROJECT_ROOT:?set LUMEN_PROJECT_ROOT}"
source venv/bin/activate
```

### 10.2 Run deadlock test case

This is the simplest built-in test case (~950 MB, expected to run through the
full workflow and reach knowledge base generation).

```bash
python3 main.py test_assets/deadlock/input.txt --config config.json
```

For the arm64 variant (requires `crash_arm64` binary from Section 5.2.1
and `test_assets/deadlock_arm64/` artifacts built via
`scripts/build_arm64_deadlock_testcase.sh`):

```bash
python3 main.py test_assets/deadlock_arm64/input.txt --config config.json
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
  kernel_source: ${LUMEN_KERNEL_SOURCE_ROOT}
  ────────────────────────────────────────────────────────
  Model: ${ANTHROPIC_MODEL:-<provider-model>}
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
| ToolExpert (crash) | Runs crash commands on vmcore (uses `crash_x86_64` or `crash_arm64` based on vmlinux arch) | 10-60s |
| ToolExpert (knowledge) | Searches knowledge base | 5-10s |
| KernelExpert | One Claude Code loop: analyzes raw logs + expert findings, writes a PoC, then verifies it through persistent SSH QEMU | 3-15 min |
| KnowledgeBase | Writes final report | 10-30s |

### 10.4 Validation points

After a successful run, verify these outputs:

```bash
# 1. Session directory was created
ls -la sessions/*/

# 2. Key loop output and one or more per-round deterministic contracts exist
ls sessions/*/kernel_expert.txt sessions/*/persistent_test_contract.round-*.json

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
an x86_64 deployment. Both the crash analysis path and the QEMU reproduction
path are cross-arch capable.

**Key requirement**: crash utility must be built with `target=ARM64` (see
Section 5.2.1). The default x86_64-targeted crash CANNOT parse arm64 vmscores.

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

# Crash arm64 binary (REQUIRED for arm64 vmcore analysis)
test -x runtime/crash-source/crash_arm64 || command -v crash_arm64
# If LUMEN_CRASH_BIN_DIRS is set, check the configured directories as well.
strings runtime/crash-source/crash_arm64 2>/dev/null | grep -E '^ARM64$' | head -1
# Expected: ARM64
```

### 12.2 Prepare arm64 kernel source and assets

```bash
# Clone an arm64-capable kernel source tree
git clone git://git.kernel.org/pub/scm/linux/kernel/git/next/linux-next.git \
  "${LUMEN_ARM64_KERNEL_SOURCE:?set LUMEN_ARM64_KERNEL_SOURCE}"

# Build the arm64 kernel image (cross-compile from x86 host)
cd "${LUMEN_ARM64_KERNEL_SOURCE:?set LUMEN_ARM64_KERNEL_SOURCE}"
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- defconfig
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- -j$(nproc) Image
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
kernel_source: ${LUMEN_ARM64_KERNEL_SOURCE}
```

Run:

```bash
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

### 12.6 Bundled arm64 test case

`test_assets/deadlock_arm64/` is a complete arm64 reference testcase (Mutex
ABBA deadlock → hung_task panic). The artifacts are gitignored (too large:
vmlinux ~417M, vmcore.elf ~513M) but a builder script is committed:

```bash
# One-shot build: kernel + module + initramfs + QEMU + vmcore capture
# Estimated: 35-65 min (kernel build dominates), ~930MB artifacts
bash scripts/build_arm64_deadlock_testcase.sh

# Output at test_assets/deadlock_arm64/:
#   vmlinux                  — arm64 ELF, with debug_info
#   Image                    — arm64 boot executable
#   vmcore.elf               — arm64 ELF core, VMCOREINFO present
#   mutex_abba_deadlock.ko   — arm64 ELF relocatable
#   boot.log                 — QEMU console, contains hung_task panic
#   input.txt                — Lumen workflow input (target_arch omitted,
#                              tests _sniff_arch_from_elf)
#   REPRODUCTION.md          — manual reproduction steps
```

Run end-to-end:

```bash
source venv/bin/activate
python3 main.py test_assets/deadlock_arm64/input.txt
# Expected: lock_analysis.txt contains real crash output
#           (mutex_lock call trace, PID 183/184 in D-state,
#            mutex owner decoding, ABBA lock order proof)
```

### 12.7 Limitations

- **No KVM acceleration** for arm64 on x86 host (TCG only, 5-10x slower than
  x86 KVM). If you have an arm64 host available, deploy Lumen there for
  faster arm64 testing.
- **Image.gz (compressed arm64)** not auto-decompressed — boot with raw
  `Image` only. Decompress with `gunzip Image.gz` if needed.
- **OLK source tree** ends up arm64-configured after running the builder
  script. Switch back with `make mrproper && make defconfig` (x86_64).


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

Rebuild the project-managed static BusyBox binaries:

```bash
bash Analysis-SKILL/tools/build_busybox.sh --arch x86_64 --clean
bash Analysis-SKILL/tools/build_busybox.sh --arch arm64 --clean
file Analysis-SKILL/tools/busybox/prebuilt/busybox_x86_64
file Analysis-SKILL/tools/busybox/prebuilt/busybox_arm64
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
# Confirm the deployment inputs are present, then use the selected provider's
# documented health check (the endpoint and model are intentionally not fixed).
test -n "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY}"
test -n "${ANTHROPIC_BASE_URL:?set ANTHROPIC_BASE_URL}"
test -n "${ANTHROPIC_MODEL:?set ANTHROPIC_MODEL}"
printf 'Configured endpoint: %s\nConfigured model: %s\n' \
  "$ANTHROPIC_BASE_URL" "$ANTHROPIC_MODEL"
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
- the structured execution plan is invalid or references a missing PoC artifact
- reproducer binary not found or crashed

---

## Appendix A: Environment Variable Reference

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `ANTHROPIC_API_KEY` | **Yes** | — | LLM API key for chat agents |
| `ANTHROPIC_BASE_URL` | **Yes** | - | LLM API endpoint for tool experts |
| `ANTHROPIC_MODEL` | **Yes** | - | Chat model name for tool experts |
| `EMBEDDING_BASE_URL` | **Yes** | — | RAG embedding API endpoint |
| `EMBEDDING_MODEL` | **Yes** | — | RAG embedding model |
| `EMBEDDING_API_KEY` | **Yes** | — | RAG embedding API key |

---

## Appendix B: File & Directory Reference

| Path | Purpose | Created by |
|------|---------|------------|
| `config.json` | Workflow configuration | `deploy.sh` from `config.json.template` |
| `.env` | LLM environment variables | `deploy.sh` template, user edits |
| `venv/` | Python virtual environment | `deploy.sh` |
| `knowledge_base/` | Archived analysis reports | `deploy.sh` |
| `outputs/` | Reproducer artifacts | `deploy.sh` |
| `sessions/` | Per-run logs and agent output | Workflow at runtime |
| `Analysis-SKILL/` | Submodule: MCP tools, skills, scripts | `git submodule update --init` |
| `~/.claude/skills/` | Installed Claude Code skills | `Analysis-SKILL/scripts/install.sh` |
| `~/.cache/lumen/` | Caches (ikconfig, crash output) | Workflow at runtime |
