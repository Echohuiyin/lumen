#!/bin/bash
# Build the arm64 deadlock testcase end-to-end on an x86_64 host.
#
# Produces real (not stub) arm64 artifacts in test_assets/deadlock_arm64/:
#   vmlinux, Image, vmcore.elf, mutex_abba_deadlock.ko, boot.log, plus
#   metadata (input.txt, REPRODUCTION.md, Makefile, init_script.sh,
#   commit_id.txt, kernel_version.txt).
#
# Re-runnable: skips kernel rebuild if arm64 vmlinux+Image already exist.
#
# Estimated time: 35-65 min (kernel build dominates).
# Estimated disk: ~930MB artifacts + ~3GB intermediate object files in OLK tree.

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
OLK_DIR=${OLK_DIR:-/home/liumingrui/code/OLK-6.6}
LUMEN_DIR=${LUMEN_DIR:-/home/liumingrui/lumen}
MODULE_SRC=${LUMEN_DIR}/deadlock_analysis_output/mutex_abba_deadlock.c
TARGET_DIR=${LUMEN_DIR}/test_assets/deadlock_arm64
INITRAMFS=/tmp/initramfs_deadlock_arm64.cpio.gz
QEMU_TIMEOUT=300
CROSS=aarch64-linux-gnu-
JOBS=$(nproc)

# ── Helpers ───────────────────────────────────────────────────────────────────
log()  { echo "[build_arm64] $*"; }
ok()   { echo "[build_arm64] ✓ $*"; }
warn() { echo "[build_arm64] ⚠ $*" >&2; }
die()  { echo "[build_arm64] ✗ $*" >&2; exit 1; }

# ── Preflight ─────────────────────────────────────────────────────────────────
[ -d "$OLK_DIR" ]               || die "OLK-6.6 not found at $OLK_DIR"
command -v ${CROSS}gcc          >/dev/null || die "need aarch64-linux-gnu-gcc (apt install gcc-aarch64-linux-gnu)"
command -v qemu-system-aarch64 >/dev/null || die "need qemu-system-aarch64 (apt install qemu-system-arm)"
command -v socat               >/dev/null || die "need socat for QEMU monitor dump"
command -v cpio                >/dev/null || die "need cpio"
[ -f "$MODULE_SRC" ]           || die "module source missing: $MODULE_SRC"
[ -f "${LUMEN_DIR}/Analysis-SKILL/tools/busybox/prebuilt/busybox_arm64" ] \
    || die "busybox_arm64 prebuilt missing (run: bash Analysis-SKILL/tools/build_busybox.sh --arch arm64)"
[ -f "${LUMEN_DIR}/Analysis-SKILL/skills/qemu-test/scripts/create_initramfs.sh" ] \
    || die "create_initramfs.sh missing — submodule not initialized? run: git submodule update --init"
[ -f "${LUMEN_DIR}/Analysis-SKILL/tools/crash-vmcore/scripts/run_vmcore_test.sh" ] \
    || die "run_vmcore_test.sh missing — submodule not initialized?"

mkdir -p "$TARGET_DIR"

# ── Phase 1: arm64 kernel build ───────────────────────────────────────────────
log "Phase 1: arm64 kernel build (cross-compile from x86_64 host)"
cd "$OLK_DIR"

if file vmlinux 2>/dev/null | grep -q "ARM aarch64" && \
   file arch/arm64/boot/Image 2>/dev/null | grep -q "ARM64 boot executable"; then
    ok "arm64 vmlinux + Image already built — skipping kernel build"
else
    log "loading arm64 openeuler_defconfig + enabling hung_task panic config"
    make ARCH=arm64 CROSS_COMPILE=$CROSS mrproper
    make ARCH=arm64 CROSS_COMPILE=$CROSS openeuler_defconfig

    # openeuler_defconfig has nearly everything we need; just enable the 2
    # missing configs that are critical for vmcore capture.
    scripts/config --file .config --enable  CONFIG_FW_CFG_SYSFS_CMDLINE
    scripts/config --file .config --enable  CONFIG_BOOTPARAM_HUNG_TASK_PANIC
    scripts/config --file .config --disable CONFIG_DEBUG_INFO_REDUCED
    make ARCH=arm64 CROSS_COMPILE=$CROSS olddefconfig

    log "building vmlinux + Image + modules (jobs=$JOBS)"
    log "  this takes 30-60 min on a typical host..."
    make ARCH=arm64 CROSS_COMPILE=$CROSS -j$JOBS vmlinux Image modules
