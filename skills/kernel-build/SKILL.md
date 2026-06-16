---
name: kernel-build
description: Build Linux kernel with custom CONFIG options. Use this skill whenever the user wants to compile a kernel, build kernel modules, enable kernel config options, or test kernel features. Supports ARM64/ARM32/x86_64 architectures with auto defconfig detection (prefers openeuler_defconfig), native and cross-compilation with auto toolchain detection.
---

# Kernel Build Skill (v2.0)

Compile the Linux kernel with custom CONFIG options for different architectures.
Supports both native compilation and cross-compilation with automatic toolchain detection.

## When to Use

Trigger this skill when user asks to:
- Build/compile the kernel with specific config options
- Enable kernel features and compile
- Test kernel compilation with custom configs
- Build kernel modules with specific drivers enabled
- Cross-compile for ARM64/ARM32 from x86_64 host

## ⚠️ CRITICAL: Version Matching Requirement

**Kernel and modules MUST be compiled in the SAME build session.**

Separate compilations will result in version mismatch:
```
# ❌ WRONG - Separate builds cause mismatch
Session 1: make Image          → Kernel X.Y.Z-abc123
Session 2: make modules        → Module X.Y.Z+ (different vermagic)

# ✅ CORRECT - Same build session
make Image && make modules     → Both X.Y.Z+ (same vermagic)
```

This skill follows the correct workflow:
1. Step 1-3: Configure kernel
2. Step 4-5: Build kernel image
3. Step 6: Build modules (immediately after kernel)

**Never skip Step 6 or build modules separately!**

## Quick Usage

```
/kernel-build <config-options> [--arch <arch>] [--jobs <N>] [--cross] [--defconfig <name>]
```

Examples:
- `/kernel-build CONFIG_UB=y` - Build with UB enabled (ARM64 default, native)
- `/kernel-build UB XCU_SCHEDULER --arch x86_64` - Build with UB and XCU for x86 (native)
- `/kernel-build ARM64_MPAM --arch arm64 --cross` - Cross-compile for ARM64 from x86_64 host
- `/kernel-build JFFS2_FS --arch arm32 --defconfig bcm2835_defconfig` - ARM32 with custom defconfig
- `/kernel-build UB --arch arm64 --jobs 64 --cross` - Cross-compile with 64 threads
- `/kernel-build FW_CFG_SYSFS DEBUG_INFO_DWARF4 PANIC_ON_OOPS --arch x86_64 --vmcore` - Build for QEMU vmcore analysis

## Parameters

### Config Options (Required)
- **Format**: `CONFIG_XXX=y` or `XXX` (simplified, auto-converts to CONFIG_XXX)
- **Multiple**: Space-separated list
- **Examples**: `CONFIG_UB=y`, `UB`, `XCU_SCHEDULER`, `CONFIG_ARM64_MPAM=y`

### Architecture (`--arch`)
- **arm64** (default) - ARM64 architecture
- **arm32** - ARM32 architecture ⚠️ Requires custom `--defconfig` (no openeuler_defconfig)
- **x86_64** - x86_64 architecture ⚠️ Uses ARCH=x86 internally (not x86_64)

### Cross-Compilation (`--cross`)
- **Purpose**: Enable cross-compilation when host ≠ target architecture
- **Auto-detection**: If `--arch` differs from host architecture, cross-compilation is recommended
- **Toolchain**: Auto-detected based on architecture:
  - ARM64: `aarch64-linux-gnu-`
  - ARM32: `arm-linux-gnueabi-` or `arm-linux-gnueabihf-`

### Jobs (`--jobs`)
- **Default**: `$(nproc)` (all CPU cores)
- **Custom**: Any number, typically matches CPU cores

### Defconfig Auto-Detection

The skill automatically detects the best defconfig based on availability:

**Priority order**:
1. **User specified**: `--defconfig <name>` parameter (highest priority)
2. **openeuler_defconfig**: If `arch/<arch>/configs/openeuler_defconfig` exists, use it
3. **defconfig**: Fallback to `arch/<arch>/configs/defconfig` (upstream kernel default)

