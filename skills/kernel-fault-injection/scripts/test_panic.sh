#!/bin/sh
# test_panic.sh - Initramfs test script for direct kernel panic
# This runs inside QEMU guest

echo "=== Direct Kernel Panic Test ==="
echo "Loading crash_panic module..."
echo "This will call panic() immediately"

insmod /modules/crash_panic.ko

# Kernel panics immediately, script ends