"""Publish complete JSON files, tolerating brief Windows sharing conflicts."""

import json
import os
from pathlib import Path
import tempfile
import time

from bench_diagnostics import emit


def atomic_write_json(path, value):
    path = Path(path)
    temporary = None
    try:
        # Unique names avoid collisions between separate status publishers.
        # Close the handle before replace, as required on Windows.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
        for attempt in range(10):
            try:
                os.replace(temporary, path)
                return
            except PermissionError as exc:
                winerror = getattr(exc, "winerror", None)
                # Retry only Windows access/sharing/lock errors. Permanent
                # errors still surface; never fall back to a partial overwrite.
                if winerror not in {5, 32, 33} or attempt == 9:
                    emit("FILE_REPLACE_FAILED", file=path.name,
                         attempt=attempt + 1, winerror=winerror)
                    raise
                delay = min(0.025 * 2 ** attempt, 0.2)
                emit("FILE_REPLACE_RETRY", file=path.name, attempt=attempt + 1,
                     winerror=winerror, delay_seconds=delay)
                time.sleep(delay)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Preserve the original failure if a scanner also holds temp.
                emit("FILE_TEMP_CLEANUP_FAILED", file=temporary.name)