**Defconfig Detection Logic**:
```bash
detect_defconfig() {
    local arch="$1"
    local user_defconfig="$2"
    local defconfig_path="arch/${arch}/configs"

    # User specified - use directly
    if [ -n "$user_defconfig" ]; then
        echo "$user_defconfig"
        return 0
    fi

    # Check openeuler_defconfig first (for openEuler kernel)
    if [ -f "${defconfig_path}/openeuler_defconfig" ]; then
        echo "openeuler_defconfig"
        return 0
    fi

    # Fallback to upstream defconfig
    if [ -f "${defconfig_path}/defconfig" ]; then
        echo "defconfig"
        return 0
    fi

    # No default found - list available options
    echo "ERROR: No default defconfig found for $arch"
    echo "Available defconfigs:"
    ls "${defconfig_path}"/*.defconfig 2>/dev/null | head -10
    return 1
}
```

**ARM32 Note**: openeuler_defconfig typically doesn't exist for ARM32. The skill will fallback to `defconfig` or require user to specify `--defconfig`.

**Common ARM32 defconfigs**:
- `bcm2835_defconfig` - Raspberry Pi
- `multi_v7_defconfig` - Multi-platform ARMv7
- `omap2plus_defconfig` - TI OMAP platforms
- `sunxi_defconfig` - Allwinner sunxi

## Architecture Support Matrix

| Architecture | ARCH Var | Image Target | Output Path | Defconfig Priority | Cross Toolchain |
|--------------|----------|--------------|-------------|-------------------|-----------------|
| **ARM64** | `arm64` | Image | `arch/arm64/boot/Image` | openeuler → defconfig | `aarch64-linux-gnu-` |
| **ARM32** | `arm` | zImage | `arch/arm/boot/zImage` | defconfig (user specify) | `arm-linux-gnueabi-` |
| **x86_64** | `x86` | bzImage | `arch/x86/boot/bzImage` | openeuler → defconfig | Native (gcc) |

## Cross-Compilation Detection

### Step 0: Environment Detection
```bash
# Detect host architecture
HOST_ARCH=$(uname -m)
TARGET_ARCH="<user-specified or default>"

# Map architecture names
case "$TARGET_ARCH" in
    arm64|aarch64) ARCH="arm64"; CROSS_PREFIX="aarch64-linux-gnu-" ;;
    arm32|arm)     ARCH="arm";   CROSS_PREFIX="arm-linux-gnueabi-" ;;
    x86_64|x86)    ARCH="x86";   CROSS_PREFIX="" ;;  # Native
    *) echo "ERROR: Unknown architecture"; exit 1 ;;
esac

# Determine if cross-compilation needed
if [ "$HOST_ARCH" != "$TARGET_ARCH" ] && [ "$TARGET_ARCH" != "x86_64" ]; then
    CROSS_COMPILE_REQUIRED=true
    echo "Cross-compilation required: $HOST_ARCH → $TARGET_ARCH"
fi

# Verify toolchain availability
if [ "$CROSS_COMPILE_REQUIRED" = true ]; then
    if ! command -v "${CROSS_PREFIX}gcc" &> /dev/null; then
        echo "ERROR: Cross toolchain not found: ${CROSS_PREFIX}gcc"
        echo "Install with: sudo apt install gcc-${ARCH}-linux-gnu"
        exit 1
    fi
    echo "✓ Cross toolchain found: ${CROSS_PREFIX}gcc"
fi
```

### Toolchain Installation Guide
```bash
# Ubuntu/Debian
sudo apt install gcc-aarch64-linux-gnu      # ARM64
sudo apt install gcc-arm-linux-gnueabi      # ARM32 (soft-float)
sudo apt install gcc-arm-linux-gnueabihf    # ARM32 (hard-float)

# Fedora/RHEL
sudo dnf install gcc-aarch64-linux-gnu      # ARM64
sudo dnf install gcc-arm-linux-gnu          # ARM32
```

