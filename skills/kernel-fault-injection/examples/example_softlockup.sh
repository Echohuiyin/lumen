#!/bin/bash
# Example: Generate vmcore from soft lockup (ARM64)
set -e

cd /home/liumingrui/code/Analysis-SKILL

# Build kernel with required configs
/kernel-build FW_CFG_SYSFS FW_CFG_SYSFS_CMDLINE DEBUG_INFO_DWARF4 \
              PANIC_ON_OOPS CRASH_CORE KEXEC PROC_VMCORE \
              DETECT_SOFTLOCKUP BOOTPARAM_SOFTLOCKUP_PANIC \
              --arch arm64 --cross --jobs 32

# Run fault injection
bash skills/kernel-fault-injection/scripts/run_fault_injection.sh softlockup --arch arm64 --timeout 60

# Analyze vmcore
/vmcore-analyzer test_outputs/softlockup_arm64/Image test_outputs/softlockup_arm64/vmcore.elf