#!/bin/bash
# Lumen 一键部署脚本
# 自动化部署内核维护工作流系统
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail()  { echo -e "${RED}[FAIL]${NC} $1"; }

# ── Config ───────────────────────────────────────────────────────────────────
VENV_DIR="venv"
USE_VENV=true
CRASH_SOURCE_DIR="${CRASH_SOURCE_DIR:-Analysis-SKILL/tools/crash}"
CRASH_REPO="${CRASH_REPO:-https://github.com/crash-utility/crash.git}"
CRASH_REF="${CRASH_REF:-9.0.2}"
CRASH_BUILDER="Analysis-SKILL/tools/crash-vmcore/scripts/build_crash.sh"
BUSYBOX_BUILDER="Analysis-SKILL/tools/build_busybox.sh"
SEMCODE_SOURCE_DIR="Analysis-SKILL/tools/semcode"
SEMCODE_REPO="${SEMCODE_REPO:-https://github.com/facebookexperimental/semcode.git}"
SEMCODE_MCP_BIN="${SEMCODE_SOURCE_DIR}/target/release/semcode-mcp"
PERSISTENT_QEMU_PROVISIONER="scripts/provision_qemu_ssh_image.sh"

# ── Pre-flight: check required external binaries ──────────────────────────────
check_cmd() {
    local bin="$1" pkg="$2"
    if command -v "$bin" &>/dev/null; then
        ok "$bin — found at $(command -v "$bin")"
        return 0
    else
        warn "$bin — NOT FOUND (install: $pkg)"
        return 1
    fi
}

