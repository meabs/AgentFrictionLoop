"""Windows/macOS/Linux differences in one place: file locking, hook-free git, shell-safe paths."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

if os.name == "nt":
    import msvcrt

    def lock_exclusive(stream) -> None:
        """Block until an exclusive lock on the first byte is held; released when the stream closes."""
        stream.seek(0)
        while True:
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                continue  # LK_LOCK retries for ~10s then raises; keep waiting
else:
    import fcntl

    def lock_exclusive(stream) -> None:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)


def empty_hooks_dir() -> str:
    """A directory with no hooks, for `git -c core.hooksPath=...` (/dev/null does not exist on Windows)."""
    path = Path(tempfile.gettempdir()) / "fbl-no-hooks"
    try:
        path.mkdir(exist_ok=True)
    except OSError:
        return os.devnull
    return path.as_posix()


def posix(path: str | Path) -> str:
    """Forward-slash form, safe inside the sh scripts Git for Windows runs as hooks."""
    return Path(path).as_posix()


def python_executable() -> str:
    return sys.executable or ("python" if os.name == "nt" else "python3")
