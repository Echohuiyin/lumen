#!/usr/bin/env bash
# Build the source-controlled recipe (not the binary image) for Lumen's
# persistent SSH QEMU guests.  It follows syzkaller's Debian/debootstrap image
# model, but keeps the artifact below runtime/ so it is never versioned.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_ROOT="${LUMEN_QEMU_IMAGE_ROOT:-${PROJECT_ROOT}/runtime/qemu-ssh}"
DISTRIBUTION="${LUMEN_DEBIAN_DISTRIBUTION:-bookworm}"
ARCH="${LUMEN_QEMU_ARCH:-all}"
MIRROR="${LUMEN_DEBIAN_MIRROR:-}"
REBUILD="${LUMEN_QEMU_REBUILD:-0}"
if [[ "$REBUILD" == "1" || "$REBUILD" == "true" ]]; then
    REBUILD=true
else
    REBUILD=false
fi

usage() {
    cat <<'EOF'
Usage: bash scripts/provision_qemu_ssh_image.sh [--arch x86_64|arm64|all] [--distribution NAME] [--mirror URL] [--image-root PATH] [--rebuild]

Builds Debian ext4 guest images with sshd and a generated root SSH key.  The
images and private keys are deployment artifacts under runtime/qemu-ssh/ and
are intentionally ignored by Git.  Cross-architecture arm64 creation requires
qemu-user-static and binfmt support on an x86_64 host.  Set
LUMEN_DEBIAN_MIRROR (or pass --mirror) to choose the package source.  When it
is omitted, the first HTTP(S) source configured by the host's APT files is
used; there is no project-specific mirror default.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --arch) ARCH="$2"; shift 2 ;;
        --distribution) DISTRIBUTION="$2"; shift 2 ;;
        --mirror) MIRROR="$2"; shift 2 ;;
        --image-root) IMAGE_ROOT="$2"; shift 2 ;;
        --rebuild) REBUILD=true; shift ;;
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

resolve_mirror() {
    if [[ -n "$MIRROR" ]]; then
        return
    fi

    local source_file candidate
    while IFS= read -r -d '' source_file; do
        candidate="$(grep -Eho 'https?://[^[:space:]]+' "$source_file" 2>/dev/null | head -n 1 || true)"
        if [[ -n "$candidate" ]]; then
            MIRROR="${candidate%/}"
            echo "[INFO] using Debian mirror discovered from ${source_file}: ${MIRROR}"
            return
        fi
    done < <(find /etc/apt -maxdepth 2 -type f \( -name '*.list' -o -name '*.sources' \) -print0 2>/dev/null)

    echo "ERROR: Debian mirror is not configured; set LUMEN_DEBIAN_MIRROR or pass --mirror URL" >&2
    exit 1
}

manifest_matches() {
    local manifest="$1" lumen_arch="$2" distribution="$3" package
    grep -Fxq "schema=1" "$manifest" &&
        grep -Fxq "arch=${lumen_arch}" "$manifest" &&
        grep -Fxq "distribution=${distribution}" "$manifest" || return 1
    for package in "${guest_packages[@]}"; do
        grep -Fxq "package=${package}" "$manifest" || return 1
    done
}

backup_artifact() {
    local artifact="$1" backup
    [[ -e "$artifact" ]] || return 0
    backup="${artifact}.legacy-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    mv -- "$artifact" "$backup"
    echo "[WARN] preserved previous artifact: ${backup}"
}

require_command sudo "install sudo/debootstrap prerequisites first"
require_command debootstrap "sudo apt install debootstrap"
require_command mke2fs "sudo apt install e2fsprogs"
require_command ssh-keygen "sudo apt install openssh-client"

host_arch="$(uname -m)"
if [[ "$host_arch" == "amd64" ]]; then host_arch="x86_64"; fi
if [[ "$host_arch" == "aarch64" ]]; then host_arch="arm64"; fi

cleanup_rootfs_mounts() {
    local rootfs="$1" mount

    # Cross-architecture bootstraps may bind host pseudo-filesystems into the
    # chroot.  They must not be copied into the ext4 image.
    for mount in "$rootfs/dev/pts" "$rootfs/dev" "$rootfs/proc" "$rootfs/sys" "$rootfs/run"; do
        if mountpoint -q "$mount"; then
            sudo umount -l "$mount"
        fi
    done
}

