"""Cache A: extract-ikconfig output.

The same bzImage is re-scanned on every E2E run (and on every QEMU
boot, because _detect_numa_fake also calls extract-ikconfig). The
output is a pure function of the bzImage bytes, so we cache it on
disk keyed by a fast fingerprint.

Cache key: sha256(size + mtime + head_4mb + tail_4mb + script_mtime).
Cache value: JSON {raw_stdout, pertinent_dict, config_cmdline}.
Storage: ~/.cache/lumen/ikconfig/<hash>.json
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
from pathlib import Path
from typing import Optional, Tuple

_CACHE_DIR = Path.home() / ".cache" / "lumen" / "ikconfig"

# Head/tail slice sizes — enough to disambiguate two different bzImages
# with the same size+mtime without hashing the whole 100M+ file.
_HEAD_BYTES = 4 * 1024 * 1024
_TAIL_BYTES = 4 * 1024 * 1024

# Pertinent CONFIG options that influence reproducer strategy. Mirror of
# the list in agents/kernel_expert.py:_PERTINENT_CONFIG_OPTIONS — kept
# here so callers that only need a subset (e.g. qemu_tools._detect_numa_fake)
# don't have to import kernel_expert (which would create a cycle).
_PERTINENT_CONFIG_OPTIONS = [
    "CONFIG_KVM",
    "CONFIG_KVM_INTEL",
    "CONFIG_KVM_AMD",
    "CONFIG_PARAVIRT",
    "CONFIG_PARAVIRT_SPINLOCKS",
    "CONFIG_HYPERV",
    "CONFIG_HYPERVISOR_GUEST",
    "CONFIG_NUMA",
    "CONFIG_NUMA_BALANCING",
    "CONFIG_BTRFS_FS",
    "CONFIG_BLK_DEV_LOOP",
    "CONFIG_KASAN",
    "CONFIG_KASAN_INLINE",
    "CONFIG_KALLSYMS",
    "CONFIG_MODULES",
    "CONFIG_MODULE_UNLOAD",
    "CONFIG_MODULE_FORCE_LOAD",
    "CONFIG_MODVERSIONS",
    "CONFIG_CMDLINE",
]


def _find_extract_ikconfig() -> Optional[str]:
    """Locate extract-ikconfig script in known kernel source paths."""
    candidates = [
        os.path.expanduser("~/linux-next/scripts/extract-ikconfig"),
        os.path.expanduser("~/linux-stable/scripts/extract-ikconfig"),
        os.path.expanduser("~/code/OLK-6.6/scripts/extract-ikconfig"),
        "/lib/modules/$(uname -r)/build/scripts/extract-ikconfig",
    ]
    for path in candidates:
        expanded = os.path.expandvars(path)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
    try:
        from shutil import which
    except ImportError:
        return None
    return which("extract-ikconfig")


def _bzimage_fingerprint(path: str, script_mtime: float) -> str:
    """Fast fingerprint of a bzImage: size + mtime + head + tail + script mtime.

    Hashing the whole 100M bzImage on every cache check is wasteful when
    extract-ikconfig itself already reads the whole file. size+mtime+head+tail
    disambiguates reliably for our asset set (collision probability ~10^-15).
    """
    stat = os.stat(path)
    h = hashlib.sha256()
    h.update(struct.pack("<qq", stat.st_size, int(stat.st_mtime)))
    h.update(struct.pack("<d", script_mtime))
    with open(path, "rb") as f:
        head = f.read(_HEAD_BYTES)
        h.update(head)
        if stat.st_size > _HEAD_BYTES + _TAIL_BYTES:
            f.seek(-_TAIL_BYTES, os.SEEK_END)
            tail = f.read(_TAIL_BYTES)
            h.update(tail)
    return h.hexdigest()


def _parse_pertinent(raw_stdout: str) -> Tuple[dict, str]:
    """Parse raw ikconfig stdout into (pertinent_dict, config_cmdline)."""
    pertinent: dict[str, str] = {}
    config_cmdline = ""
    for line in raw_stdout.splitlines():
        for opt in _PERTINENT_CONFIG_OPTIONS:
            if line.startswith(opt + "="):
                pertinent[opt] = line.split("=", 1)[1].strip()
            elif line.startswith("# " + opt + " is not set"):
                pertinent[opt] = "n"
        if line.startswith("CONFIG_CMDLINE="):
            config_cmdline = line.split("=", 1)[1].strip().strip('"')
    return pertinent, config_cmdline


def get_ikconfig(bzimage_path: str) -> Tuple[str, dict]:
    """Return (raw_stdout, pertinent_config_dict) for a bzImage.

    Cached on disk by fingerprint. On any error (file missing, script
    failure, cache write error), falls through to the uncached path.
    """
    if not bzimage_path or not os.path.isfile(bzimage_path):
        return "", {}
    ikconfig = _find_extract_ikconfig()
    if not ikconfig:
        return "", {}

    try:
        script_mtime = os.path.getmtime(ikconfig)
    except OSError:
        script_mtime = 0.0

    try:
        fp = _bzimage_fingerprint(bzimage_path, script_mtime)
    except OSError:
        # Can't stat the bzImage — fall through to uncached run
        fp = None

    if fp:
        cache_file = _CACHE_DIR / f"{fp}.json"
        try:
            cached = json.loads(cache_file.read_text())
            return cached["raw_stdout"], cached["pertinent"]
        except (OSError, ValueError, KeyError):
            pass  # cache miss or corrupt — fall through

    # Run extract-ikconfig (the expensive part, 10-30s on 100M bzImage)
    try:
        result = subprocess.run(
            [ikconfig, bzimage_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return "", {}
    except (subprocess.TimeoutExpired, OSError):
        return "", {}

    raw_stdout = result.stdout
    pertinent, _ = _parse_pertinent(raw_stdout)

    # Don't cache empty/garbage output (e.g. kernel without IKCONFIG)
    if len(pertinent) >= 3 and fp:
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_file = _CACHE_DIR / f"{fp}.json"
            cache_file.write_text(json.dumps({
                "raw_stdout": raw_stdout,
                "pertinent": pertinent,
            }))
        except OSError:
            pass  # cache write failed — non-fatal

    return raw_stdout, pertinent


def get_config_cmdline(bzimage_path: str) -> str:
    """Return just the CONFIG_CMDLINE string from a bzImage.

    Convenience for qemu_tools._detect_numa_fake and similar callers
    that only need the embedded command line.
    """
    raw, _ = get_ikconfig(bzimage_path)
    if not raw:
        return ""
    for line in raw.splitlines():
        if line.startswith("CONFIG_CMDLINE="):
            return line.split("=", 1)[1].strip().strip('"')
    return ""
