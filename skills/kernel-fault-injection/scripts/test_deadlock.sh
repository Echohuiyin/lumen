#!/bin/sh
# test_deadlock.sh - Initramfs test script for mutex ABBA deadlock
# This runs inside QEMU guest
# NOTE: Kernel must be configured with BOOTPARAM_HUNG_TASK_PANIC=y
# Hung task will trigger panic after 120 seconds, capturing vmcore

echo "=== Mutex ABBA Deadlock Test ==="
echo "Loading crash_deadlock module..."
echo "Two threads will deadlock, hung task detector will find after 120s and trigger panic"
echo "This script will wait indefinitely - kernel panic will terminate QEMU"

# Enable hung_task panic (should be already enabled via kernel config)
echo 1 > /proc/sys/kernel/hung_task_panic
echo "hung_task_panic enabled: $(cat /proc/sys/kernel/hung_task_panic)"
echo "hung_task_timeout: $(cat /proc/sys/kernel/hung_task_timeout_secs) seconds"

# Load the deadlock module
insmod /modules/crash_deadlock.ko

echo "Module loaded, threads deadlocked"
echo "Waiting for hung task detection (120s) and kernel panic..."
echo "DO NOT EXIT - let kernel panic capture vmcore"

# Wait indefinitely - kernel panic will terminate QEMU and capture vmcore
# The script will never reach this point if panic occurs
# If no panic after 180s, manual shutdown (for debugging)
sleep 180

echo "WARNING: No panic after 180s - hung_task may not be enabled"
echo "Manual poweroff"
poweroff -f

# Threads are deadlocked, hung_task will panic after 120s
# No need to sleep - kernel will panic and kdump captures vmcore