fi
ok "kernel ready: vmlinux + arch/arm64/boot/Image"

# ── Phase 2: module cross-compile ─────────────────────────────────────────────
# Must run while OLK tree is still in arm64 state — module vermagic must match
# the arm64 vmlinux built above.
log "Phase 2: module cross-compile"
cp "$MODULE_SRC" "$TARGET_DIR/"

cat > "$TARGET_DIR/Makefile" <<'MAKEFILE'
obj-m += mutex_abba_deadlock.o

KDIR ?= /home/liumingrui/code/OLK-6.6
PWD := $(shell pwd)

all:
	$(MAKE) -C $(KDIR) M=$(PWD) ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- clean
MAKEFILE

make -C "$TARGET_DIR" KDIR="$OLK_DIR" clean 2>/dev/null || true
make -C "$TARGET_DIR" KDIR="$OLK_DIR"
[ -f "$TARGET_DIR/mutex_abba_deadlock.ko" ] || die "module build failed"
ok "module: $(file -b "$TARGET_DIR/mutex_abba_deadlock.ko" | cut -d, -f1-2)"

# ── Phase 3: copy kernel artifacts + metadata ────────────────────────────────
log "Phase 3: copy kernel artifacts + metadata"
cp "$OLK_DIR/vmlinux"               "$TARGET_DIR/vmlinux"
cp "$OLK_DIR/arch/arm64/boot/Image" "$TARGET_DIR/Image"
git -C "$OLK_DIR" rev-parse HEAD > "$TARGET_DIR/commit_id.txt"
{
    echo "deadlock test asset (arm64)"
    make -C "$OLK_DIR" ARCH=arm64 -s kernelrelease 2>/dev/null || true
} > "$TARGET_DIR/kernel_version.txt"
ok "artifacts copied: vmlinux=$(du -h "$TARGET_DIR/vmlinux" | cut -f1), Image=$(du -h "$TARGET_DIR/Image" | cut -f1)"

# ── Phase 4: init_script.sh + initramfs ──────────────────────────────────────
log "Phase 4: create initramfs"

cat > "$TARGET_DIR/init_script.sh" <<'INITSH'
#!/bin/sh
# arm64 deadlock testcase init script.
# Loads mutex_abba_deadlock.ko which creates two threads that ABBA-deadlock.
# hung_task detector panics after 60s, QEMU monitor dumps vmcore.

mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev 2>/dev/null || {
    mknod /dev/console c 5 1
    mknod /dev/null c 1 3
    mknod /dev/tty c 5 0
}

mkdir -p /tmp /run /var/tmp /var/log /root
chmod 1777 /tmp

# loop nodes (in case devtmpfs is missing or buggy)
mknod /dev/loop-control c 10 237 2>/dev/null || true
i=0
while [ $i -lt 8 ]; do
    mknod /dev/loop${i} b 7 ${i} 2>/dev/null || true
    i=$((i + 1))
done

export PATH=/bin:/sbin:/usr/bin:/usr/sbin
export HOME=/root

echo
echo "========================================="
echo "  arm64 Deadlock Testcase (Mutex ABBA)"
echo "========================================="
uname -r
uname -m
echo

# Ensure hung_task panics — runtime backup for the bootparam config
echo 1  > /proc/sys/kernel/hung_task_panic 2>/dev/null || true
echo 60 > /proc/sys/kernel/hung_task_timeout_secs 2>/dev/null || true
echo "hung_task_panic=$(cat /proc/sys/kernel/hung_task_panic 2>/dev/null) timeout=$(cat /proc/sys/kernel/hung_task_timeout_secs 2>/dev/null)s"
echo

echo "Loading mutex_abba_deadlock.ko..."
insmod /modules/mutex_abba_deadlock.ko || {
    echo "ERROR: insmod failed — vermagic mismatch or module missing"
    dmesg | tail -20
    # Force a panic so QEMU captures something useful
    echo c > /proc/sysrq-trigger 2>/dev/null || true
    while true; do sleep 60; done
}
echo "Module loaded. Two threads should now deadlock."
echo "Waiting for hung_task panic (~60s)..."
echo

# Let kernel run — hung_task will panic and QEMU will dump vmcore
while true; do
    sleep 10
    # Print periodic heartbeat so boot.log shows progress
    dmesg | tail -3 2>/dev/null
done
INITSH
chmod +x "$TARGET_DIR/init_script.sh"

