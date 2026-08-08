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
CODEX_CLI="${LUMEN_CODEX_CLI:-codex}"
CODEX_RUNTIME_HOME="${LUMEN_CODEX_RUNTIME_HOME:-runtime/codex-home}"
CODEX_AUTH_SOURCE="${LUMEN_CODEX_AUTH_SOURCE:-${CODEX_HOME:-${HOME}/.codex}/auth.json}"
CODEX_AUTH_TARGET="${CODEX_RUNTIME_HOME}/.codex/auth.json"
CODEX_SKILLS_DIR="${LUMEN_CODEX_SKILLS_DIR:-.agents/skills}"
CRASH_SOURCE_DIR="${CRASH_SOURCE_DIR:-runtime/crash-source}"
CRASH_BIN_DIRS="${LUMEN_CRASH_BIN_DIRS:-}"
CRASH_REPO="${CRASH_REPO:-https://github.com/crash-utility/crash.git}"
CRASH_REF="${CRASH_REF:-9.0.2}"
GNU_MIRROR="${LUMEN_GNU_MIRROR:-}"
CRASH_BUILDER="Analysis-SKILL/tools/crash-vmcore/scripts/build_crash.sh"
BUSYBOX_BUILDER="Analysis-SKILL/tools/build_busybox.sh"
SEMCODE_SOURCE_DIR="Analysis-SKILL/tools/semcode"
SEMCODE_REPO="${SEMCODE_REPO:-https://github.com/facebookexperimental/semcode.git}"
SEMCODE_MCP_BIN="${SEMCODE_SOURCE_DIR}/target/release/semcode-mcp"
PERSISTENT_QEMU_PROVISIONER="scripts/provision_qemu_ssh_image.sh"
HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
    x86_64|amd64|aarch64|arm64) ;;
    *)
        fail "不支持的主机架构: $HOST_ARCH（仅支持 x86_64/amd64 或 arm64/aarch64）"
        exit 1
        ;;
esac

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
    check_cmd "$CODEX_CLI" "npm install -g @openai/codex" || ((fail_count++))
    check_cmd bwrap "apt install bubblewrap" || ((fail_count++))
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
    check_cmd x86_64-linux-gnu-gcc "apt install gcc-x86-64-linux-gnu" || ((fail_count++))
    check_cmd aarch64-linux-gnu-gcc "apt install gcc-aarch64-linux-gnu" || ((fail_count++))
    check_cmd cargo "安装 Rust/cargo（国内网络可使用 rsproxy）" || ((fail_count++))
    check_cmd rustc "安装 Rust/rustc（国内网络可使用 rsproxy）" || ((fail_count++))
    check_cmd mountpoint "apt install util-linux" || ((fail_count++))

    # ── Crash utility: arch-specific binaries ────────────────────────────────
    # crash is compiled with a single TARGET arch hardcoded. An x86_64-targeted
    # crash CANNOT parse an arm64 vmcore ("machine type mismatch" → "not a
    # supported file format"). Lumen auto-selects crash_<arch> based on
    # vmlinux's ELF e_machine. Verify the binaries exist.
    # Lumen looks for arch-suffixed binaries at:
    #   Analysis-SKILL/tools/crash/crash_<arch>  (source-built)
    #   directories listed in LUMEN_CRASH_BIN_DIRS or the executable PATH
    echo ""
    info "=== crash 二进制 (按架构区分) ==="
    # Check for arch-suffixed binaries in Lumen's lookup paths
    local crash_x86_64_found=""
    local crash_arm64_found=""
    local crash_bin_dirs=("$CRASH_SOURCE_DIR")
    local configured_crash_bin_dirs="${LUMEN_CRASH_BIN_DIRS:-$CRASH_BIN_DIRS}"
    if [ -n "$configured_crash_bin_dirs" ]; then
        local old_ifs="$IFS"
        IFS=:
        read -r -a extra_crash_dirs <<< "$configured_crash_bin_dirs"
        IFS="$old_ifs"
        crash_bin_dirs+=("${extra_crash_dirs[@]}")
    fi
    for d in "${crash_bin_dirs[@]}"; do
        if [ -z "$crash_x86_64_found" ] && [ -x "${d}/crash_x86_64" ]; then
            crash_x86_64_found="${d}/crash_x86_64"
        fi
        if [ -z "$crash_arm64_found" ] && [ -x "${d}/crash_arm64" ]; then
            crash_arm64_found="${d}/crash_arm64"
        fi
    done

    if [ -z "$crash_x86_64_found" ] && command -v crash_x86_64 &>/dev/null; then
        crash_x86_64_found="$(command -v crash_x86_64)"
    fi
    if [ -z "$crash_arm64_found" ] && command -v crash_arm64 &>/dev/null; then
        crash_arm64_found="$(command -v crash_arm64)"
    fi

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
    if [ -d Analysis-SKILL/skills ]; then
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
        return 1
    else
        echo ""
        ok "=== 所有外部依赖已就绪 ==="
    fi
}