## Build Workflow (Enhanced)

### Step 0: Environment Check
```bash
# 1. Detect host and target architecture
HOST_ARCH=$(uname -m)
# Map user input to ARCH variable

# 2. Check for existing config
if [ -f .config ]; then
    echo "⚠️ Existing .config detected"
    read -p "Clean previous build? [y/N] " -n 1 -r
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        make clean
    fi
fi

# 3. Verify cross toolchain if needed
if [ "$NEEDS_CROSS" = true ]; then
    ${CROSS_COMPILE}gcc --version || exit 1
fi

# 4. Report build configuration
echo "Build Configuration:"
echo "  Host:      $HOST_ARCH"
echo "  Target:    $TARGET_ARCH (ARCH=$ARCH)"
echo "  Cross:     $CROSS_COMPILE"
echo "  Defconfig: $DEFCONFIG"
echo "  Jobs:      $JOBS"
```

### Step 1: Load Base Defconfig (Auto-Detection)
```bash
# Auto-detect best defconfig
DEFCONFIG=$(detect_defconfig "$ARCH" "$USER_DEFCONFIG")

if [ -z "$DEFCONFIG" ]; then
    echo "ERROR: No suitable defconfig found"
    echo "Please specify --defconfig <name>"
    exit 1
fi

echo "✓ Using defconfig: $DEFCONFIG"
if [ "$DEFCONFIG" = "openeuler_defconfig" ]; then
    echo "  (openEuler kernel detected)"
elif [ "$DEFCONFIG" = "defconfig" ]; then
    echo "  (upstream kernel default)"
fi

make ARCH=$ARCH CROSS_COMPILE=$CROSS_COMPILE $DEFCONFIG
```

### Step 2: Enable Config Options
```bash
scripts/config --file .config --enable CONFIG_XXX
# For tristate configs wanting module:
scripts/config --file .config --set-val CONFIG_YYY m
```

### Step 3: Resolve Dependencies
```bash
make ARCH=$ARCH CROSS_COMPILE=$CROSS_COMPILE olddefconfig
```

### Step 4: Build Kernel Image
```bash
START=$(date +%s)
make ARCH=$ARCH CROSS_COMPILE=$CROSS_COMPILE -j$JOBS vmlinux
```

### Step 5: Build Boot Image
```bash
# ARM64
make ARCH=arm64 CROSS_COMPILE=$CROSS_COMPILE Image

# ARM32
make ARCH=arm CROSS_COMPILE=$CROSS_COMPILE zImage

# x86_64 (native)
make ARCH=x86 bzImage
```

### Step 6: Build Modules
```bash
make ARCH=$ARCH CROSS_COMPILE=$CROSS_COMPILE -j$JOBS modules
```

### Step 7: Report Results
```bash
END=$(date +%s)
TOTAL=$((END-START))
echo "Build completed in ${TOTAL}s"

# Show kernel image
ls -lh vmlinux arch/$ARCH/boot/$IMAGE_TARGET

# Show modules for requested configs
for config in $CONFIGS; do
    status=$(grep "^$config=" .config | cut -d'=' -f2)
    if [ "$status" = "m" ]; then
        find . -name "*.ko" -path "*${config_name}*"
    fi
done
```

## CONFIG Validation

### Smart Type Detection
```bash
# Determine if config should be 'y' or 'm' based on Kconfig
config_name="JFFS2_FS"
kconfig_file=$(grep -r "config $config_name" --include=Kconfig -l | head -1)

if grep -A2 "config $config_name" "$kconfig_file" | grep -q "tristate"; then
    DEFAULT_VAL="m"  # Module mode preferred
elif grep -A2 "config $config_name" "$kconfig_file" | grep -q "bool"; then
    DEFAULT_VAL="y"  # Built-in only
fi
```

