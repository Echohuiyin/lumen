---
name: qemu-test
description: Boot kernel in QEMU for verification and testing. Use this skill when user mentions 'qemu test', 'qemu boot', '启动 qemu', '用 qemu 验证', '虚拟机测试内核', or wants to test/boot/debug kernel in a virtual machine. Supports ARM64/ARM32/x86_64 architectures with automatic minimal rootfs creation. For kernel compilation, use kernel-build skill separately.
---

# QEMU Kernel Testing Skill

Boot and test the Linux kernel in QEMU virtual machine. **Focus on QEMU execution, not kernel build.**

## When to Use

Trigger this skill when user asks to:
- Boot kernel in QEMU
- Run automated kernel tests in QEMU
- Debug kernel issues with QEMU
- Verify kernel functionality in virtual machine

**NOT for**: Kernel compilation - use `/kernel-build` skill for that.

## Quick Usage

```
/qemu-test [options]

Options:
  --arch <arch>          Architecture: arm64, arm32, x86_64 (default: arm64)
  --kernel <path>        Kernel image path (required if no existing image)
  --modules <path>       Path to kernel modules directory
  --rootfs <path>        Custom root filesystem
  --interactive          Interactive mode (QEMU console)
  --script <path>        Run test script in QEMU
  --cmd <command>        Execute command after boot
  --timeout <seconds>    Timeout for automated tests (default: 300)
  --log                  Collect boot/kernel logs
  --output <dir>         Output directory for artifacts
  --vmcore               Enable vmcoreinfo device for crash analysis
```

Examples:
- `/qemu-test --arch arm64 --interactive` - Boot ARM64 kernel interactively
- `/qemu-test --script tests/ub_test.sh --timeout 120` - Run test script
- `/qemu-test --cmd "dmesg | grep UB"` - Execute command and collect output
- `/qemu-test --kernel arch/x86/boot/bzImage --arch x86_64` - Boot specific kernel
- `/qemu-test --arch x86_64 --vmcore --script crash_test.sh` - Boot for vmcore capture

## Architecture Support

| Arch | QEMU Command | Kernel Image |
|------|-------------|--------------|
| arm64 | `qemu-system-aarch64 -M virt` | `arch/arm64/boot/Image` |
| arm32 | `qemu-system-arm -M virt` | `arch/arm/boot/zImage` |
| x86_64 | `qemu-system-x86_64` | `arch/x86/boot/bzImage` |

## Workflow

### Step 1: Find or Use Kernel Image

**Check for existing kernel image** in the source tree:
```bash
# ARM64
ls -lh arch/arm64/boot/Image

# ARM32  
ls -lh arch/arm/boot/zImage

# x86_64
ls -lh arch/x86/boot/bzImage
```

If kernel image exists, use it directly. If not:
- **Option A**: User provides kernel path via `--kernel`
- **Option B**: Suggest user to run `/kernel-build` first

**DO NOT attempt kernel compilation in this skill.**

### Step 2: Create Minimal Initramfs

**CRITICAL: Busybox Architecture Matching**

Before creating initramfs, verify busybox architecture matches QEMU target:

```bash
# Detect host architecture
HOST_ARCH=$(uname -m)  # e.g., x86_64

# Determine target architecture
TARGET_ARCH="$ARCH"    # arm64, arm32, x86_64

# Architecture compatibility matrix
if [ "$HOST_ARCH" != "$TARGET_ARCH" ] && [ "$TARGET_ARCH" != "x86_64" ]; then
    # Cross-architecture testing requires cross-compiled busybox
    BUSYBOX_REQUIRED="cross-compiled"
fi
```

**Busybox Architecture Detection**:
```bash
# Check busybox architecture
BUSYBOX_PATH="/usr/bin/busybox"  # or custom path
BUSYBOX_ARCH=$(file "$BUSYBOX_PATH" | grep -oE "x86-64|ARM aarch64|ARM,")

# Verify match
if [ "$TARGET_ARCH" = "arm64" ] && [ "$BUSYBOX_ARCH" != "ARM aarch64" ]; then
    echo "ERROR: x86-64 busybox cannot run in ARM64 QEMU"
    echo "Solution: Cross-compile ARM64 busybox (see Busybox section below)"
    exit 1
fi
```

**Required Busybox Applets Checklist**:

Create initramfs with these minimum applets enabled:

| Category | Required Applets | Purpose |
|----------|-----------------|---------|
| **Shell** | `sh`, `ash` | Script execution |
| **Basic** | `cat`, `ls`, `echo`, `mkdir`, `sleep` | File operations |
| **Mount** | `mount`, `umount` | Filesystem mounting |
| **System** | `poweroff`, `reboot`, `dmesg` | System control |
| **Modules** | `insmod`, `lsmod`, `rmmod` | Module loading |
| **Info** | `uname`, `grep` | System info |
| **Device** | `mknod` | Device node creation |
| **Test** | `test`, `[` (same command) | Condition checks |
| **Logs** | `tail`, `head` | Log viewing |
| **Time** | `date` | Timestamps |

