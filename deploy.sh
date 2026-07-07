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
    check_cmd cpio "apt install cpio" || ((fail_count++))
    check_cmd gzip "apt install gzip (usually pre-installed)" || ((fail_count++))
    check_cmd crash "apt install crash" || ((fail_count++))
    check_cmd claude "npm install -g @anthropic-ai/claude-code" || ((fail_count++))
    check_cmd git "apt install git" || ((fail_count++))

    # Optional: arm64 cross-arch analysis & reproduction
    # Not required for x86_64-only deployments
    echo ""
    info "=== arm64 跨架构分析 (可选) ==="
    check_cmd qemu-system-aarch64 "apt install qemu-system-arm (for arm64 QEMU boot)" || true
    check_cmd aarch64-linux-gnu-gcc "apt install gcc-aarch64-linux-gnu (for arm64 cross-compile)" || true
    check_cmd arm-linux-gnueabi-gcc "apt install gcc-arm-linux-gnueabi (for arm32 cross-compile)" || true

    # busybox: check both system and project prebuilt
    if command -v busybox &>/dev/null; then
        ok "busybox — found at $(command -v busybox)"
    elif [ -f Analysis-SKILL/tools/busybox/prebuilt/busybox_x86_64 ]; then
        ok "busybox x86_64 — Analysis-SKILL/tools/busybox/prebuilt/busybox_x86_64"
    else
        warn "busybox x86_64 — NOT FOUND (install: apt install busybox-static)"
        ((fail_count++))
    fi

    # arm64 busybox prebuilt (committed to repo)
    if [ -f Analysis-SKILL/tools/busybox/prebuilt/busybox_arm64 ]; then
        ok "busybox arm64 — Analysis-SKILL/tools/busybox/prebuilt/busybox_arm64"
    else
        warn "busybox arm64 — NOT FOUND (arm64 QEMU boot will fail; run: bash Analysis-SKILL/tools/build_busybox.sh --arch arm64)"
    fi

    # semcode MCP (optional)
    if command -v semcode-mcp &>/dev/null; then
        ok "semcode-mcp — found at $(command -v semcode-mcp)"
    elif [ -x "${HOME}/semcode/target/release/semcode-mcp" ]; then
        ok "semcode-mcp — found at ${HOME}/semcode/target/release/semcode-mcp"
    else
        warn "semcode-mcp — NOT FOUND (optional, kernel_expert will skip MCP tools)"
    fi

    # git submodule
    if [ -f Analysis-SKILL/CLAUDE.md ]; then
        ok "Analysis-SKILL submodule — present"
    else
        warn "Analysis-SKILL submodule — missing (run: git submodule update --init)"
        ((fail_count++))
    fi

    # busybox build script (if no busybox at all)
    if [ -f tools/build_busybox.sh ]; then
        ok "busybox build script — tools/build_busybox.sh"
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

# ── Kernel source (必须) ─────────────────────────────────────────────────────
# 内核源码目录，Crash 分析和语义搜索需要
export KERNEL_SOURCE_DIR="${HOME}/code/OLK-6.6"

# ── semcode MCP (可选) ───────────────────────────────────────────────────────
# 语义代码搜索 MCP 服务器，用于 kernel_expert 自动代码分析
export SEMCODE_MCP_BIN="${HOME}/semcode/target/release/semcode-mcp"
export SEMCODE_DB_DIR="${KERNEL_SOURCE_DIR}/.semcode.db"
ENVEOF
        warn "已创建 .env 模板 — 请编辑 .env 填写 API key 和 KERNEL_SOURCE_DIR"
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
            # Substitute env vars into template
            envsubst < config.json.template > config.json 2>/dev/null || \
                cp config.json.template config.json
            ok "已从模板创建 config.json"
            warn "请检查 config.json 中的 API key 配置"
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
    import langgraph; import langchain; import langchain_openai
    print('  核心模块: langgraph/langchain/langchain-openai — OK')
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
    echo '    kernel_source: $KERNEL_SOURCE_DIR'
    echo ""
    echo "  也可以用 test_assets/ 内置用例快速测试:"
    echo "    python main.py test_assets/deadlock/input.txt --config config.json"
    echo ""
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
    echo -e "${GREEN}"
    echo "======================================"
    echo "  Lumen 内核维护工作流系统 - 部署"
    echo "======================================"
    echo -e "${NC}"

    check_python
    preflight_check
    create_virtualenv
    install_deps
    setup_env
    init_dirs
    git submodule update --init 2>/dev/null || true
    verify
    usage

    echo ""
    ok "部署完成！"
}

main
