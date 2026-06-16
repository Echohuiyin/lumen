# kernel-fault-injection

Inject kernel faults in QEMU to generate vmcore files for crash analysis.

## Usage

```
/kernel-fault-injection <fault_type> [--arch x86_64|arm64] [--kernel <path>] [--output <dir>]
```

### Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `fault_type` | Type of fault to inject | Required |
| `--arch` | Target architecture | `x86_64` |
| `--kernel` | Pre-built kernel image | Auto-build |
| `--output` | Output directory | `./test_outputs/<fault_type>` |
| `--timeout` | QEMU timeout in seconds | `60` |

### Fault Types

| Type | Description | Panic Pattern |
|------|-------------|---------------|
| `nullptr` | NULL pointer dereference | `kernel BUG at` / `NULL pointer dereference` |
| `softlockup` | CPU stuck in infinite loop | `BUG: soft lockup - CPU# stuck` |
| `deadlock` | Mutex ABBA deadlock | `blocked for more than 120 seconds` |
| `panic` | Direct kernel panic | `Kernel panic` |
| `stack-overflow` | Stack overflow via recursion | `stack-overflow` |

## Examples

### Basic Usage

```bash
# Generate vmcore from null pointer dereference
/kernel-fault-injection nullptr

# Generate vmcore from soft lockup (ARM64)
/kernel-fault-injection softlockup --arch arm64

# Use pre-built kernel
/kernel-fault-injection deadlock --kernel /path/to/vmlinux
```

### Output

Each run produces:

```
test_outputs/nullptr/
├── vmcore.elf          # Captured vmcore file
├── boot.log            # QEMU console output
├── crash_nullptr.ko    # Compiled fault module
└── analysis.txt       # Quick analysis summary
```

## Workflow

```
┌─────────────────────────────────────────────────────────────┐
│                    kernel-fault-injection                    │
├─────────────────────────────────────────────────────────────┤
│  1. Prepare fault module                                    │
│     └── modules/crash_<type>.c                              │
│                                                             │
│  2. Build kernel (reuse /kernel-build)                      │
│     └── CONFIG_PANIC_ON_OOPS, CONFIG_FW_CFG_SYSFS, etc.     │
│                                                             │
│  3. Create initramfs with module                            │
│     └── insmod /modules/crash_<type>.ko                     │
│                                                             │
│  4. Run QEMU with vmcoreinfo (reuse /qemu-test)             │
│     └── -device vmcoreinfo -M q35,dump-guest-core=on        │
│                                                             │
│  5. Capture vmcore on crash                                 │
│     └── dump-guest-memory vmcore.elf                        │
│                                                             │
│  6. Validate vmcore                                         │
│     └── readelf -n vmcore.elf | grep VMCOREINFO            │
└─────────────────────────────────────────────────────────────┘
```

## Kernel Requirements

The kernel must be built with these options:

```bash
/kernel-build FW_CFG_SYSFS FW_CFG_SYSFS_CMDLINE DEBUG_INFO_DWARF4 \
              PANIC_ON_OOPS CRASH_CORE KEXEC --arch x86_64
```

Required configs:
- `CONFIG_FW_CFG_SYSFS=y` - Kernel-QEMU communication
- `CONFIG_FW_CFG_SYSFS_CMDLINE=y` - fw_cfg command line
- `CONFIG_DEBUG_INFO_DWARF4=y` - Debug symbols for crash
- `CONFIG_PANIC_ON_OOPS=y` - Panic on kernel oops
- `CONFIG_CRASH_CORE=y` - Crash core support

## Fault Module Details

### nullptr - NULL Pointer Dereference

```c
static int __init crash_init(void) {
    int *ptr = NULL;
    *ptr = 42;  // Trigger panic
    return 0;
}
```

**Config**: `CONFIG_PANIC_ON_OOPS=y`

### softlockup - CPU Soft Lockup

```c
static int __init softlockup_init(void) {
    local_irq_disable();
    while(1);  // Infinite loop, trigger soft lockup
    return 0;
}
```

