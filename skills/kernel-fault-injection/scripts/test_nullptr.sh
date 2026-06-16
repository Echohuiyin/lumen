#!/bin/sh
# test_nullptr.sh - Initramfs test script for NULL pointer dereference
# This runs inside QEMU guest

echo "=== NULL Pointer Dereference Test ==="
echo "Loading crash_nullptr module..."

insmod /modules/crash_nullptr.ko

echo "Module loaded, crash should occur immediately"
echo "QEMU will capture vmcore on panic"

# Module triggers panic, script ends here