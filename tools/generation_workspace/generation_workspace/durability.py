"""Durability primitives: fsync for files and directories, same-filesystem
preflight check.
"""

from __future__ import annotations

import os
from pathlib import Path


def fsync_file(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class CrossFilesystemError(RuntimeError):
    pass


def assert_same_filesystem(*paths: Path) -> None:
    """Preflight: all given paths (that exist) must be on the same
    filesystem/device. Raises CrossFilesystemError otherwise (EXDEV guard).
    """
    existing = [p for p in paths if p.exists()]
    if len(existing) < 2:
        return
    device_ids = {p.stat().st_dev for p in existing}
    if len(device_ids) > 1:
        raise CrossFilesystemError(f"paths span multiple filesystems: {[str(p) for p in existing]}")