# ── Codex CLI ────────────────────────────────────────────────────────────────
ensure_codex_cli() {
    if [ -n "${LUMEN_CODEX_CLI:-}" ]; then
        CODEX_CLI="$LUMEN_CODEX_CLI"
    fi
    if command -v "$CODEX_CLI" &>/dev/null; then
        CODEX_CLI="$(command -v "$CODEX_CLI")"
    elif [ "$CODEX_CLI" = "codex" ] && command -v npm &>/dev/null \
        && [ -x "$(npm prefix -g)/bin/codex" ]; then
        CODEX_CLI="$(npm prefix -g)/bin/codex"
        export PATH="$(dirname "$CODEX_CLI"):${PATH}"
        hash -r
    elif [ "$CODEX_CLI" != "codex" ]; then
        fail "Configured Codex CLI is missing: $CODEX_CLI"
        return 1
    elif ! command -v npm &>/dev/null; then
        fail "Codex CLI is missing and npm is unavailable; install Node.js/npm first"
        return 1
    else
        info "Installing Codex CLI with npm"
        npm install -g @openai/codex
        CODEX_CLI="$(npm prefix -g)/bin/codex"
        export PATH="$(dirname "$CODEX_CLI"):${PATH}"
        hash -r
    fi

    if [ ! -x "$CODEX_CLI" ]; then
        fail "Codex CLI installation did not produce an executable: $CODEX_CLI"
        return 1
    fi
    export LUMEN_CODEX_CLI="$CODEX_CLI"
    if [ -f .env ] && ! grep -Eq '^[[:space:]]*(export[[:space:]]+)?LUMEN_CODEX_CLI=' .env; then
        printf '\n# Resolved by deploy.sh; source .env before running Lumen.\n' >> .env
        printf 'export LUMEN_CODEX_CLI=%q\n' "$CODEX_CLI" >> .env
        ok "Codex CLI path recorded in .env"
    fi
    ok "Codex CLI: $($CODEX_CLI --version)"
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
        if [ -n "${LUMEN_PIP_INDEX_URL:-}" ]; then
            pip install --index-url "$LUMEN_PIP_INDEX_URL" -r requirements.txt -q
        else
            pip install -r requirements.txt -q
        fi
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
# Set these values for the provider selected by your deployment.
# The endpoint and model are deployment inputs, not project constants.
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-}"
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-}"

# Codex Kernel Expert runtime selection. Keep these as environment inputs so
# `source .env` and one-shot deployment runs use the same effective settings.
export LUMEN_CODEX_MODEL="${LUMEN_CODEX_MODEL:-}"
export LUMEN_CODEX_REASONING_EFFORT="${LUMEN_CODEX_REASONING_EFFORT:-xhigh}"
export LUMEN_CODEX_SERVICE_TIER="${LUMEN_CODEX_SERVICE_TIER:-}"

# ── RAG Embedding API ────────────────────────────────────────────────────────
# Required by knowledge_search and knowledge_base Chroma import.
# Any OpenAI-compatible /v1/embeddings endpoint can be used.
export EMBEDDING_BASE_URL="${EMBEDDING_BASE_URL:-}"
export EMBEDDING_MODEL="${EMBEDDING_MODEL:-}"
export EMBEDDING_API_KEY="${EMBEDDING_API_KEY:-}"