### Invalid Config Handling
```bash
# Search for similar configs
config_pattern="INVALID"
matches=$(grep -r "config.*$config_pattern" --include=Kconfig | head -5)

if [ -z "$matches" ]; then
    echo "ERROR: CONFIG_$config_pattern not found"
    echo "Suggestions:"
    echo "$matches"
    exit 1
fi
```

### Vmcore Analysis Configs (QEMU Compatible)

For building kernels that work with QEMU `dump-guest-memory` and crash analysis:

```bash
# Required configs for QEMU vmcore support
FW_CFG_SYSFS=y          # QEMU fw_cfg interface (enables vmcoreinfo)
FW_CFG_SYSFS_CMDLINE=y  # fw_cfg command line support
CRASH_CORE=y            # Crash kernel core functionality
DEBUG_INFO_DWARF4=y     # Debug symbols (DWARF4 format)
PANIC_ON_OOPS=y         # Panic on kernel oops

# Example: Build kernel for QEMU vmcore testing
/kernel-build FW_CFG_SYSFS FW_CFG_SYSFS_CMDLINE DEBUG_INFO_DWARF4 PANIC_ON_OOPS CRASH_CORE --arch x86_64
```

**Important**: These configs enable the kernel to communicate with QEMU's vmcoreinfo device, which embeds `NT_VMCOREINFO` ELF notes in memory dumps. This is essential for crash 9.0.2+ to analyze QEMU-generated vmcores.

## Output Report

### Kernel Images
```
Build Target: ARM64 (cross-compiled from x86_64)
Cross Toolchain: aarch64-linux-gnu-gcc 13.2.0

Kernel Images:
  vmlinux: vmlinux (386M)
  Image:   arch/arm64/boot/Image (24M)

Config Status:
  CONFIG_UB [=y] Built-in
    → Compiled into vmlinux
  CONFIG_JFFS2_FS [=m] Module
    → fs/jffs2/jffs2.ko
    → Load: insmod fs/jffs2/jffs2.ko

Build Time: 3m 25s
```

## Example Builds (Enhanced)

### Example 1: Native x86_64 Build (openEuler kernel)
```
/kernel-build UB XCU_SCHEDULER --arch x86_64 --jobs 32
```

Output:
```
✓ Environment check
  Host: x86_64, Target: x86_64 → Native build (no cross)
  Toolchain: gcc (native)

✓ Defconfig auto-detection
  Checking: arch/x86/configs/openeuler_defconfig → Found
  Using: openeuler_defconfig (openEuler kernel)

✓ Step 1: Loaded openeuler_defconfig (ARCH=x86)
✓ Step 2: Enabled CONFIG_UB CONFIG_XCU_SCHEDULER
✓ Step 3: Dependencies resolved
⏳ Step 4: Building vmlinux...
  [32 threads] Elapsed: 2m 15s
✓ Step 4: vmlinux ready (386M)
⏳ Step 5: Building bzImage...
✓ Step 5: bzImage ready (14M)
⏳ Step 6: Building modules...
✓ Step 6: Modules ready

✓ Build completed successfully

Kernel images:
  vmlinux: vmlinux (386M)
  bzImage: arch/x86/boot/bzImage (14M)

Build time: 3m 25s
```

### Example 2: Cross-Compile ARM64 from x86_64 (openEuler kernel)
```
/kernel-build ARM64_MPAM --arch arm64 --cross --jobs 64
```

Output:
```
✓ Environment check
  Host: x86_64, Target: arm64 → Cross-compilation required
  Toolchain: aarch64-linux-gnu-gcc 13.2.0 ✓

✓ Defconfig auto-detection
  Checking: arch/arm64/configs/openeuler_defconfig → Found
  Using: openeuler_defconfig (openEuler kernel)

✓ Step 1: Loaded openeuler_defconfig (ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu-)
✓ Step 2: Enabled CONFIG_ARM64_MPAM
✓ Step 3: Dependencies resolved
⏳ Step 4: Building vmlinux...
  [64 threads] CROSS_COMPILE=aarch64-linux-gnu-
  Elapsed: 3m 10s
✓ Step 4: vmlinux ready (386M)
⏳ Step 5: Building Image...
✓ Step 5: Image ready (24M)
⏳ Step 6: Building modules...
✓ Step 6: Modules ready

✓ Cross-compile completed successfully

Kernel images (for ARM64 target):
  vmlinux: vmlinux (386M)
  Image:   arch/arm64/boot/Image (24M)

Build time: 4m 05s (cross-compilation overhead: +40s)
```

