#!/bin/sh
# test_stack-overflow.sh - Initramfs test script for stack overflow
# This runs inside QEMU guest

echo "=== Stack Overflow Test ==="
echo "Loading crash_stack_overflow module..."
echo "This will recursively call function until stack overflows"

insmod /modules/crash_stack_overflow.ko

echo "Module loaded, stack overflow in progress..."

# Stack overflow triggers panic, script ends