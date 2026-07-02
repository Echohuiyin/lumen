"""Cache B: crash command output.

Crash commands like `sys`, `ps`, `bt -a`, `log`, `waitq` on a fixed
vmcore+vmlinux pair are pure functions — same command + same vmcore +
same vmlinux always produces the same text output.  We wrap the crash
session with a disk-backed memoization layer.

Cache key: (vmcore_fingerprint, vmlinux_fingerprint, command_string).
Fingerprint: (resolved_path, file_size, file_mtime, head_1mb_md5).
Storage: ~/.cache/lumen/crash/<vmcore_fp>/<vmlinux_fp>.jsonl
         (one JSON line per command — append-only, self-healing).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

_CACHE_DIR = Path.home() / ".cache" / "lumen" / "crash"
_HEAD_BYTES = 1024 * 1024  # 1MB head slice for fingerprint


def _file_fingerprint(path: str) -> dict:
    """Fast fingerprint of a vmcore/vmlinux: (size, mtime, head_1mb_md5).

    Avoiding a full SHA-256 of 529MB-2.1GB files while still
    disambiguating reliably for our asset set.
    """
    st = os.stat(path)
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        head = f.read(_HEAD_BYTES)
        md5.update(head)
    return {
        "path": os.path.realpath(path),
        "size": st.st_size,
        "mtime": int(st.st_mtime),
        "head_md5": md5.hexdigest(),
    }


def _cache_path(vmcore_fp: dict, vmlinux_fp: dict) -> Path:
    """Derive the JSONL cache file path."""
    vmc_key = f"{vmcore_fp['size']}_{vmcore_fp['mtime']}_{vmcore_fp['head_md5']}"
    vml_key = f"{vmlinux_fp['size']}_{vmlinux_fp['mtime']}_{vmlinux_fp['head_md5']}"
    return _CACHE_DIR / vmc_key / f"{vml_key}.jsonl"


def _load_index(cache_file: Path) -> dict:
    """Load JSONL cache into an in-memory index: {command: (output, success)}.

    Lines that fail to parse are silently dropped (self-healing).
    """
    index: dict[str, tuple[str, bool]] = {}
    try:
        with open(cache_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    cmd = entry.get("command", "")
                    if cmd:
                        index[cmd] = (entry["output"], entry.get("success", True))
                except (json.JSONDecodeError, KeyError):
                    continue
    except (OSError, FileNotFoundError):
        pass
    return index


def _append_entry(cache_file: Path, command: str, output: str, success: bool) -> None:
    """Append one JSON line to the cache. Best-effort."""
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps({"command": command, "output": output, "success": success})
        with open(cache_file, "a") as f:
            f.write(entry + "\n")
    except OSError:
        pass


def lookup_command(
    vmcore_path: str,
    vmlinux_path: str,
    command: str,
) -> Optional[tuple[str, bool]]:
    """Look up a cached crash command result. Returns (output, success) or None."""
    try:
        vmcore_fp = _file_fingerprint(vmcore_path)
        vmlinux_fp = _file_fingerprint(vmlinux_path)
    except OSError:
        return None
    cache_file = _cache_path(vmcore_fp, vmlinux_fp)
    index = _load_index(cache_file)

    # Verify fingerprints still match (cache not stale from a vmcore regen)
    entry = index.get(command)
    if entry is None:
        return None
    # Re-check fingerprints on every lookup in case the file was replaced
    # at the same path.  _load_index loads all entries; the fingerprints
    # embedded in the path are checked here at query time.
    return entry


def store_command(
    vmcore_path: str,
    vmlinux_path: str,
    command: str,
    output: str,
    success: bool,
) -> None:
    """Cache a crash command result. Best-effort."""
    try:
        vmcore_fp = _file_fingerprint(vmcore_path)
        vmlinux_fp = _file_fingerprint(vmlinux_path)
    except OSError:
        return
    cache_file = _cache_path(vmcore_fp, vmlinux_fp)
    _append_entry(cache_file, command, output, success)
