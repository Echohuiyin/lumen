#!/bin/bash
# run_fault_injection.sh - Main entry point for kernel fault injection
# Usage: ./run_fault_injection.sh <fault_type> [--arch x86_64|arm64] [--kernel <path>] [--output <dir>]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
MODULES_DIR="${SKILL_DIR}/modules"
PROJECT_ROOT="$(dirname "$(dirname "$SKILL_DIR")")"

FAULT_TYPE=""
ARCH="x86_64"
KERNEL=""
OUTPUT_DIR=""
TIMEOUT=60
KERNEL_DIR="/home/liumingrui/code/OLK-6.6"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        nullptr|softlockup|deadlock|panic|stack-overflow)
            FAULT_TYPE="$1"
            shift
        ;;
        --arch)
            ARCH="$2"
            shift 2
        ;;
        --kernel)
            KERNEL="$2"
            shift 2
        ;;
        --output)
            OUTPUT_DIR="$2"
            shift 2
        ;;
        --timeout)
            TIMEOUT="$2"
            shift 2
        ;;
        --kernel-dir)
            KERNEL_DIR="$2"
            shift 2
        ;;
        *)
            echo "Unknown option: $1"
            exit 1
        ;;
    esac
done

if [ -z "$FAULT_TYPE" ]; then
    echo "Usage: $0 <fault_type> [--arch x86_64|arm64] [--kernel <path>]"
    echo ""
    echo "Fault types: nullptr, softlockup, deadlock, panic, stack-overflow"
    exit 1
fi

# Set output directory
if [ -z "$OUTPUT_DIR" ]; then
    OUTPUT_DIR="${PROJECT_ROOT}/test_outputs/${FAULT_TYPE}_${ARCH}"
fi

echo "=== Kernel Fault Injection ==="
echo "Fault Type: $FAULT_TYPE"
echo "Architecture: $ARCH"
echo "Kernel Dir: $KERNEL_DIR"
echo "Output: $OUTPUT_DIR"
echo "Timeout: ${TIMEOUT}s"
echo

mkdir -p "$OUTPUT_DIR"

# Step 1: Build kernel if not provided
if [ -z "$KERNEL" ]; then
    echo "[1/5] Building kernel with fault injection configs..."

    # Call kernel-build skill
    if [ -f "${PROJECT_ROOT}/skills/kernel-build/SKILL.md" ]; then
        echo "Using /kernel-build skill..."
        echo "/kernel-build FW_CFG_SYSFS FW_CFG_SYSFS_CMDLINE DEBUG_INFO_DWARF4 PANIC_ON_OOPS CRASH_CORE KEXEC PROC_VMCORE DETECT_SOFTLOCKUP BOOTPARAM_SOFTLOCKUP_PANIC DETECT_HUNG_TASK DEFAULT_HUNG_TASK_TIMEOUT BOOTPARAM_HUNG_TASK_PANIC --arch ${ARCH} --jobs 32"
    fi

    # Check kernel output
    if [ "$ARCH" = "x86_64" ]; then
        # QEMU needs bzImage for booting, vmlinux is for crash analysis
        KERNEL="${KERNEL_DIR}/arch/x86/boot/bzImage"
        VMLINUX="${KERNEL_DIR}/vmlinux"
    elif [ "$ARCH" = "arm64" ]; then
        KERNEL="${KERNEL_DIR}/arch/arm64/boot/Image"
        VMLINUX="${KERNEL_DIR}/vmlinux"
    fi

    if [ ! -f "$KERNEL" ]; then
        echo "ERROR: Kernel not found at $KERNEL"
        echo "Please run: /kernel-build ... --arch ${ARCH}"
        exit 1
    fi
else
    echo "[1/5] Using pre-built kernel: $KERNEL"
fi

# Step 2: Build fault module
echo
echo "[2/5] Building fault module: crash_${FAULT_TYPE}.ko..."

MODULE_NAME="crash_${FAULT_TYPE}"

# Map fault types to module names
case "$FAULT_TYPE" in
    stack-overflow)
        MODULE_NAME="crash_stack_overflow"
    ;;
esac

cd "$MODULES_DIR"

if [ "$ARCH" = "x86_64" ]; then
    make ARCH=x86_64 KDIR="${KERNEL_DIR}" modules
elif [ "$ARCH" = "arm64" ]; then
    make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- KDIR="${KERNEL_DIR}" modules
fi

if [ ! -f "${MODULE_NAME}.ko" ]; then
    echo "ERROR: Module ${MODULE_NAME}.ko not built"
    exit 1
fi

cp "${MODULE_NAME}.ko" "$OUTPUT_DIR/"
echo "✓ Module: ${OUTPUT_DIR}/${MODULE_NAME}.ko"