preflight_check() {
    echo ""
    info "=== 外部依赖检查 ==="

    local fail_count=0

    check_cmd python3 "python3 (>= 3.10)" || ((fail_count++))
    check_cmd qemu-system-x86_64 "apt install qemu-system-x86" || ((fail_count++))
    check_cmd qemu-system-aarch64 "apt install qemu-system-arm" || ((fail_count++))
    check_cmd qemu-img "apt install qemu-utils" || ((fail_count++))
    check_cmd ssh "apt install openssh-client" || ((fail_count++))
    check_cmd scp "apt install openssh-client" || ((fail_count++))
    check_cmd ssh-keygen "apt install openssh-client" || ((fail_count++))
    check_cmd debootstrap "apt install debootstrap" || ((fail_count++))
    check_cmd cpio "apt install cpio" || ((fail_count++))
    check_cmd gzip "apt install gzip (usually pre-installed)" || ((fail_count++))
    check_cmd claude "npm install -g @anthropic-ai/claude-code" || ((fail_count++))
    check_cmd git "apt install git" || ((fail_count++))
    check_cmd wget "apt install wget" || ((fail_count++))
    check_cmd make "apt install build-essential" || ((fail_count++))
    check_cmd gcc "apt install build-essential" || ((fail_count++))
    check_cmd g++ "apt install build-essential" || ((fail_count++))
    check_cmd bison "apt install bison" || ((fail_count++))
    check_cmd flex "apt install flex" || ((fail_count++))
    check_cmd patch "apt install patch" || ((fail_count++))
    check_cmd makeinfo "apt install texinfo" || ((fail_count++))
    check_cmd file "apt install file" || ((fail_count++))
    check_cmd mke2fs "apt install e2fsprogs" || ((fail_count++))
    check_cmd aarch64-linux-gnu-gcc "apt install gcc-aarch64-linux-gnu" || ((fail_count++))

    # ── Crash utility: arch-specific binaries ────────────────────────────────
    # crash is compiled with a single TARGET arch hardcoded. An x86_64-targeted
    # crash CANNOT parse an arm64 vmcore ("machine type mismatch" → "not a
    # supported file format"). Lumen auto-selects crash_<arch> based on
    # vmlinux's ELF e_machine. Verify the binaries exist.
    # Lumen looks for arch-suffixed binaries at:
    #   Analysis-SKILL/tools/crash/crash_<arch>  (source-built)
    #   /usr/local/bin/crash_<arch>
    echo ""
    info "=== crash 二进制 (按架构区分) ==="
    # Check for arch-suffixed binaries in Lumen's lookup paths
    local crash_x86_64_found=""
    local crash_arm64_found=""
    for d in "$CRASH_SOURCE_DIR" "/usr/local/bin"; do
        if [ -z "$crash_x86_64_found" ] && [ -x "${d}/crash_x86_64" ]; then
            crash_x86_64_found="${d}/crash_x86_64"
        fi
        if [ -z "$crash_arm64_found" ] && [ -x "${d}/crash_arm64" ]; then
            crash_arm64_found="${d}/crash_arm64"
        fi
    done

    if [ -n "$crash_x86_64_found" ]; then
        ok "crash_x86_64 — $crash_x86_64_found"
    else
        warn "crash_x86_64 — NOT FOUND (will build from source)"
    fi
    if [ -n "$crash_arm64_found" ]; then
        ok "crash_arm64 — $crash_arm64_found"
    else
        warn "crash_arm64 — NOT FOUND (will build from source)"
    fi

    # Optional: arm32 cross-arch analysis & reproduction
    echo ""
    info "=== arm32 跨架构分析 (可选) ==="
    check_cmd arm-linux-gnueabi-gcc "apt install gcc-arm-linux-gnueabi (for arm32 cross-compile)" || true

    # BusyBox binaries are built from the bundled source for each target.
    if [ -f Analysis-SKILL/tools/busybox/prebuilt/busybox_x86_64 ]; then
        ok "busybox x86_64 — Analysis-SKILL/tools/busybox/prebuilt/busybox_x86_64"
    else
        warn "busybox x86_64 — NOT FOUND (will build from source)"
    fi

    # arm64 BusyBox target
    if [ -f Analysis-SKILL/tools/busybox/prebuilt/busybox_arm64 ]; then
        ok "busybox arm64 — Analysis-SKILL/tools/busybox/prebuilt/busybox_arm64"
    else
        warn "busybox arm64 — NOT FOUND (will build from source)"
    fi

    # semcode MCP is built into Analysis-SKILL/tools/semcode by deploy.sh.
    if [ -x "$SEMCODE_MCP_BIN" ]; then
        ok "semcode-mcp — found at $SEMCODE_MCP_BIN"
    else
        warn "semcode-mcp — NOT FOUND (will build into $SEMCODE_SOURCE_DIR)"
    fi

    # git submodule
    if [ -f Analysis-SKILL/CLAUDE.md ]; then
        ok "Analysis-SKILL submodule — present"
    else
        warn "Analysis-SKILL submodule — missing (run: git submodule update --init)"
        ((fail_count++))
    fi

    if [ -f "$BUSYBOX_BUILDER" ]; then
        ok "busybox build script — $BUSYBOX_BUILDER"
    else
        warn "busybox build script — NOT FOUND (initialize Analysis-SKILL submodule)"
        ((fail_count++))
    fi

    if [ -f "$CRASH_BUILDER" ]; then
        ok "crash build script — $CRASH_BUILDER"
    else
        warn "crash build script — NOT FOUND (initialize Analysis-SKILL submodule)"
        ((fail_count++))
    fi

    if [ "$fail_count" -gt 0 ]; then
        echo ""
        warn "=== $fail_count 个依赖缺失，请安装后重试 ==="
        echo ""
    else
        echo ""
        ok "=== 所有外部依赖已就绪 ==="
    fi
}

# ── Python version check ─────────────────────────────────────────────────────
check_python() {
    if ! command -v python3 &>/dev/null; then
        fail "未找到 python3，请安装 Python 3.10+"
        exit 1
    fi
    local ver_ok
    ver_ok=$(python3 -c 'import sys; print(sys.version_info >= (3,10))')
    if [ "$ver_ok" != "True" ]; then
        fail "Python 版本过低: $(python3 --version)，需要 3.10+"
        exit 1
    fi
    ok "Python 版本: $(python3 --version)"
}

# ── Virtual env ──────────────────────────────────────────────────────────────
create_virtualenv() {
    if [ -d "$VENV_DIR" ]; then
        info "虚拟环境已存在: $VENV_DIR"
    else
        python3 -m venv "$VENV_DIR"
        ok "虚拟环境已创建: $VENV_DIR"
    fi
    source "$VENV_DIR/bin/activate"
    ok "虚拟环境已激活"
}

