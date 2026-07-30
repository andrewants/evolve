"""Create a bounded, in-memory archive of Momentum's persistent data."""

from __future__ import annotations

import io
import os
import zipfile

MAX_BACKUP_BYTES = 45 * 1024 * 1024
MAX_BACKUP_FILES = 2_000


def create_data_backup(data_dir: str) -> tuple[bytes, int]:
    """Return a ZIP of regular files below *data_dir* and its file count.

    Symlinks are deliberately excluded so an entry in /data cannot make a
    backup disclose a file elsewhere in the container.
    """
    root = os.path.abspath(data_dir)
    if not os.path.isdir(root):
        raise ValueError("Data folder is not available")

    files: list[tuple[str, str]] = []
    total = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            name
            for name in dirs
            if not os.path.islink(os.path.join(current, name))
        )
        for name in sorted(names):
            path = os.path.join(current, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            if name.startswith(".momentum-") and name.endswith(".tmp"):
                continue
            size = os.path.getsize(path)
            total += size
            if total > MAX_BACKUP_BYTES:
                raise ValueError("Data folder is too large for a Telegram backup")
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            files.append((path, relative))
            if len(files) > MAX_BACKUP_FILES:
                raise ValueError("Data folder contains too many files to back up")

    if not files:
        raise ValueError("Data folder is empty")

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, relative in files:
            archive.write(path, relative)
    payload = output.getvalue()
    if len(payload) > MAX_BACKUP_BYTES:
        raise ValueError("Compressed backup is too large to send through Telegram")
    return payload, len(files)
