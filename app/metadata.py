from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

FILENAME = "musicasyt.json"
MAX_BYTES = 16 * 1024 * 1024
TRACK_COLLECTIONS = ("tracks", "tracks_unavailable", "tracks_error")

def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()

def _load(user_dir: Path) -> dict:
    path = user_dir / FILENAME
    if path.is_symlink():
        raise PermissionError("Refusing a symlink for MusicasYT metadata")
    if not path.exists():
        return {"version": 1, "playlists": {}}
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("Invalid MusicasYT metadata file")
    content = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(content, dict) or not isinstance(content.get("playlists"), dict):
        raise ValueError("Invalid MusicasYT metadata format")
    return content

def read(user_dir: Path) -> dict:
    return _load(user_dir)

@contextmanager
def _directory_lock(user_dir: Path):
    fd = os.open(user_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

def modify(user_dir: Path, change: Callable[[dict], None]) -> dict:
    with _directory_lock(user_dir):
        content = _load(user_dir)
        change(content["playlists"])
        content["version"] = 1
        encoded = (json.dumps(content, ensure_ascii=False, indent=2) + "\n").encode()
        if len(encoded) > MAX_BYTES:
            raise ValueError("MusicasYT metadata exceeds size limit")
        fd, temp = tempfile.mkstemp(prefix=".musicasyt-", suffix=".tmp", dir=user_dir)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(encoded)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, user_dir / FILENAME)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return content

def patch(user_dir: Path, playlist_id: str, **fields) -> dict:
    result = {}
    def apply(playlists):
        nonlocal result
        current = playlists.setdefault(playlist_id, {"playlist_id": playlist_id})
        for name in TRACK_COLLECTIONS:
            if not isinstance(current.get(name), dict):
                current[name] = {}
        current.update(fields)
        current["updated_at"] = timestamp()
        result = dict(current)
    modify(user_dir, apply)
    return result

def delete_entry(user_dir: Path, playlist_id: str) -> None:
    modify(user_dir, lambda playlists: playlists.pop(playlist_id, None))


def register_track(user_dir: Path, playlist_id: str, collection: str, track_id: str, file_name: str) -> dict:
    if collection not in TRACK_COLLECTIONS:
        raise ValueError("Invalid track collection")
    if not isinstance(track_id, str) or not track_id:
        raise ValueError("Invalid track identifier")
    if not isinstance(file_name, str) or not file_name:
        raise ValueError("Invalid track name")

    result = {}

    def apply(playlists):
        current = playlists.setdefault(playlist_id, {"playlist_id": playlist_id})
        for name in TRACK_COLLECTIONS:
            if not isinstance(current.get(name), dict):
                current[name] = {}
            if name != collection:
                current[name].pop(track_id, None)
        current[collection][track_id] = file_name
        current["updated_at"] = timestamp()
        result.update(current)

    modify(user_dir, apply)
    return result
