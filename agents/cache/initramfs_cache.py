"""Cache C: initramfs cpio.gz.

create_initramfs is called once per test attempt (up to 3× per E2E).
With the same arch + test.sh + modules + binaries + script version,
the cpio.gz output is byte-identical. Test retries that don't change
the reproducer can reuse the cached cpio.gz instead of rebuilding it.

Cache key: sha256(arch + script_path + script_mtime + test_script
                 + modules_dir contents + binaries_dir contents).
Cache value: cpio.gz bytes (stored once under the cache dir; copied
             to the requested output_path on hit).
Storage: ~/.cache/lumen/initramfs/<hash>.cpio.gz
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional

_CACHE_DIR = Path.home() / ".cache" / "lumen" / "initramfs"


def _hash_file(h: hashlib._Hash, path: Path) -> None:
    """Hash a single file's contents."""
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)


def _hash_dir(h: hashlib._Hash, dir_path: Path, suffixes: tuple[str, ...]) -> None:
    """Hash a directory's file listing + contents of files matching suffixes.

    Includes file names + sizes + mtimes in the hash so a directory
    recompile (newer .ko mtime) invalidates without re-reading contents.
    """
    entries = sorted(dir_path.iterdir(), key=lambda p: p.name)
    for entry in entries:
        if entry.is_file():
            try:
                stat = entry.stat()
                h.update(entry.name.encode())
                h.update(str(stat.st_size).encode())
                h.update(str(int(stat.st_mtime)).encode())
                if entry.suffix in suffixes:
                    _hash_file(h, entry)
            except OSError:
                continue
        elif entry.is_dir():
            h.update(entry.name.encode())


def _compute_cache_key(
    *,
    arch: str,
    script_path: Path,
    test_script_path: Optional[str],
    modules_dir: Optional[str],
    binaries_dir: Optional[str],
) -> str:
    """Build a content hash over all inputs that affect cpio.gz output."""
    h = hashlib.sha256()
    h.update(arch.encode())
    h.update(str(script_path).encode())
    try:
        h.update(str(int(os.path.getmtime(script_path))).encode())
    except OSError:
        pass

    if test_script_path and os.path.isfile(test_script_path):
        h.update(b"test_script:")
        _hash_file(h, Path(test_script_path))

    if modules_dir:
        mdir = Path(modules_dir)
        if mdir.is_file() and mdir.suffix == ".ko":
            # Single .ko file passed directly
            h.update(b"module:")
            _hash_file(h, mdir)
        elif mdir.is_dir():
            h.update(b"modules_dir:")
            _hash_dir(h, mdir, suffixes=(".ko", ".c", "Makefile"))

    if binaries_dir and Path(binaries_dir).is_dir():
        h.update(b"binaries_dir:")
        _hash_dir(h, Path(binaries_dir), suffixes=("",))  # hash all files

    return h.hexdigest()


def lookup(key: str) -> Optional[Path]:
    """Return cached cpio.gz path if it exists, else None."""
    p = _CACHE_DIR / f"{key}.cpio.gz"
    return p if p.exists() else None


def store(key: str, src_path: str) -> None:
    """Copy a freshly built cpio.gz into the cache. Best-effort.

    Uses copy (not move) so the original output_path remains valid for
    the caller that just built it.
    """
    try:
        import shutil
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        dst = _CACHE_DIR / f"{key}.cpio.gz"
        shutil.copy2(src_path, dst)
    except OSError:
        pass


def get_or_build_key(
    *,
    arch: str,
    script_path: Path,
    test_script_path: Optional[str],
    modules_dir: Optional[str],
    binaries_dir: Optional[str],
) -> str:
    """Compute the cache key for the given inputs. Used by create_initramfs
    to decide whether to short-circuit on a cache hit."""
    return _compute_cache_key(
        arch=arch,
        script_path=script_path,
        test_script_path=test_script_path,
        modules_dir=modules_dir,
        binaries_dir=binaries_dir,
    )