build_one() {
    local lumen_arch="$1" deb_arch rootfs image key manifest qemu_static binfmt_name
    local manifest_tmp
    local -a guest_packages
    local use_static=false
    case "$lumen_arch" in
        x86_64) deb_arch="amd64" ;;
        arm64) deb_arch="arm64" ;;
        *) echo "ERROR: unsupported architecture: $lumen_arch" >&2; exit 2 ;;
    esac
    rootfs="${IMAGE_ROOT}/${lumen_arch}/rootfs"
    image="${IMAGE_ROOT}/${lumen_arch}/debian.img"
    key="${IMAGE_ROOT}/${lumen_arch}/lumen_qemu_ed25519"
    manifest="${IMAGE_ROOT}/${lumen_arch}/guest-components.manifest"
    guest_packages=(
        openssh-server kmod iproute2 ca-certificates coreutils
        curl tar time strace psmisc iputils-ping dnsutils net-tools
        gcc libc6-dev make stress-ng
    )
    if [[ "$lumen_arch" == "arm64" ]]; then
        guest_packages+=(haveged)
    fi
    mkdir -p "$(dirname "$rootfs")"

    if [[ -e "$rootfs" ]]; then
        echo "ERROR: incomplete rootfs exists: $rootfs; remove it explicitly before rebuilding" >&2
        exit 1
    fi
    if [[ -s "$image" && -s "$key" && -s "${key}.pub" && -s "$manifest" ]] &&
        manifest_matches "$manifest" "$lumen_arch" "$DISTRIBUTION"; then
        echo "[OK] persistent SSH image with declared guest components exists: $lumen_arch"
        return
    fi
    if [[ "$REBUILD" != true ]] && [[ -e "$image" || -e "$key" || -e "${key}.pub" || -e "$manifest" ]]; then
        echo "ERROR: existing ${lumen_arch} artifacts have no matching guest-components.manifest; set LUMEN_QEMU_REBUILD=1 or pass --rebuild to preserve them and rebuild" >&2
        exit 1
    fi
    if [[ "$REBUILD" == true ]]; then
        backup_artifact "$image"
        backup_artifact "$key"
        backup_artifact "${key}.pub"
        backup_artifact "$manifest"
    fi
    resolve_mirror

    if [[ "$lumen_arch" != "$host_arch" ]]; then
        case "$lumen_arch" in
            x86_64) qemu_static="$(command -v qemu-x86_64-static || true)"; binfmt_name="qemu-x86_64" ;;
            arm64) qemu_static="$(command -v qemu-aarch64-static || true)"; binfmt_name="qemu-aarch64" ;;
        esac
        if [[ -x "$qemu_static" ]]; then
            use_static=true
        elif [[ ! -r "/proc/sys/fs/binfmt_misc/${binfmt_name}" ]] || ! grep -qx "enabled" "/proc/sys/fs/binfmt_misc/${binfmt_name}"; then
            echo "ERROR: ${lumen_arch} guest bootstrap needs $qemu_static or enabled ${binfmt_name}: install qemu-user-binfmt/binfmt-support" >&2
            exit 1
        fi
        sudo debootstrap --foreign --arch="$deb_arch" "$DISTRIBUTION" "$rootfs" "$MIRROR"
        if [[ "$use_static" == true ]]; then
            sudo cp "$qemu_static" "$rootfs/usr/bin/"
        fi
        sudo chroot "$rootfs" /debootstrap/debootstrap --second-stage
    else
        sudo debootstrap --arch="$deb_arch" "$DISTRIBUTION" "$rootfs" "$MIRROR"
    fi

    if [[ ! -f "$key" ]]; then
        # debootstrap runs under sudo, so runtime/qemu-ssh/<arch>/ is owned
        # by root. chown the key directory back to the invoking user before
        # ssh-keygen writes the keypair (without sudo).
        sudo install -d -m 0700 -o "$(id -u)" -g "$(id -g)" "$(dirname "$key")"
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
    # arm64 TCG on an x86 host may have too little entropy for sshd to
    # create host keys promptly; this is the same issue called out by
    # syzkaller's arm64 QEMU setup guide.
    sudo chroot "$rootfs" /usr/bin/env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        "${guest_packages[@]}"
    sudo chroot "$rootfs" systemctl enable ssh
    if [[ "$lumen_arch" == "arm64" ]]; then
        sudo chroot "$rootfs" systemctl enable serial-getty@ttyAMA0.service
    fi
    if [[ "$use_static" == true ]]; then
        sudo rm -f "$rootfs/usr/bin/$(basename "$qemu_static")"
    fi

    # A raw ext4 image is accepted by both x86 IDE and arm virtio block QEMU.
    cleanup_rootfs_mounts "$rootfs"
    truncate -s 2G "$image"
    sudo mke2fs -q -t ext4 -d "$rootfs" "$image"
    sudo chown "$(id -u):$(id -g)" "$image"
    sudo rm -rf "$rootfs"
    manifest_tmp="${manifest}.tmp.$$"
    {
        echo "schema=1"
        echo "arch=${lumen_arch}"
        echo "distribution=${DISTRIBUTION}"
        for package in "${guest_packages[@]}"; do
            echo "package=${package}"
        done
    } > "$manifest_tmp"
    mv -f -- "$manifest_tmp" "$manifest"
    echo "[OK] built persistent SSH image: $image"
}

case "$ARCH" in
    all) build_one x86_64; build_one arm64 ;;
    x86_64|arm64) build_one "$ARCH" ;;
    *) echo "ERROR: --arch must be x86_64, arm64, or all" >&2; exit 2 ;;
esac
