#!/bin/bash
# Automated test execution wrapper for QEMU
# Usage: run_test.sh --arch <arch> --kernel <path> --test-script <path> [--timeout <seconds>]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ARCH="arm64"
KERNEL=""
TEST_SCRIPT=""
TIMEOUT=300
MEMORY="512M"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --arch)
            ARCH="$2"
            shift 2
        ;;
        --kernel)
            KERNEL="$2"
            shift 2
        ;;
        --test-script)
            TEST_SCRIPT="$2"
            shift 2
        ;;
        --timeout)
            TIMEOUT="$2"
            shift 2
        ;;
        --memory)
            MEMORY="$2"
            shift 2
        ;;
        *)
            echo "Unknown option: $1"
            exit 1
        ;;
    esac
done

# Validate inputs
if [ -z "$KERNEL" ]; then
    echo "ERROR: Kernel image not specified"
    exit 1
fi

if [ ! -f "$KERNEL" ]; then
    echo "ERROR: Kernel image not found: $KERNEL"
    exit 1
fi

if [ -z "$TEST_SCRIPT" ]; then
    echo "ERROR: Test script not specified"
    exit 1
fi

if [ ! -f "$TEST_SCRIPT" ]; then
    echo "ERROR: Test script not found: $TEST_SCRIPT"
    exit 1
fi

echo "========================================="
echo "  Automated Kernel Test Runner"
echo "========================================="
echo
echo "Architecture: $ARCH"
echo "Kernel: $KERNEL"
echo "Test script: $TEST_SCRIPT"
echo "Timeout: $TIMEOUT seconds"
echo

# Step 1: Create initramfs with test script
echo "Step 1: Creating initramfs with test script..."
INITRAMFS="/tmp/initramfs_test_$$_$(date +%s).cpio.gz"

$SCRIPT_DIR/create_initramfs.sh \
    --test-script "$TEST_SCRIPT" \
    --output "$INITRAMFS"

if [ ! -f "$INITRAMFS" ]; then
    echo "ERROR: Failed to create initramfs"
    exit 1
fi

echo

# Step 2: Select boot script based on architecture
echo "Step 2: Selecting boot script..."
case "$ARCH" in
    arm64|aarch64)
        BOOT_SCRIPT="$SCRIPT_DIR/boot_arm64.sh"
        ;;
    arm32|arm)
        BOOT_SCRIPT="$SCRIPT_DIR/boot_arm32.sh"
        ;;
    x86_64|x86)
        BOOT_SCRIPT="$SCRIPT_DIR/boot_x86.sh"
        ;;
    *)
        echo "ERROR: Unsupported architecture: $ARCH"
        echo "Supported: arm64, arm32, x86_64"
        exit 1
        ;;
esac

echo "Using: $BOOT_SCRIPT"
echo

# Step 3: Run QEMU test
echo "Step 3: Running test in QEMU..."
echo

LOG_DIR="qemu_logs"
mkdir -p "$LOG_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOG_DIR/test_${TIMESTAMP}.log"

$BOOT_SCRIPT \
    --kernel "$KERNEL" \
    --initrd "$INITRAMFS" \
    --timeout "$TIMEOUT" \
    --memory "$MEMORY" \
    2>&1 | tee "$LOG_FILE"

EXIT_STATUS=${PIPESTATUS[0]}

echo

# Step 4: Analyze results
echo "Step 4: Analyzing test results..."
echo

# Check for test completion markers
if grep -q "Test completed with status:" "$LOG_FILE"; then
    TEST_RESULT=$(grep "Test completed with status:" "$LOG_FILE" | tail -1 | grep -oP 'status: \K[0-9]+')

    echo "========================================="
    if [ "$TEST_RESULT" = "0" ]; then
        echo "✓ TEST PASSED"
    else
        echo "✗ TEST FAILED (exit code: $TEST_RESULT)"
    fi
    echo "========================================="
else
    echo "⚠ Test completion marker not found in log"
    echo "Boot may have failed or timed out"
fi

echo
echo "Test log: $LOG_FILE"
echo "Log size: $(du -h "$LOG_FILE" | cut -f1)"

# Show test output section
if grep -q "Running Test Script" "$LOG_FILE"; then
    echo
    echo "Test output:"
    sed -n '/Running Test Script/,/Test completed/p' "$LOG_FILE" | head -30
fi

# Cleanup
rm -f "$INITRAMFS"

exit $EXIT_STATUS