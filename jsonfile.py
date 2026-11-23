"""
jsonfile.py - crash-safe JSON files.

Writes go to a temporary file in the same folder that is then renamed over the
original, so a crash or power cut mid-write leaves the old file intact instead
of a truncated one.
"""

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).parent
# Runtime state lives in data/, which docker-compose.yml mounts as a folder.
# (Renaming over a single-file bind mount fails, so atomic writes need this.)
DATA_DIR = Path(os.environ.get("DATA_DIR") or APP_DIR / "data")


def data_path(name: str) -> Path:
    """
    data/<name>, copied once from the old location next to the code
    (where state.json and player_map.json used to live) if it's still there.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / name
    legacy = APP_DIR / name
    if not path.exists() and legacy.is_file() and legacy.stat().st_size > 0:
        shutil.copyfile(legacy, path)
        log.info(f"Moved {name} into {DATA_DIR}")
    return path


def write(path: str | Path, data, indent: int | None = 2) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp makes the file owner-only; keep the old file's permissions.
        os.chmod(tmp, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read(path: str | Path, default=None):
    """Parsed JSON, or `default` if the file is missing or empty."""
    path = Path(path)
    if not path.exists():
        return default
    content = path.read_text(encoding="utf-8").strip()
    return json.loads(content) if content else default
