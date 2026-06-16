#!/bin/bash
# Example: Generate vmcore from mutex ABBA deadlock (x86_64)
set -e

cd /home/liumingrui/code/Analysis-SKILL

# Build kernel with required configs
/kernel-build FW_CFG_SYSFS FW_CFG_SYSFS_CMDLINE DEBUG_INFO_DWARF4 \
              PANIC_ON_OOPS CRASH_CORE KEXEC PROC_VMCORE \
              DETECT_HUNG_TASK DEFAULT_HUNG_TASK_TIMEOUT \
              --arch x86_64 --jobs 32

# Run fault injection (requires longer timeout for hung task)
bash skills/kernel-fault-injection/scripts/run_fault_injection.sh deadlock --arch x86_64 --timeout 150

# Analyze vmcore
/vmcore-analyzer test_outputs/deadlock_x86_64/vmlinux test_outputs/deadlock_x86_64/vmcore.elf