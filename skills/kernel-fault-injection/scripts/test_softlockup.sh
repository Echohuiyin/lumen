#!/bin/sh
# test_softlockup.sh - Initramfs test script for soft lockup
# This runs inside QEMU guest

echo "=== Soft Lockup Test ==="
echo "Loading crash_softlockup module..."
echo "This will disable interrupts and enter infinite loop"
echo "Watchdog will detect CPU stuck after ~22 seconds"

insmod /modules/crash_softlockup.ko

echo "Module loaded, CPU stuck, waiting for watchdog panic..."

# CPU is stuck, script cannot continue