**Common Busybox Issues & Solutions**:

| Issue | Cause | Solution |
|-------|-------|----------|
| `Failed to execute /init (error -8)` | Architecture mismatch | Cross-compile busybox for target arch |
| `command: not found` | Applet not enabled | Add to busybox config |
| `tail: invalid option` | Minimal tail config | Enable `CONFIG_FEATURE_TAIL_USE_F` |
| `[: not found` | test/[ applet missing | Enable `CONFIG_TEST=y` |

Read and execute `scripts/create_initramfs.sh`:

Creates a minimal bootable initramfs with:
- Busybox (static linked, **architecture-matched**)
- Basic init script
- Mounts: /proc, /sys, /dev
- Test script embedding (if --script provided)

**Busybox Cross-Compilation** (when host ≠ target):

```bash
# ARM64 Busybox (from x86_64 host)
cd /tmp
wget https://busybox.net/downloads/busybox-1.36.1.tar.bz2
tar xf busybox-1.36.1.tar.bz2 && cd busybox-1.36.1

make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- allnoconfig

# Enable required applets (see checklist above)
scripts/config --enable CONFIG_STATIC --enable CONFIG_ASH --enable CONFIG_SH
scripts/config --enable CONFIG_CAT CONFIG_LS CONFIG_MOUNT CONFIG_INSMOD
scripts/config --enable CONFIG_TEST CONFIG_TAIL CONFIG_DATE CONFIG_DMESG
scripts/config --enable CONFIG_POWEROFF CONFIG_REBOOT CONFIG_UNAME

yes "" | make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- oldconfig
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- -j$(nproc)

# Result: busybox (ARM64 static, ~1.1M)
file busybox  # Verify: ELF 64-bit LSB executable, ARM aarch64, statically linked
```

### Step 3: Prepare QEMU Command

Based on architecture, construct QEMU command:

**ARM64**:
```bash
qemu-system-aarch64 \
    -M virt \
    -cpu cortex-a57 \
    -smp 2 \
    -m 512M \
    -nographic \
    -kernel $KERNEL_IMAGE \
    -initrd $INITRAMFS \
    -append "console=ttyAMA0 root=/dev/ram rw"
```

**ARM32**:
```bash
qemu-system-arm \
    -M virt \
    -cpu cortex-a15 \
    -smp 2 \
    -m 512M \
    -nographic \
    -kernel $KERNEL_IMAGE \
    -initrd $INITRAMFS \
    -append "console=ttyAMA0 root=/dev/ram rw"
```

**x86_64**:
```bash
qemu-system-x86_64 \
    -M q35,dump-guest-core=on \  # q35 machine type for proper ELF format
    -smp 2 \
    -m 512M \
    -nographic \
    -kernel $KERNEL_IMAGE \
    -initrd $INITRAMFS \
    -append "console=ttyS0 panic=10 oops=panic"
```

### Vmcore Capture Mode (--vmcore)

For kernels built with `FW_CFG_SYSFS` and crash analysis support:

```bash
# x86_64 with vmcoreinfo device (crash 9.0.2+ compatible)
qemu-system-x86_64 \
    -M q35,dump-guest-core=on \
    -device vmcoreinfo \        # Enables NT_VMCOREINFO ELF note
    -smp 2 \
    -m 512M \
    -nographic \
    -kernel $KERNEL_IMAGE \
    -initrd $INITRAMFS \
    -append "console=ttyS0 panic=10 oops=panic" \
    -monitor unix:/tmp/qemu.sock,server,nowait

# Capture vmcore after crash
echo "dump-guest-memory /tmp/vmcore.elf" | socat - UNIX-CONNECT:/tmp/qemu.sock
```

**Important Requirements**:
1. Kernel must be built with `CONFIG_FW_CFG_SYSFS=y` (use `/kernel-build` with FW_CFG_SYSFS)
2. Use crash 9.0.2+ for analysis (older versions may segfault on QEMU dumps)
3. See `docs/qemu_vmcore_generation.md` for complete guide

### Step 4: Execute QEMU

Run QEMU and capture output:

**Interactive mode**: Run QEMU directly, user interacts with console
**Automated mode**: Use timeout, capture logs, analyze results

### Step 5: Collect Outputs

Save all artifacts to output directory:

```
outputs/
├── kernel_image         # Kernel image used (copied if needed)
├── modules/*.ko         # Modules (if --modules provided)
├── initramfs.cpio.gz    # Initramfs created
├── test_script.sh       # Test script (if --script)
├── boot.log             # QEMU boot output
├── test_result.log      # Test execution results
└── summary.txt          # Test summary
```

## Output Organization

**CRITICAL**: All generated files MUST be saved to the output directory.

### Required Outputs Checklist

Before completing workflow, verify:
- [ ] `kernel_image` - Kernel image exists and copied
- [ ] `initramfs.cpio.gz` - Initramfs created
- [ ] `boot.log` - QEMU output captured
- [ ] `test_result.log` - Test results (if test ran)
- [ ] `summary.txt` - Execution summary

### Implementation Pattern

```bash
OUTPUT_DIR="${OUTPUT_DIR:-qemu_outputs_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$OUTPUT_DIR/modules"
mkdir -p "$OUTPUT_DIR/logs"

# Copy kernel image to outputs
cp "$KERNEL_IMAGE" "$OUTPUT_DIR/kernel_image"

# Copy initramfs
cp "$INITRAMFS" "$OUTPUT_DIR/initramfs.cpio.gz"

# Copy test script if provided
if [ -n "$TEST_SCRIPT" ]; then
    cp "$TEST_SCRIPT" "$OUTPUT_DIR/test_script.sh"
fi

# Copy modules if provided
if [ -n "$MODULES_DIR" ]; then
    cp -r "$MODULES_DIR/*.ko" "$OUTPUT_DIR/modules/"
fi

# Save logs
cp "$QEMU_LOG" "$OUTPUT_DIR/boot.log"

# Generate summary
cat > "$OUTPUT_DIR/summary.txt" << EOF
QEMU Test Summary
=================
Date: $(date)
Architecture: $ARCH
Kernel: $KERNEL_IMAGE ($(ls -lh $KERNEL_IMAGE))
Initramfs: $OUTPUT_DIR/initramfs.cpio.gz
Mode: $MODE
Result: $RESULT
EOF
```

## Test Script Execution

Test scripts are embedded into initramfs and executed during boot.

### Test Script Requirements
- POSIX-compatible (busybox sh)
- Clear output with status messages
- Exit code 0 = success
- Handle timeout gracefully

### Example Test Script
```bash
#!/bin/sh
echo "=== UB Driver Test ==="

# Check kernel config
grep "CONFIG_UB" /proc/config.gz || echo "CONFIG check: no /proc/config.gz"

# Check dmesg
dmesg | grep -i "ub subsystem" && echo "✓ UB subsystem initialized" || echo "✗ No UB messages"

# Check sysfs
ls /sys/bus/ub && echo "✓ UB bus registered" || echo "✗ No UB bus"

echo "Test completed"
exit 0
```

## Integration with kernel-build

This skill **does not build kernels**. Use `/kernel-build` separately:

```bash
# Step 1: Build kernel with kernel-build
/kernel-build JFFS2_FS --arch x86_64

# Step 2: Boot with qemu-test
/qemu-test --arch x86_64 --script tests/jffs2_test.sh
```

**If user requests both build and test**:
1. First invoke `/kernel-build` to compile kernel
2. Then use the built kernel image for QEMU boot
3. Keep the two skills separate and focused

## Common Use Cases

### 1. Quick Boot Verification
```bash
# Use existing kernel
/qemu-test --arch arm64 --interactive
```

### 2. Automated Driver Test
```bash
# Test UB subsystem
/qemu-test --arch arm64 --script tests/ub_test.sh --timeout 60
```

### 3. Log Collection for Debugging
```bash
# Collect boot logs
/kemu-test --arch arm64 --log --timeout 30
```

### 4. Build + Test Workflow (two skills)
```bash
# Build first
/kernel-build UB --arch x86_64

# Then test
/qemu-test --arch x86_64 --script tests/ub_test.sh
```

## Error Handling

### Kernel Not Found
```
ERROR: Kernel image not found at arch/arm64/boot/Image

Solutions:
1. Build kernel: /kernel-build --arch arm64
2. Provide kernel: --kernel /path/to/Image
```

### QEMU Not Installed
```
ERROR: qemu-system-aarch64 not found

Install: sudo apt install qemu-system-arm
```

### Boot Timeout
```
⚠ Timeout reached (120 seconds)

Partial output saved to: outputs/boot.log
Suggest: Increase --timeout or use --interactive to debug
```

## Implementation Scripts

Scripts in `scripts/` directory:
- **create_initramfs.sh**: Generate minimal initramfs
- **boot_arm64.sh**: ARM64 QEMU launch
- **boot_arm32.sh**: ARM32 QEMU launch
- **boot_x86.sh**: x86_64 QEMU launch
- **run_test.sh**: Automated test wrapper

Read and execute these scripts when implementing the workflow.