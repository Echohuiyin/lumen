#!/bin/bash
# Create minimal initramfs for QEMU kernel testing
# Usage: create_initramfs.sh [--test-script <path>] [--modules <path>] [--interactive] [--arch <arch>]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
BUSYBOX_VERSION="1.36.1"
ARCH="${ARCH:-arm64}"  # Default to arm64, can be overridden

OUTPUT_DIR="/tmp/initramfs_${ARCH}"
OUTPUT_FILE="/tmp/initramfs_${ARCH}.cpio.gz"

TEST_SCRIPT=""
MODULES_DIR=""
INTERACTIVE=0

# 架构映射
detect_busybox_arch() {
    local busybox="$1"
    if [ ! -f "$busybox" ]; then
        echo "unknown"
        return 1
    fi
    local info=$(file "$busybox" 2>/dev/null)

    if echo "$info" | grep -qE "ARM aarch64"; then
        echo "arm64"
    elif echo "$info" | grep -qE "ARM,"; then
        echo "arm32"
    elif echo "$info" | grep -qE "x86-64"; then
        echo "x86_64"
    else
        echo "unknown"
    fi
}

# 查找并验证 busybox
find_busybox() {
    local target_arch="$1"
    local candidates=(
        # 优先使用项目内预编译版本
        "${PROJECT_ROOT}/tools/busybox/prebuilt/busybox_${target_arch}"
        # 新构建路径
        "/tmp/busybox_build_${target_arch}/busybox-${BUSYBOX_VERSION}/busybox"
        # 旧构建路径（兼容）
        "/tmp/busybox_build/busybox-${BUSYBOX_VERSION}/busybox"
        # 系统busybox
        "/usr/bin/busybox"
        "/bin/busybox"
    )

    for busybox in "${candidates[@]}"; do
        if [ -x "$busybox" ]; then
            local detected_arch=$(detect_busybox_arch "$busybox")
            if [ "$detected_arch" = "$target_arch" ]; then
                echo "$busybox"
                return 0
            else
                echo "⚠️  Busybox at $busybox is $detected_arch, but need $target_arch (skipping)" >&2
            fi
        fi
    done
    return 1
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --test-script)
            TEST_SCRIPT="$2"
            shift 2
        ;;
        --modules)
            MODULES_DIR="$2"
            shift 2
        ;;
        --interactive)
            INTERACTIVE=1
            shift
        ;;
        --output)
            OUTPUT_FILE="$2"
            shift 2
        ;;
        --arch)
            ARCH="$2"
            OUTPUT_DIR="/tmp/initramfs_${ARCH}"
            OUTPUT_FILE="/tmp/initramfs_${ARCH}.cpio.gz"
            shift 2
        ;;
        *)
            echo "Unknown option: $1"
            exit 1
        ;;
    esac
done

echo "Creating minimal initramfs for $ARCH..."

# Clean and create directory structure
rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"/{bin,dev,proc,sys,etc,lib,modules}

# Find busybox with architecture detection
BUSYBOX=$(find_busybox "$ARCH")

if [ -n "$BUSYBOX" ] && [ -f "$BUSYBOX" ]; then
    DETECTED_ARCH=$(detect_busybox_arch "$BUSYBOX")
    echo "✓ Busybox found: $BUSYBOX"
    echo "  Architecture: $DETECTED_ARCH (matches target: $ARCH)"

    # Check static linking
    if ldd "$BUSYBOX" 2>&1 | grep -q "not a dynamic executable"; then
        echo "  Static linking: yes (ideal for initramfs)"
        cp "$BUSYBOX" "$OUTPUT_DIR/bin/busybox"
    else
        echo "  Static linking: no (may need additional libraries)"
        echo "  Recommend: rebuild with CONFIG_STATIC=y"
        cp "$BUSYBOX" "$OUTPUT_DIR/bin/busybox"
        # Copy required libraries (basic attempt)
        ldd "$BUSYBOX" | grep -o "/lib[^ ]*" | while read lib; do
            cp "$lib" "$OUTPUT_DIR/lib/" 2>/dev/null || true
        done
    fi