### Example 3: ARM32 with Custom Defconfig
```
/kernel-build JFFS2_FS --arch arm32 --defconfig bcm2835_defconfig --cross
```

Output:
```
✓ Environment check
  Host: x86_64, Target: arm32 → Cross-compilation required
  Toolchain: arm-linux-gnueabi-gcc 13.2.0 ✓

✓ Defconfig auto-detection
  User specified: bcm2835_defconfig (Raspberry Pi)

✓ Step 1: Loaded bcm2835_defconfig (ARCH=arm CROSS_COMPILE=arm-linux-gnueabi-)
✓ Step 2: Enabled CONFIG_JFFS2_FS (tristate → =m)
✓ Step 3: Dependencies resolved
⏳ Step 4: Building vmlinux...
  Elapsed: 2m 30s
✓ Step 4: vmlinux ready (124M)
⏳ Step 5: Building zImage...
✓ Step 5: zImage ready (8M)
⏳ Step 6: Building modules...
✓ Step 6: Modules ready

✓ Build completed successfully

Kernel images (for ARM32/Raspberry Pi):
  vmlinux: vmlinux (124M)
  zImage:  arch/arm/boot/zImage (8M)

Config status:
  CONFIG_JFFS2_FS [=m] Module mode
    → jffs2.ko (fs/jffs2/jffs2.ko)

Build time: 3m 15s
```

### Example 4: Auto Cross-Detection
```
/kernel-build UB --arch arm64 --jobs 32
```

Output (on x86_64 host without --cross specified):
```
⚠️ Cross-compilation recommended
  Host (x86_64) differs from Target (arm64)

Auto-detecting cross toolchain...
  ✓ aarch64-linux-gnu-gcc found
  → Enabling cross-compilation automatically

Proceeding with cross-compile...
  [Build continues as Example 2]
```

### Example 5: Upstream Kernel (no openeuler_defconfig)
```
/kernel-build JFFS2_FS --arch arm64 --jobs 32
```

Output (using upstream kernel without openeuler_defconfig):
```
✓ Environment check
  Host: x86_64, Target: arm64 → Cross-compilation required
  Toolchain: aarch64-linux-gnu-gcc 13.2.0 ✓

✓ Defconfig auto-detection
  Checking: arch/arm64/configs/openeuler_defconfig → Not found
  Checking: arch/arm64/configs/defconfig → Found
  Using: defconfig (upstream kernel default)

✓ Step 1: Loaded defconfig (ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu-)
✓ Step 2: Enabled CONFIG_JFFS2_FS (tristate → =m)
✓ Step 3: Dependencies resolved
  [Build continues normally]
```

### Example 6: Missing Toolchain Error
```
/kernel-build UB --arch arm32 --cross
```

Output (without ARM32 toolchain):
```
✓ Environment check
  Host: x86_64, Target: arm32 → Cross-compilation required

❌ ERROR: Cross toolchain not found
  Required: arm-linux-gnueabi-gcc
  Status: Not installed

Install toolchain:
  Ubuntu/Debian: sudo apt install gcc-arm-linux-gnueabi
  Fedora/RHEL:   sudo dnf install gcc-arm-linux-gnu

Alternative: Use native ARM32 machine or specify correct toolchain prefix.
```

## Error Handling