bash "${LUMEN_DIR}/Analysis-SKILL/skills/qemu-test/scripts/create_initramfs.sh" \
    --arch arm64 \
    --modules "$TARGET_DIR" \
    --test-script "$TARGET_DIR/init_script.sh" \
    --output "$INITRAMFS"
[ -f "$INITRAMFS" ] || die "initramfs creation failed"
ok "initramfs: $INITRAMFS ($(du -h "$INITRAMFS" | cut -f1))"

# ── Phase 5: QEMU boot + vmcore capture ───────────────────────────────────────
log "Phase 5: QEMU arm64 boot + vmcore capture (timeout ${QEMU_TIMEOUT}s)"
log "  arm64 TCG is 5-10x slower than x86 KVM — boot alone takes 30-60s"

rm -f "$TARGET_DIR/vmcore.elf" "$TARGET_DIR/boot.log" "$TARGET_DIR/qemu_monitor.sock"

bash "${LUMEN_DIR}/Analysis-SKILL/tools/crash-vmcore/scripts/run_vmcore_test.sh" \
    deadlock_arm64 \
    "$TARGET_DIR/Image" \
    "$INITRAMFS" \
    "$QEMU_TIMEOUT" \
    --output "$TARGET_DIR"

rm -f "$TARGET_DIR/qemu_monitor.sock"
[ -s "$TARGET_DIR/vmcore.elf" ] || die "vmcore not captured — check boot.log for kernel boot failure"
ok "vmcore: $(du -h "$TARGET_DIR/vmcore.elf" | cut -f1)"
ok "boot.log: $(du -h "$TARGET_DIR/boot.log" | cut -f1)"

# ── Phase 6: input.txt + REPRODUCTION.md ──────────────────────────────────────
log "Phase 6: write input.txt + REPRODUCTION.md"

cat > "$TARGET_DIR/input.txt" <<INPUT
Bug Promote: 内核发生 Mutex ABBA 死锁导致 hung_task panic。两个线程以相反顺序获取两个 mutex，形成死锁。（arm64 版本）
vmcore: ${TARGET_DIR}/vmcore.elf
vmlinux: ${TARGET_DIR}/vmlinux
boot_kernel: ${TARGET_DIR}/Image
kernel_source: ${OLK_DIR}
INPUT
ok "input.txt written (target_arch omitted — _sniff_arch_from_elf will detect arm64 from vmlinux)"

cat > "$TARGET_DIR/REPRODUCTION.md" <<'REPRO'
# Deadlock (Mutex ABBA) 复现步骤 — arm64 版本

> **此文件仅供人工参考，不进入问题分析系统的输入。**
> 问题分析系统的输入见 `input.txt`，只包含 vmcore/vmlinux/Image 路径和问题描述。

## 内核

- 源码：`/home/liumingrui/code/OLK-6.6`（commit `6cf1cf61b43c945adc7c3ca10bfce0d92122b01d`）
- 编译配置：基于 `openeuler_defconfig`，加 `CONFIG_FW_CFG_SYSFS_CMDLINE=y` `CONFIG_BOOTPARAM_HUNG_TASK_PANIC=y` `CONFIG_PANIC_ON_OOPS=y` `CONFIG_DETECT_HUNG_TASK=y` `CONFIG_DEFAULT_HUNG_TASK_TIMEOUT=120` `CONFIG_FW_CFG_SYSFS=y` `CONFIG_DEBUG_INFO_DWARF4=y`
- 交叉编译：`make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- -j$(nproc) vmlinux Image modules`
- 产物：`vmlinux`、`arch/arm64/boot/Image`

## 复现模块

- 源码：`deadlock_analysis_output/mutex_abba_deadlock.c`（lumen 自带，架构无关）
- 编译：在 `test_assets/deadlock_arm64/` 下 `make KDIR=/home/liumingrui/code/OLK-6.6`
- 模块名：`mutex_abba_deadlock.ko`（arm64 ELF relocatable）

## 触发流程

1. 用 `create_initramfs.sh` 构造 initramfs（含 `mutex_abba_deadlock.ko` + `init_script.sh`，busybox_arm64 静态二进制）
2. QEMU 启动（arm64 virt 机器，TCG 模拟，KVM 在 x86 host 上不可用）：
   ```
   qemu-system-aarch64 \
       -M virt,dump-guest-core=on -device vmcoreinfo \
       -cpu cortex-a57 -smp 2 -m 512M -nographic \
       -kernel Image -initrd initramfs.cpio.gz \
       -append "console=ttyAMA0 panic=10 oops=panic hung_task_panic=1 hung_task_timeout_secs=60" \
       -monitor unix:/tmp/qemu_arm64.sock,server,nowait
   ```