# ── Install Python deps ──────────────────────────────────────────────────────
install_deps() {
    if [ -f "requirements.txt" ]; then
        pip install -r requirements.txt -q
        ok "Python 依赖安装完成"
    else
        fail "未找到 requirements.txt"
        exit 1
    fi
}

# ── Env / config ─────────────────────────────────────────────────────────────
setup_env() {
    echo ""
    info "=== 环境变量配置 ==="

    # .env template if not exists
    if [ ! -f .env ]; then
        cat > .env << 'ENVEOF'
# ── LLM API ──────────────────────────────────────────────────────────────────
# 后端服务：https://api.deepseek.com/anthropic（DeepSeek Anthropic 兼容）
# 或 https://api.openai.com/v1（原生 OpenAI）
export ANTHROPIC_API_KEY="sk-your-key-here"
export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
export ANTHROPIC_MODEL="deepseek-v4-flash"

# ── RAG Embedding API ────────────────────────────────────────────────────────
# Required by knowledge_search and knowledge_base Chroma import.
# Any OpenAI-compatible /v1/embeddings endpoint can be used.
export EMBEDDING_BASE_URL="http://localhost:11434/v1"
export EMBEDDING_MODEL="bge-large-zh"
export EMBEDDING_API_KEY="not-required"

ENVEOF
        warn "已创建 .env 模板 — 请编辑 .env 填写 LLM 配置；如使用非默认 RAG embedding，请调整 EMBEDDING_*"
    else
        ok ".env 已存在"
    fi

    # Source .env if present
    if [ -f .env ]; then
        set -a; source .env; set +a
        ok "已加载 .env"
    fi

    # config.json
    if [ ! -f config.json ]; then
        if [ -f config.json.template ]; then
            # Keep variable placeholders so values from .env are resolved at runtime.
            cp config.json.template config.json
            ok "已从模板创建 config.json"
            warn "请编辑 .env 填写 LLM 配置；如使用非默认 RAG embedding，请调整 EMBEDDING_*，并在运行前执行 source .env"
        else
            warn "未找到 config.json.template，请手动创建 config.json"
        fi
    else
        ok "config.json 已存在"
    fi
}

# ── Directory init ────────────────────────────────────────────────────────────
init_dirs() {
    mkdir -p knowledge_base outputs
    ok "目录结构已创建 (knowledge_base/ outputs/)"
}

# ── Dual-architecture tools ──────────────────────────────────────────────────
build_crash_binary() {
    local target="$1" output="$2"
    if [ -x "$output" ]; then
        ok "crash_${target} 已存在: $output"
        return
    fi

    info "从源码构建 crash_${target}（首次构建会编译 GDB，需数分钟）"
    bash "$CRASH_BUILDER" \
        --arch "$target" \
        --source-dir "$CRASH_SOURCE_DIR" \
        --output "$output" \
        --repo-url "$CRASH_REPO" \
        --repo-ref "$CRASH_REF" \
        --clean
    ok "crash_${target} 构建完成: $output"
}

build_dual_arch_tools() {
    echo ""
    info "=== 构建 x86_64 / arm64 分析与复现工具 ==="

    # crash is target-specific: a binary built for one target cannot parse the
    # other target's vmcore. Keep both in the runtime lookup directory.
    build_crash_binary "x86_64" "${CRASH_SOURCE_DIR}/crash_x86_64"
    build_crash_binary "arm64" "${CRASH_SOURCE_DIR}/crash_arm64"

    local busybox_dir="Analysis-SKILL/tools/busybox/prebuilt"
    if [ ! -x "${busybox_dir}/busybox_x86_64" ]; then
        info "从源码构建 BusyBox x86_64"
        bash "$BUSYBOX_BUILDER" --arch x86_64 --clean
    else
        ok "BusyBox x86_64 已存在: ${busybox_dir}/busybox_x86_64"
    fi
    if [ ! -x "${busybox_dir}/busybox_arm64" ]; then
        info "从源码构建 BusyBox arm64"
        bash "$BUSYBOX_BUILDER" --arch arm64 --clean
    else
        ok "BusyBox arm64 已存在: ${busybox_dir}/busybox_arm64"
    fi
}

