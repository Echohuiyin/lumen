# Architecture-Specific QEMU Configurations

Detailed configuration for each supported architecture.

## ARM64 (aarch64)

### Machine Configuration
- **Machine Type**: `virt` (QEMU virtual platform)
- **CPU**: `cortex-a57` (default), or `cortex-a72`, `max`
- **Memory**: Default 512MB, configurable
- **Console**: `ttyAMA0` (UART console)

### Kernel Image
- **Location**: `arch/arm64/boot/Image`
- **Format**: Raw ARM64 kernel image
- **Compression**: None (Image is uncompressed)

### QEMU Command
```bash
qemu-system-aarch64 \
    -M virt \
    -cpu cortex-a57 \
    -smp 2 \
    -m 512M \
    -nographic \
    -kernel arch/arm64/boot/Image \
    -initrd initramfs.cpio.gz \
    -append "console=ttyAMA0 root=/dev/ram rw"
```

### Kernel Command Line Parameters
```
console=ttyAMA0   - Console output to UART
root=/dev/ram     - Root filesystem on RAM disk
rw                - Mount root read-write
panic=1           - Panic timeout (for automated tests)
```

### Advanced Options
```bash
# Multiple CPUs
-smp 4                    # 4 CPU cores

# More memory
-m 1024M                  # 1GB RAM

# CPU type alternatives
-cpu cortex-a72           # Cortex-A72
-cpu max                  # Enable all CPU features

# GIC version (Generic Interrupt Controller)
-M virt,gic-version=3     # GICv3

# Network (for network testing)
-netdev user,id=net0 \
-device virtio-net-device,netdev=net0

# Disk (for storage testing)
-drive file=disk.img,format=raw,id=hd0 \
-device virtio-blk-device,drive=hd0
```

### DTB (Device Tree)
- QEMU virt machine generates DTB automatically
- Can provide custom DTB: `-dtb custom.dtb`

## ARM32 (arm)

### Machine Configuration
- **Machine Type**: `virt` (QEMU virtual platform)
- **CPU**: `cortex-a15` (default), or `cortex-a9`
- **Memory**: Default 512MB, configurable
- **Console**: `ttyAMA0` (UART console)

### Kernel Image
- **Location**: `arch/arm/boot/zImage`
- **Format**: Compressed ARM kernel image (zImage)
- **Compression**: gzip compressed

### QEMU Command
```bash
qemu-system-arm \
    -M virt \
    -cpu cortex-a15 \
    -smp 2 \
    -m 512M \
    -nographic \
    -kernel arch/arm/boot/zImage \
    -initrd initramfs.cpio.gz \
    -append "console=ttyAMA0 root=/dev/ram rw"
```

### Kernel Command Line Parameters
Same as ARM64, but console is also `ttyAMA0`.

### Advanced Options
```bash
# Multiple CPUs
-smp 4                    # 4 CPU cores

# CPU alternatives
-cpu cortex-a9            # Cortex-A9

# Memory
-m 1024M                  # 1GB RAM

# Network
-netdev user,id=net0 \
-device virtio-net-device,netdev=net0

# Disk
-drive file=disk.img,format=raw,id=hd0 \
-device virtio-blk-device,drive=hd0
```

## x86_64

### Machine Configuration
- **Machine Type**: Default PC (i440FX + PIIX)
- **CPU**: QEMU Virtual CPU (default), or `host` (if KVM available)
- **Memory**: Default 512MB, configurable
- **Console**: `ttyS0` (Serial console)

### Kernel Image
- **Location**: `arch/x86/boot/bzImage`
- **Format**: Compressed x86 kernel image (bzImage)
- **Compression**: gzip/zstd compressed

### QEMU Command
```bash
qemu-system-x86_64 \
    -smp 2 \
    -m 512M \
    -nographic \
    -kernel arch/x86/boot/bzImage \
    -initrd initramfs.cpio.gz \
    -append "console=ttyS0 root=/dev/ram rw"
```

