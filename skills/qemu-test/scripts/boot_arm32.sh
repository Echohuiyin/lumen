#!/bin/bash
# Boot ARM32 kernel in QEMU
# Usage: boot_arm32.sh --kernel <path> --initrd <path> [--interactive] [--timeout <seconds>]

set -e

KERNEL=""
INITRD=""
INTERACTIVE=0
TIMEOUT=300
MEMORY="512M"
CPU_COUNT=2

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --kernel)
            KERNEL="$2"
            shift 2
        ;;
        --initrd)
            INITRD="$2"
            shift 2
        ;;
        --interactive)
            INTERACTIVE=1
            shift
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

if [ -z "$INITRD" ]; then
    echo "ERROR: Initramfs not specified"
    exit 1
fi

if [ ! -f "$INITRD" ]; then
    echo "ERROR: Initramfs not found: $INITRD"
    exit 1
fi

# Check QEMU availability
if ! command -v qemu-system-arm &> /dev/null; then
    echo "ERROR: qemu-system-arm not found"
    echo "Please install QEMU for ARM:"
    echo "  Ubuntu/Debian: apt install qemu-system-arm"
    exit 1
fi

echo "========================================="
echo "  Booting ARM32 Kernel in QEMU"
echo "========================================="
echo
echo "Kernel: $KERNEL ($(du -h "$KERNEL" | cut -f1))"
echo "Initramfs: $INITRD ($(du -h "$INITRD" | cut -f1))"
echo "Architecture: ARM32 (arm)"
echo "Machine: virt"
echo "CPU: cortex-a15 ($CPU_COUNT cores)"
echo "Memory: $MEMORY"
echo "Timeout: $TIMEOUT seconds"
echo "Mode: $([ $INTERACTIVE -eq 1 ] && echo 'interactive' || echo 'automated')"
echo

# Kernel command line
CMDLINE="console=ttyAMA0 root=/dev/ram rw"
if [ $INTERACTIVE -eq 0 ]; then
    CMDLINE="$CMDLINE panic=1"
fi

# QEMU command
QEMU_CMD="qemu-system-arm \
    -M virt \
    -cpu cortex-a15 \
    -smp $CPU_COUNT \
    -m $MEMORY \
    -nographic \
    -kernel $KERNEL \
    -initrd $INITRD \
    -append \"$CMDLINE\""

echo "Launching QEMU..."
echo

# Run QEMU
if [ $INTERACTIVE -eq 1 ]; then
    eval $QEMU_CMD
else
    LOG_FILE="/tmp/qemu_boot_$$_$(date +%s).log"

    timeout "$TIMEOUT" eval $QEMU_CMD 2>&1 | tee "$LOG_FILE"

    EXIT_STATUS=$?

    echo
    echo "========================================="

    if [ $EXIT_STATUS -eq 0 ]; then
        echo "✓ Boot completed successfully"
    elif [ $EXIT_STATUS -eq 124 ]; then
        echo "⚠ Timeout reached ($TIMEOUT seconds)"
    else
        echo "✗ Boot failed (exit code: $EXIT_STATUS)"
    fi

    echo "========================================="
    echo
    echo "Boot log saved to: $LOG_FILE"
    echo "Log size: $(du -h "$LOG_FILE" | cut -f1)"

    echo
    echo "Last boot messages:"
    tail -20 "$LOG_FILE"
fi

exit $EXIT_STATUS