### Missing Defconfig
```
ERROR: No suitable defconfig found for arm32

Checked:
  - arch/arm/configs/openeuler_defconfig → Not found
  - arch/arm/configs/defconfig → Not found

Available ARM32 defconfigs:
  - bcm2835_defconfig (Raspberry Pi)
  - multi_v7_defconfig (Multi-platform ARMv7)
  - omap2plus_defconfig (TI OMAP platforms)
  - sunxi_defconfig (Allwinner sunxi)

Please specify --defconfig <name>
Example: /kernel-build UB --arch arm32 --defconfig multi_v7_defconfig
```

### Missing Cross Toolchain
```
ERROR: Cross toolchain not available

For ARM64 cross-compile from x86_64:
  sudo apt install gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu

For ARM32 cross-compile from x86_64:
  sudo apt install gcc-arm-linux-gnueabi binutils-arm-linux-gnueabi
```

### Build Failure
```bash
# Show last 50 lines of build log
tail -50 build.log

# Common causes:
# 1. Missing dependency config → run make olddefconfig
# 2. Toolchain issue → verify CROSS_COMPILE
# 3. Architecture mismatch → check ARCH setting
```

## Testing and Verification

### Post-Build Verification
```bash
# 1. Check kernel image exists and architecture
file vmlinux
file arch/$ARCH/boot/$IMAGE

# Expected output for ARM64 cross-compile:
# vmlinux: ELF 64-bit LSB executable, ARM aarch64, version 1 (SYSV)...

# 2. Verify modules
find . -name "*.ko" | wc -l

# 3. Check config was enabled
grep "^CONFIG_XXX=" .config

# 4. Test module loading (on target hardware)
# insmod /path/to/module.ko
```

### Architecture Verification
```bash
# Verify cross-compiled image matches target architecture
file arch/arm64/boot/Image
# Expected: Linux kernel ARM64 boot executable Image

file arch/arm/boot/zImage
# Expected: Linux kernel ARM boot executable zImage

file arch/x86/boot/bzImage
# Expected: Linux kernel x86 boot executable bzImage
```

## Performance Optimization

### Native vs Cross-Compile Performance
| Build Type | Typical Time (32 jobs) | Overhead |
|------------|------------------------|----------|
| x86_64 native | 3-4 min | None |
| ARM64 cross | 3.5-4.5 min | +10-15% |
| ARM32 cross | 2.5-3.5 min | +10-15% |

### Job Count Recommendation
```bash
# Native build: Use all cores
JOBS=$(nproc)

# Cross-compile: Slightly reduce due to overhead
JOBS=$(($(nproc) - 2))
```

## Implementation Notes

### Key Variables
```bash
HOST_ARCH=$(uname -m)           # x86_64, aarch64, etc.
ARCH=x86|arm|arm64              # Kernel ARCH variable
CROSS_COMPILE=prefix-           # Empty for native, prefix for cross
DEFCONFIG=openeuler_defconfig   # Or custom
JOBS=$(nproc)                   # Thread count
IMAGE_TARGET=bzImage|zImage|Image
```

### Architecture Mapping Logic
```python
def setup_build_env(target_arch, cross_compile=False):
    host_arch = get_host_arch()  # x86_64, aarch64

    # Map to kernel ARCH
    arch_map = {
        'x86_64': 'x86',
        'arm64': 'arm64',
        'arm32': 'arm',
        'arm': 'arm'
    }
    ARCH = arch_map[target_arch]

    # Determine cross-compile
    if host_arch != target_arch and target_arch != 'x86_64':
        cross_map = {
            'arm64': 'aarch64-linux-gnu-',
            'arm32': 'arm-linux-gnueabi-',
            'arm': 'arm-linux-gnueabi-'
        }
        CROSS_COMPILE = cross_map[target_arch]
    else:
        CROSS_COMPILE = ''

    # Check defconfig
    if ARCH == 'arm' and DEFCONFIG == 'openeuler_defconfig':
        DEFCONFIG = 'multi_v7_defconfig'  # Fallback

    return ARCH, CROSS_COMPILE, DEFCONFIG
```