### Kernel Command Line Parameters
```
console=ttyS0     - Console output to serial port
root=/dev/ram     - Root filesystem on RAM disk
rw                - Mount root read-write
panic=1           - Panic timeout (for automated tests)
earlyprintk=serial - Early kernel messages to serial
```

### Advanced Options
```bash
# KVM acceleration (requires KVM support)
-enable-kvm \
-cpu host                # Use host CPU features

# Multiple CPUs
-smp 4                   # 4 CPU cores

# More memory
-m 2048M                 # 2GB RAM

# Network
-netdev user,id=net0 \
-device virtio-net-pci,netdev=net0

# Disk
-drive file=disk.img,format=raw \
-device virtio-blk-pci,drive=hd0

# Multiple serial ports
-serial stdio            # Serial 0 (console)
-serial mon:stdio        # Multiplexed with monitor
```

## Common Options for All Architectures

### Console Settings
```bash
-nographic               # Disable graphical output, use serial console
-serial stdio            # Connect serial to terminal stdio
```

### Memory Configuration
```bash
-m 512M                  # 512 MB RAM (default)
-m 1024M                 # 1 GB RAM
-m 2048M                 # 2 GB RAM
```

### SMP (Multiple CPUs)
```bash
-smp 1                   # Single CPU
-smp 2                   # 2 CPUs (default)
-smp 4                   # 4 CPUs
-smp 8                   # 8 CPUs
```

### Timeout Control
```bash
# Using timeout command wrapper
timeout 300 qemu-system-...  # 5 minute timeout
timeout 60 qemu-system-...   # 1 minute timeout
```

### Log Capture
```bash
# Capture all output
qemu-system-... 2>&1 | tee boot.log

# Capture with timeout
timeout 300 qemu-system-... 2>&1 | tee boot.log
```

## Performance Tuning

### KVM Acceleration (x86_64 only)
```bash
-enable-kvm              # Use KVM for faster execution
-cpu host                # Use host CPU features
```

Check KVM availability:
```bash
ls -la /dev/kvm          # Check KVM device
kvm-ok                   # Ubuntu tool to check KVM
```

### Memory Optimization
```bash
-m 512M,slots=2,maxmem=2048M  # Memory hotplug support
```

### Network Performance
```bash
-netdev tap,id=net0,ifname=tap0 \
-device virtio-net-pci,netdev=net0  # TAP networking (faster)
```

## Debugging Options

### Kernel Debugging
```bash
-append "console=ttyAMA0 debug loglevel=8"  # Max debug level

-append "initcall_debug"                    # Debug init calls

-append "slub_debug=FZP"                    # SLUB allocator debug
```

### QEMU Monitor
```bash
-monitor stdio            # QEMU monitor on stdio
-monitor telnet:localhost:4444,server,nowait  # Remote monitor
```

### GDB Debugging
```bash
-s -S                     # Wait for GDB connection
-gdb tcp::1234            # GDB server on port 1234

# Connect from GDB:
gdb vmlinux
target remote localhost:1234
```

## Troubleshooting

### No Boot Output
- Check console parameter matches architecture (ttyAMA0 vs ttyS0)
- Ensure `-nographic` is used
- Try `earlyprintk` for x86

### Kernel Panic
- Check initramfs is valid and contains init script
- Verify kernel command line parameters
- Check kernel config has necessary drivers built-in

### Timeout Issues
- Increase timeout value
- Check kernel is booting (look for partial output)
- Use `--interactive` to debug manually

### Performance Issues
- Use KVM for x86_64 if available
- Reduce CPU count if testing single-threaded code
- Use smaller initramfs

## Device Types by Architecture

### ARM64/ARM32
- Network: `virtio-net-device`
- Disk: `virtio-blk-device`
- All use MMIO (no PCI)

### x86_64
- Network: `virtio-net-pci`
- Disk: `virtio-blk-pci`
- Uses PCI bus

This distinction is important when configuring devices in QEMU.