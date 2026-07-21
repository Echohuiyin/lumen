#!/usr/bin/env bash
# Build the source-controlled recipe (not the binary image) for Lumen's
# persistent SSH QEMU guests.  It follows syzkaller's Debian/debootstrap image
# model, but keeps the artifact below runtime/ so it is never versioned.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_ROOT="${PROJECT_ROOT}/runtime/qemu-ssh"
DISTRIBUTION="bookworm"
ARCH="all"
MIRROR="http://deb.debian.org/debian"

usage() {
    cat <<'EOF'
Usage: bash scripts/provision_qemu_ssh_image.sh [--arch x86_64|arm64|all] [--distribution bookworm] [--image-root PATH]

Builds Debian ext4 guest images with sshd and a generated root SSH key.  The
images and private keys are deployment artifacts under runtime/qemu-ssh/ and
are intentionally ignored by Git.  Cross-architecture arm64 creation requires
qemu-user-static and binfmt support on an x86_64 host.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --arch) ARCH="$2"; shift 2 ;;
        --distribution) DISTRIBUTION="$2"; shift 2 ;;
        --image-root) IMAGE_ROOT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "ERROR: required command not found: $1 ($2)" >&2
        exit 1
    fi
}

require_command sudo "install sudo/debootstrap prerequisites first"
require_command debootstrap "sudo apt install debootstrap"
require_command mke2fs "sudo apt install e2fsprogs"
require_command ssh-keygen "sudo apt install openssh-client"

host_arch="$(uname -m)"
if [[ "$host_arch" == "amd64" ]]; then host_arch="x86_64"; fi
if [[ "$host_arch" == "aarch64" ]]; then host_arch="arm64"; fi

build_one() {
    local lumen_arch="$1" deb_arch rootfs image key qemu_static
    case "$lumen_arch" in
        x86_64) deb_arch="amd64" ;;
        arm64) deb_arch="arm64" ;;
        *) echo "ERROR: unsupported architecture: $lumen_arch" >&2; exit 2 ;;
    esac
    rootfs="${IMAGE_ROOT}/${lumen_arch}/rootfs"
    image="${IMAGE_ROOT}/${lumen_arch}/debian.img"
    key="${IMAGE_ROOT}/${lumen_arch}/lumen_qemu_ed25519"

    if [[ -s "$image" && -s "$key" ]]; then
        echo "[OK] persistent SSH image exists: $lumen_arch"
        return
    fi
    if [[ -e "$rootfs" ]]; then
        echo "ERROR: incomplete rootfs exists: $rootfs; remove it explicitly before rebuilding" >&2
        exit 1
    fi
    if [[ "$lumen_arch" != "$host_arch" ]]; then
        qemu_static="/usr/bin/qemu-aarch64-static"
        if [[ ! -x "$qemu_static" ]]; then
            echo "ERROR: arm64 guest bootstrap needs qemu-user-static: sudo apt install qemu-user-static binfmt-support" >&2
            exit 1
        fi
        sudo debootstrap --foreign --arch="$deb_arch" "$DISTRIBUTION" "$rootfs" "$MIRROR"
        sudo cp "$qemu_static" "$rootfs/usr/bin/"
        sudo chroot "$rootfs" /debootstrap/debootstrap --second-stage
    else
        sudo debootstrap --arch="$deb_arch" "$DISTRIBUTION" "$rootfs" "$MIRROR"
    fi

    if [[ ! -f "$key" ]]; then
        mkdir -p "$(dirname "$key")"
        ssh-keygen -q -t ed25519 -N '' -f "$key"
        chmod 600 "$key"
    fi
    sudo install -d -m 0700 "$rootfs/root/.ssh"
    sudo install -m 0600 "${key}.pub" "$rootfs/root/.ssh/authorized_keys"
    sudo install -d -m 0755 "$rootfs/etc/ssh/sshd_config.d" "$rootfs/etc/network/interfaces.d"
    sudo tee "$rootfs/etc/apt/sources.list" >/dev/null <<EOF
deb $MIRROR $DISTRIBUTION main
EOF
    sudo tee "$rootfs/etc/ssh/sshd_config.d/lumen.conf" >/dev/null <<'EOF'
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
EOF
    sudo tee "$rootfs/etc/network/interfaces.d/eth0" >/dev/null <<'EOF'
auto eth0
iface eth0 inet dhcp
EOF
    sudo chroot "$rootfs" /usr/bin/env DEBIAN_FRONTEND=noninteractive apt-get update
    guest_packages=(
        openssh-server kmod iproute2 ca-certificates coreutils
        curl tar time strace psmisc iputils-ping dnsutils net-tools
        gcc libc6-dev make
    )
    if [[ "$lumen_arch" == "arm64" ]]; then
        # arm64 TCG on an x86 host may have too little entropy for sshd to
        # create host keys promptly; this is the same issue called out by
        # syzkaller's arm64 QEMU setup guide.
        guest_packages+=(haveged)
    fi
    sudo chroot "$rootfs" /usr/bin/env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        "${guest_packages[@]}"
    sudo chroot "$rootfs" systemctl enable ssh
    if [[ "$lumen_arch" == "arm64" ]]; then
        sudo chroot "$rootfs" systemctl enable serial-getty@ttyAMA0.service
    fi
    sudo rm -f "$rootfs/usr/bin/qemu-aarch64-static"

    # A raw ext4 image is accepted by both x86 IDE and arm virtio block QEMU.
    truncate -s 2G "$image"
    sudo mke2fs -q -t ext4 -d "$rootfs" "$image"
    sudo chown "$(id -u):$(id -g)" "$image"
    sudo rm -rf "$rootfs"
    echo "[OK] built persistent SSH image: $image"
}

case "$ARCH" in
    all) build_one x86_64; build_one arm64 ;;
    x86_64|arm64) build_one "$ARCH" ;;
    *) echo "ERROR: --arch must be x86_64, arm64, or all" >&2; exit 2 ;;
esac