# ── Persistent SSH QEMU guests ───────────────────────────────────────────────
provision_persistent_qemu_images() {
    echo ""
    info "=== 构建常驻 SSH QEMU 镜像 (x86_64 / arm64) ==="
    if [ ! -f "$PERSISTENT_QEMU_PROVISIONER" ]; then
        fail "缺失常驻 QEMU 镜像构建脚本: $PERSISTENT_QEMU_PROVISIONER"
        exit 1
    fi
    # The provisioner is intentionally source-controlled while its Debian
    # images and SSH keys live in runtime/ and are ignored by Git.  It uses
    # sudo/debootstrap only when an image is absent.
    bash "$PERSISTENT_QEMU_PROVISIONER" --arch all
    ok "常驻 SSH QEMU 镜像已就绪"
}

# ── semcode MCP ───────────────────────────────────────────────────────────────
build_semcode_mcp() {
    echo ""
    info "=== 构建 semcode MCP ==="

    if [ -x "$SEMCODE_MCP_BIN" ]; then
        ok "semcode-mcp 已存在: $SEMCODE_MCP_BIN"
        return
    fi

    if ! command -v cargo &>/dev/null; then
        fail "未找到 cargo，无法构建 semcode-mcp。请先安装 Rust 工具链。"
        exit 1
    fi

    if [ ! -f "${SEMCODE_SOURCE_DIR}/Cargo.toml" ]; then
        info "获取 semcode 源码: $SEMCODE_SOURCE_DIR"
        rm -rf "$SEMCODE_SOURCE_DIR"
        git clone "$SEMCODE_REPO" "$SEMCODE_SOURCE_DIR"
    fi

    info "从源码构建 semcode-mcp（首次构建耗时较长）"
    (
        cd "$SEMCODE_SOURCE_DIR"
        cargo build --release
    )
    ok "semcode-mcp 构建完成: $SEMCODE_MCP_BIN"
}

# ── Verify ────────────────────────────────────────────────────────────────────
verify() {
    info "=== 验证安装 ==="
    local venv_python="$VENV_DIR/bin/python"

    for f in main.py requirements.txt; do
        if [ ! -f "$f" ]; then
            fail "缺失关键文件: $f"
            exit 1
        fi
    done

    if [ "$USE_VENV" = true ]; then
        "$venv_python" -c "
try:
    import langgraph; import langchain; import langchain_core; import langchain_openai; import pytest
    print('  核心模块: langgraph/langchain/langchain-core/langchain-openai/pytest — OK')
except ImportError as e:
    print(f'  导入失败: {e}')
    exit(1)
" || { fail "核心模块验证失败，请检查 requirements.txt"; exit 1; }
    fi

    ok "安装验证通过"
}

# ── Usage ─────────────────────────────────────────────────────────────────────
usage() {
    echo ""
    info "=== 使用方式 ==="
    echo ""
    echo "  1. 激活环境:  source venv/bin/activate"
    echo "  2. 分析问题:  python main.py your_input.txt --config config.json"
    echo ""
    echo "  输入文件格式 (见 input.txt.template):"
    echo '    Bug Promote: 问题描述'
    echo '    vmcore: ./test_case/vmcore.elf'
    echo '    vmlinux: ./test_case/vmlinux'
    echo '    boot_kernel: ./test_case/bzImage'
    echo '    kernel_source: /path/to/linux'
    echo ""
    echo "  也可以用 test_assets/ 内置用例快速测试:"
    echo "    python main.py test_assets/deadlock/input.txt --config config.json"
    echo "    python main.py test_assets/deadlock_arm64/input.txt --config config.json  # arm64"
    echo ""
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
    echo -e "${GREEN}"
    echo "======================================"
    echo "  Lumen 内核维护工作流系统 - 部署"
    echo "======================================"
    echo -e "${NC}"

    git submodule update --init --recursive
    check_python
    preflight_check
    create_virtualenv
    install_deps
    setup_env
    init_dirs
    build_dual_arch_tools
    provision_persistent_qemu_images
    build_semcode_mcp
    verify
    usage

    echo ""
    ok "部署完成！"
}

main