else
    echo "✗ No valid busybox found for $ARCH"
    echo "  Architecture mismatch may cause QEMU boot failure"
    echo ""
    echo "Solution: Build busybox for $ARCH"
    echo "  cd ${PROJECT_ROOT}/tools"
    echo "  ./build_busybox.sh --arch $ARCH --clean"
    echo ""
    echo "Or for x86_64 native testing:"
    echo "  sudo apt install busybox-static"
    exit 1
fi

# Create busybox symlinks
cd "$OUTPUT_DIR/bin"
for cmd in sh ash cat ls mkdir mount umount echo sleep poweroff reboot dmesg grep \
           uname lsmod insmod rmmod modprobe ifconfig ip route ping wget curl \
           vi less more head tail wc awk sed tr cut sort uniq diff find xargs \
           test "[" "bracket" true false; do
    ln -sf busybox "$cmd" 2>/dev/null || true
done
cd -

# Create init script
INIT_SCRIPT="$OUTPUT_DIR/init"
cat > "$INIT_SCRIPT" << 'EOF'
#!/bin/sh

# Mount essential filesystems
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev 2>/dev/null || {
    mknod /dev/console c 5 1
    mknod /dev/null c 1 3
    mknod /dev/tty c 5 0
}

export PATH=/bin:/sbin:/usr/bin:/usr/sbin
export HOME=/root

echo
echo "========================================="
echo "  Minimal Initramfs for Kernel Testing"
echo "========================================="
echo
uname -r
uname -m
echo

# Show recent kernel messages
echo "Recent kernel messages:"
dmesg | tail -n 20
echo

# Check if test.sh exists - it handles module loading
if test -f /test.sh; then
    echo "========================================="
    echo "  Running Test Script"
    echo "========================================="
    echo
    sh /test.sh
    echo
    # Test script should handle everything including shutdown
    # Don't auto-poweroff - let test.sh or kernel panic handle it
else
    # No test script - load modules if provided
    if ls /modules/*.ko 2>/dev/null; then
        echo "Loading kernel modules..."
        for mod in /modules/*.ko; do
            insmod "$mod"
            echo "  Loaded: $mod"
        done
        echo
    fi

    # Interactive mode or shutdown
    if test "$INTERACTIVE" = "1"; then
        echo "Interactive mode - type 'poweroff' to exit"
        exec /bin/sh
    else
        echo "Automated test complete"
        sleep 2
        poweroff -f
    fi
fi
EOF

chmod +x "$INIT_SCRIPT"

# Copy test script if provided
if [ -n "$TEST_SCRIPT" ] && [ -f "$TEST_SCRIPT" ]; then
    cp "$TEST_SCRIPT" "$OUTPUT_DIR/test.sh"
    chmod +x "$OUTPUT_DIR/test.sh"
    echo "Test script included: $TEST_SCRIPT"
fi

# Copy modules if provided
if [ -n "$MODULES_DIR" ] && [ -d "$MODULES_DIR" ]; then
    cp -r "$MODULES_DIR"/*.ko "$OUTPUT_DIR/modules/" 2>/dev/null || true
    echo "Modules included from: $MODULES_DIR"
fi

# Set interactive flag in init
if [ $INTERACTIVE -eq 1 ]; then
    sed -i 's/INTERACTIVE=0/INTERACTIVE=1/' "$INIT_SCRIPT"
fi

# Create basic device nodes (fallback)
mknod "$OUTPUT_DIR/dev/console" c 5 1 2>/dev/null || true
mknod "$OUTPUT_DIR/dev/null" c 1 3 2>/dev/null || true
mknod "$OUTPUT_DIR/dev/tty" c 5 0 2>/dev/null || true

# Create cpio archive
echo "Creating initramfs archive..."
cd "$OUTPUT_DIR"
find . | cpio -o -H newc 2>/dev/null | gzip > "$OUTPUT_FILE"
cd -

# Get size
SIZE=$(du -h "$OUTPUT_FILE" | cut -f1)

echo
echo "✓ Initramfs created successfully"
echo "  Location: $OUTPUT_FILE"
echo "  Size: $SIZE"
echo "  Interactive: $INTERACTIVE"
if [ -n "$TEST_SCRIPT" ]; then
    echo "  Test script: $(basename $TEST_SCRIPT)"
fi
echo

# Cleanup
rm -rf "$OUTPUT_DIR"

exit 0