ENVEOF
        warn "已创建 .env 模板 — 请编辑 .env 填写 LLM 和 embedding 配置"
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
# Project-isolated Codex auth and repository skill discovery.
setup_codex_runtime() {
    echo ""
    info "=== Codex project-isolated runtime ==="

    mkdir -p "${CODEX_RUNTIME_HOME}/.codex" "$CODEX_SKILLS_DIR"
    chmod 700 "$CODEX_RUNTIME_HOME" "${CODEX_RUNTIME_HOME}/.codex"

    if [ -n "${CODEX_API_KEY:-}" ]; then
        ok "Codex will use invocation-scoped CODEX_API_KEY"
    else
        if [ ! -f "$CODEX_AUTH_SOURCE" ]; then
            fail "Missing Codex authentication: $CODEX_AUTH_SOURCE"
            fail "Run 'codex login' first or set LUMEN_CODEX_AUTH_SOURCE/CODEX_API_KEY"
            return 1
        fi
        if [ "$(readlink -f "$CODEX_AUTH_SOURCE")" != "$(readlink -f "$CODEX_AUTH_TARGET" 2>/dev/null || true)" ]; then
            install -m 600 "$CODEX_AUTH_SOURCE" "$CODEX_AUTH_TARGET"
        else
            chmod 600 "$CODEX_AUTH_TARGET"
        fi
        if [ ! -s "$CODEX_AUTH_TARGET" ]; then
            fail "Codex auth provisioning failed: $CODEX_AUTH_TARGET"
            return 1
        fi
        ok "Codex auth provisioned at $CODEX_AUTH_TARGET (mode 600)"
    fi

    local source_dir skill_dir skill_name target relative_target
    for source_dir in Analysis-SKILL/skills skills; do
        [ -d "$source_dir" ] || continue
        for skill_dir in "$source_dir"/*; do
            [ -f "$skill_dir/SKILL.md" ] || continue
            skill_name="$(basename "$skill_dir")"
            # Kernel Expert is restricted to defensive userspace diagnostics;
            # do not expose the legacy fault-injection skill, whose examples
            # use kernel modules and UAF injection and can misclassify inputs.
            if [ "$skill_name" = "kernel-fault-injection" ]; then
                continue
            fi
            target="${CODEX_SKILLS_DIR}/${skill_name}"
            if [ -e "$target" ] && [ ! -L "$target" ]; then
                fail "Refusing to replace existing Codex skill path: $target"
                return 1
            fi
            if [ -L "$target" ] && [ "$(readlink -f "$target")" = "$(readlink -f "$skill_dir")" ]; then
                continue
            fi
            if [ -L "$target" ]; then
                unlink "$target"
            fi
            relative_target="$(realpath --relative-to="$CODEX_SKILLS_DIR" "$skill_dir")"
            ln -s "$relative_target" "$target"
        done
    done
    if ! find -L "$CODEX_SKILLS_DIR" -mindepth 2 -maxdepth 2 -name SKILL.md -print -quit | grep -q .; then
        fail "No project Codex skills were provisioned under $CODEX_SKILLS_DIR"
        return 1
    fi
    ok "Project Codex skills provisioned under $CODEX_SKILLS_DIR"

    if [ -z "${CODEX_API_KEY:-}" ]; then
        HOME="$CODEX_RUNTIME_HOME" CODEX_HOME="${CODEX_RUNTIME_HOME}/.codex" \
            "$CODEX_CLI" login status
    fi
}

init_dirs() {
    mkdir -p knowledge_base outputs
    ok "目录结构已创建 (knowledge_base/ outputs/)"
}

# ── Dual-architecture tools ──────────────────────────────────────────────────
build_crash_binary() {
    local target="$1" output="$2"
    local make_target host_triplet target_marker previous_target
    local gdb_target_marker gdb_previous_target
    if [ -x "$output" ]; then
        ok "crash_${target} 已存在: $output"
        return
    fi

    case "$target" in
        x86_64) make_target="X86_64" ;;
        arm64) make_target="ARM64" ;;
        *) fail "不支持的 crash 目标架构: $target"; exit 1 ;;
    esac
    if [ ! -f "$CRASH_SOURCE_DIR/Makefile" ]; then
        info "获取固定版本 crash 源码: $CRASH_REF"
        local runtime_dir
        runtime_dir="$(dirname "$CRASH_SOURCE_DIR")"
        if [ ! -d "$runtime_dir" ]; then
            mkdir -p "$runtime_dir"
        fi
        if [ ! -w "$runtime_dir" ]; then
            sudo chown "$(id -u):$(id -g)" "$runtime_dir"
        fi
        git clone --depth 1 --branch "$CRASH_REF" "$CRASH_REPO" "$CRASH_SOURCE_DIR"
    fi
    if [ -n "$GNU_MIRROR" ]; then
        sed -i "s|http://ftp.gnu.org/gnu|${GNU_MIRROR%/}|g" "$CRASH_SOURCE_DIR/Makefile"
    else
        info "LUMEN_GNU_MIRROR 未配置，保留 crash 工具的上游 GNU 源"
    fi
    if [ ! -f "$CRASH_SOURCE_DIR/gdb-16.2.patch" ]; then
        git show HEAD:gdb-16.2.patch > "$CRASH_SOURCE_DIR/gdb-16.2.patch"
    fi
    # crash's release tarball build expects this generated exclusion list,
    # but it is not tracked by the upstream repository.
    if [ ! -f "$CRASH_SOURCE_DIR/gdb.files" ]; then
        printf 'dummy\n' > "$CRASH_SOURCE_DIR/gdb.files"
    fi
    host_triplet="$(gcc -dumpmachine)"
    target_marker="$CRASH_SOURCE_DIR/.lumen-crash-target"
    previous_target="$(cat "$target_marker" 2>/dev/null || true)"
    # The sidecar marker was added after the first dual-target deployments and
    # may be absent or stale in an existing checkout.  crash itself records
    # the target used to configure GDB in gdb-16.2/crash.target; use that
    # marker as the source of truth whenever a generated GDB tree exists.
    gdb_target_marker="$CRASH_SOURCE_DIR/gdb-16.2/crash.target"
    gdb_previous_target="$(cat "$gdb_target_marker" 2>/dev/null || true)"
    if [ -d "$CRASH_SOURCE_DIR/gdb-16.2" ] && {
        [ -z "$gdb_previous_target" ] || [ "$gdb_previous_target" != "$make_target" ] ||
        { [ -n "$previous_target" ] && [ "$previous_target" != "$make_target" ] &&
          [ "$gdb_previous_target" != "$make_target" ]; };
    }; then
        info "清理不匹配的 crash GDB 构建目录 (已有: ${gdb_previous_target:-未知}, 需要: $make_target)"
        find "$CRASH_SOURCE_DIR" -maxdepth 1 -type d -name 'gdb-[0-9]*' -exec rm -rf {} +
    fi

    info "从固定源码构建 crash_${target}（首次构建会编译 GDB，需数分钟）"
    (
        cd "$CRASH_SOURCE_DIR"
        make clean || true
        # crash's configure wrapper reads the lower-case `target` make
        # variable and rewrites TARGET/GDB settings accordingly.  Passing only
        # TARGET leaves the wrapper on its host-default (usually X86_64), so a
        # subsequent ARM64 build can reuse an X86_64 GDB tree and fail at the
        # final link.  Keep the explicit host triplet for native GDB builds.
        make target="$make_target" GDB_CONF_FLAGS="--host=$host_triplet" -j"$(nproc)"
        install -Dm 0755 crash "$OLDPWD/$output"
    )
    printf '%s\n' "$make_target" > "$target_marker"
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
    local x86_toolchain_dir="runtime/toolchains/x86_64-linux-gnu/bin"
    if [[ "$HOST_ARCH" == "aarch64" || "$HOST_ARCH" == "arm64" ]]; then
        mkdir -p "$x86_toolchain_dir"
        for tool in gcc g++ ld as ar nm objcopy objdump ranlib strip readelf; do
            if command -v "x86_64-linux-gnu-$tool" >/dev/null 2>&1; then
                ln -sf "$(command -v "x86_64-linux-gnu-$tool")" "$x86_toolchain_dir/$tool"
            fi
        done
    fi
    build_busybox() {
        local target="$1"
        if [[ "$target" == "x86_64" && ( "$HOST_ARCH" == "aarch64" || "$HOST_ARCH" == "arm64" ) ]]; then
            PATH="$(cd "$x86_toolchain_dir" && pwd):$PATH" bash "$BUSYBOX_BUILDER" --arch "$target" --clean
        else
            bash "$BUSYBOX_BUILDER" --arch "$target" --clean
        fi
    }
    if [ ! -x "${busybox_dir}/busybox_x86_64" ]; then
        info "从源码构建 BusyBox x86_64"
        build_busybox x86_64
    else
        ok "BusyBox x86_64 已存在: ${busybox_dir}/busybox_x86_64"
    fi
    if [ ! -x "${busybox_dir}/busybox_arm64" ]; then
        info "从源码构建 BusyBox arm64"
        build_busybox arm64
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

    # Keep an already provisioned submodule intact.  Semcode is intentionally
    # built under Analysis-SKILL and makes that worktree dirty; forcing a
    # checkout here would discard the deployed toolchain.
    if ! git submodule update --init --recursive; then
        warn "Analysis-SKILL 有本地部署产物，保留当前工作区并继续"
    fi
    check_python
    # Load deployment inputs before preflight so explicitly configured paths,
    # including LUMEN_CRASH_BIN_DIRS, are visible to all checks.
    setup_env
    ensure_codex_cli
    preflight_check
    create_virtualenv
    install_deps
    setup_codex_runtime
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