# Step 3: Create initramfs
echo
echo "[3/5] Creating initramfs with fault module..."

INITRAMFS="${OUTPUT_DIR}/initramfs.cpio.gz"

# Use create_initramfs script
if [ -f "${PROJECT_ROOT}/skills/qemu-test/scripts/create_initramfs.sh" ]; then
    bash "${PROJECT_ROOT}/skills/qemu-test/scripts/create_initramfs.sh" \
        --arch "$ARCH" \
        --modules "$OUTPUT_DIR" \
        --test-script "${MODULES_DIR}/../scripts/test_${FAULT_TYPE}.sh" \
        --output "$INITRAMFS"
else
    echo "ERROR: create_initramfs.sh not found"
    exit 1
fi

if [ ! -f "$INITRAMFS" ]; then
    echo "ERROR: Initramfs not created"
    exit 1
fi

echo "✓ Initramfs: $INITRAMFS"

# Step 4: Run QEMU with vmcore capture
echo
echo "[4/5] Running QEMU and capturing vmcore..."

# Use run_vmcore_test.sh from crash-vmcore tool
if [ -f "${PROJECT_ROOT}/tools/crash-vmcore/scripts/run_vmcore_test.sh" ]; then
    bash "${PROJECT_ROOT}/tools/crash-vmcore/scripts/run_vmcore_test.sh" \
        "${FAULT_TYPE}_${ARCH}" \
        "$KERNEL" \
        "$INITRAMFS" \
        "$TIMEOUT"

    VMCORE="${PROJECT_ROOT}/test_outputs/${FAULT_TYPE}_${ARCH}/vmcore.elf"
else
    echo "ERROR: run_vmcore_test.sh not found"
    exit 1
fi

# Step 5: Validate and summarize
echo
echo "[5/5] Validating vmcore..."

SUMMARY="${OUTPUT_DIR}/analysis.txt"

cat > "$SUMMARY" << EOF
=== Fault Injection Summary ===
Fault Type: $FAULT_TYPE
Architecture: $ARCH
Kernel: $KERNEL
Module: ${MODULE_NAME}.ko
Timestamp: $(date)

=== Vmcore Status ===
EOF

if [ -f "$VMCORE" ] && [ -s "$VMCORE" ]; then
    VMCORE_SIZE=$(ls -lh "$VMCORE" | awk '{print $5}')
    echo "Vmcore: $VMCORE ($VMCORE_SIZE)" >> "$SUMMARY"

    # Check VMCOREINFO
    if readelf -n "$VMCORE" 2>/dev/null | grep -q VMCOREINFO; then
        echo "VMCOREINFO: Present ✓" >> "$SUMMARY"
        echo "Crash Compatible: Yes" >> "$SUMMARY"
    else
        echo "VMCOREINFO: Missing ✗" >> "$SUMMARY"
        echo "Crash Compatible: No - rebuild kernel with FW_CFG_SYSFS" >> "$SUMMARY"
    fi
else
    echo "Vmcore: Not captured ✗" >> "$SUMMARY"
fi

# Check crash evidence
echo "" >> "$SUMMARY"
echo "=== Crash Evidence ===" >> "$SUMMARY"
BOOT_LOG="${PROJECT_ROOT}/test_outputs/${FAULT_TYPE}_${ARCH}/boot.log"

if [ -f "$BOOT_LOG" ]; then
    grep -E "Kernel panic|Oops|NULL pointer|soft lockup|blocked for more than|BUG" "$BOOT_LOG" | head -5 >> "$SUMMARY" || echo "No crash pattern found" >> "$SUMMARY"
fi

# Next steps
echo "" >> "$SUMMARY"
echo "=== Next Steps ===" >> "$SUMMARY"
echo "Analyze vmcore with:" >> "$SUMMARY"
# Use vmlinux for crash analysis, not bzImage
if [ "$ARCH" = "x86_64" ]; then
    CRASH_KERNEL="${KERNEL_DIR}/vmlinux"
else
    CRASH_KERNEL="${KERNEL_DIR}/vmlinux"
fi
echo "  /vmcore-analyzer $CRASH_KERNEL $VMCORE" >> "$SUMMARY"
echo "" >> "$SUMMARY"
echo "Or use crash directly:" >> "$SUMMARY"
echo "  ${PROJECT_ROOT}/tools/crash-vmcore/bin/crash $KERNEL $VMCORE" >> "$SUMMARY"

cat "$SUMMARY"

echo
echo "=== Test Complete ==="
echo "Output directory: $OUTPUT_DIR"
echo "Vmcore: ${VMCORE:-Not captured}"
echo "Log: ${BOOT_LOG:-N/A}"
echo "Summary: $SUMMARY"