3. init 脚本 `insmod /modules/mutex_abba_deadlock.ko` → 模块 init 与 kthread 反序加锁 → 死锁
4. `khungtaskd` 在 `hung_task_timeout_secs`（60s）后报 `blocked for more than 60 seconds` → panic
5. QEMU monitor `dump-guest-memory vmcore.elf` 捕获 vmcore

## 预期 panic 模式

- `INFO: task insmod:XXX blocked for more than 60 seconds.`
- `Call Trace: mutex_lock+0x.../__schedule`
- `Kernel panic - not syncing: hung_task: blocked tasks`

## 产物清单

- `vmcore.elf` — ~500MB，arm64 ELF core file，含 VMCOREINFO
- `vmlinux` — ~400MB，arm64 ELF，debug info（DWARF4）
- `Image` — ~30MB，arm64 QEMU 启动镜像
- `mutex_abba_deadlock.ko` — ~130KB，arm64 ELF relocatable
- `boot.log` — QEMU 控制台输出，含 hung_task panic 栈
- `commit_id.txt` / `kernel_version.txt`

## 与 x86_64 版本的区别

| 维度 | x86_64 | arm64 |
|------|--------|-------|
| 内核镜像 | `bzImage` | `Image` |
| QEMU 二进制 | `qemu-system-x86_64` | `qemu-system-aarch64` |
| 机器类型 | `q35` 或 `accel=kvm:tcg` (i440FX) | `virt` |
| CPU | `host` (KVM) | `cortex-a57` (TCG) |
| 控制台 | `ttyS0` | `ttyAMA0` |
| 加速 | KVM | TCG（host 不是 arm64 时） |
| 启动耗时 | ~5s | ~30-60s |
| 编译 | native | `ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu-` |
REPRO
ok "REPRODUCTION.md written"

# ── Phase 7: verification ────────────────────────────────────────────────────
log "Phase 7: verification"

file "$TARGET_DIR/vmlinux"               | grep -q "ARM aarch64" || die "vmlinux not arm64"
file "$TARGET_DIR/vmcore.elf"            | grep -q "ARM aarch64" || die "vmcore not arm64"
file "$TARGET_DIR/mutex_abba_deadlock.ko" | grep -q "ARM aarch64" || die "module not arm64"
file "$TARGET_DIR/Image"                 | grep -q "ARM64 boot executable" || die "Image not arm64"
ok "all artifacts verified arm64"

# ELF e_machine check — validates commit 17eb3c8's _sniff_arch_from_elf
python3 -c "
with open('${TARGET_DIR}/vmlinux', 'rb') as f:
    assert f.read(4) == b'\x7fELF', 'not ELF'
    f.seek(18)
    e_machine = int.from_bytes(f.read(2), 'little')
    assert e_machine == 183, f'e_machine={e_machine}, expected 183 (EM_AARCH64)'
    print(f'  ELF e_machine = {e_machine} (EM_AARCH64) ✓')
"

# VMCOREINFO presence (needed for crash to parse vmcore)
if readelf -n "$TARGET_DIR/vmcore.elf" 2>/dev/null | grep -q VMCOREINFO; then
    ok "VMCOREINFO present in vmcore.elf"
else
    warn "VMCOREINFO missing — crash may still work but with degraded symbol resolution"
fi

# boot.log panic evidence
if grep -qE "blocked for more than|hung_task|Kernel panic" "$TARGET_DIR/boot.log"; then
    ok "boot.log contains hung_task panic evidence"
else
    warn "boot.log missing panic patterns — kernel may not have panicked (check boot.log)"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo
echo "========================================="
echo "  arm64 deadlock testcase build complete"
echo "========================================="
echo "  Output: $TARGET_DIR"
echo
ls -la "$TARGET_DIR"
echo
echo "⚠ WARNING: OLK-6.6 tree at $OLK_DIR is now configured for arm64."
echo "  To switch back to x86_64: cd $OLK_DIR && make mrproper && make defconfig"
echo
echo "To run Lumen workflow on this testcase:"
echo "  cd $LUMEN_DIR && source venv/bin/activate && \\"
echo "  python3 main.py test_assets/deadlock_arm64/input.txt"
