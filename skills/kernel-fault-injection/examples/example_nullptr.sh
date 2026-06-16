#!/bin/bash
# Example: Generate vmcore from NULL pointer dereference (x86_64)
set -e

cd /home/liumingrui/code/Analysis-SKILL

# Build kernel with required configs
/kernel-build FW_CFG_SYSFS FW_CFG_SYSFS_CMDLINE DEBUG_INFO_DWARF4 \
              PANIC_ON_OOPS CRASH_CORE KEXEC PROC_VMCORE \
              --arch x86_64 --jobs 32

# Run fault injection
bash skills/kernel-fault-injection/scripts/run_fault_injection.sh nullptr --arch x86_64 --timeout 60

# Analyze vmcore
/vmcore-analyzer test_outputs/nullptr_x86_64/vmlinux test_outputs/nullptr_x86_64/vmcore.elf