**Config**: `CONFIG_DETECT_SOFTLOCKUP=y`, `CONFIG_BOOTPARAM_SOFTLOCKUP_PANIC=y`

### deadlock - Mutex ABBA Deadlock

```c
static int __init deadlock_init(void) {
    // Thread 1: lock A -> lock B
    // Thread 2: lock B -> lock A
    // Both blocked forever
}
```

**Config**: `CONFIG_DETECT_HUNG_TASK=y`, `CONFIG_DEFAULT_HUNG_TASK_TIMEOUT=120`

## QEMU Configuration

### x86_64

```bash
qemu-system-x86_64 \
    -M q35,dump-guest-core=on \
    -device vmcoreinfo \
    -smp 2 -m 512M \
    -nographic \
    -kernel vmlinux \
    -initrd initramfs.cpio.gz \
    -append "console=ttyS0 panic=10 oops=panic" \
    -monitor unix:/tmp/qemu.sock,server,nowait
```

### ARM64

```bash
qemu-system-aarch64 \
    -M virt,dump-guest-core=on \
    -device vmcoreinfo \
    -cpu cortex-a57 \
    -smp 2 -m 512M \
    -nographic \
    -kernel Image \
    -initrd initramfs.cpio.gz \
    -append "console=ttyAMA0 panic=10 oops=panic" \
    -monitor unix:/tmp/qemu.sock,server,nowait
```

## Vmcore Capture

When crash is detected, capture vmcore via QEMU monitor:

```bash
# Auto-captured by run_test.sh
echo "dump-guest-memory vmcore.elf" | socat - UNIX-CONNECT:/tmp/qemu.sock
```

## Vmcore Analysis

Use crash utility (9.0.2+ required for QEMU vmcore):

```bash
# Use compiled crash from tools/crash-vmcore
../tools/crash-vmcore/bin/crash vmlinux vmcore.elf

# Or use /vmcore-analyzer skill
/vmcore-analyzer vmlinux vmcore.elf
```

## Integration with Other Skills

### With kernel-build

```bash
# Build kernel with required configs
/kernel-build FW_CFG_SYSFS DEBUG_INFO_DWARF4 PANIC_ON_OOPS --arch x86_64

# Then inject fault
/kernel-fault-injection nullptr
```

### With qemu-test

```bash
# Fault injection uses qemu-test internally
# But can also use pre-configured QEMU:
/qemu-test vmlinux initramfs.cpio.gz --vmcore
```

### With vmcore-analyzer

```bash
# After generating vmcore
/kernel-fault-injection nullptr --output /tmp/test
/vmcore-analyzer /tmp/test/vmlinux /tmp/test/vmcore.elf
```

## Troubleshooting

### VMCOREINFO missing

```bash
readelf -n vmcore.elf | grep VMCOREINFO
# No output
```

**Solution**: Ensure kernel has `CONFIG_FW_CFG_SYSFS=y` and QEMU has `-device vmcoreinfo`

### Crash segfault

**Cause**: Using crash 8.x with QEMU vmcore

**Solution**: Use crash 9.0.2+ from `tools/crash-vmcore/bin/crash`

### Vmcore empty

**Cause**: panic=-1 causes immediate reboot

**Solution**: Use `panic=10` kernel parameter

## Files

```
skills/kernel-fault-injection/
├── SKILL.md                    # This file
├── modules/                    # Fault injection modules
│   ├── crash_nullptr.c
│   ├── crash_softlockup.c
│   ├── crash_deadlock.c
│   ├── crash_panic.c
│   └── crash_stack_overflow.c
├── scripts/
│   ├── run_fault_injection.sh  # Main entry point
│   ├── create_initramfs.sh     # Create initramfs with module
│   └── capture_vmcore.sh       # Vmcore capture logic
└── examples/
    ├── test_nullptr.sh
    ├── test_softlockup.sh
    └── test_deadlock.sh
```