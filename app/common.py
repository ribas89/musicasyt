from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")
PLAYLIST_PATTERN = re.compile(r"^[A-Za-z0-9_-]{5,128}$")
INVALID_COMPONENT = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')

def validate_username(username: str) -> str:
    if not isinstance(username, str) or not USERNAME_PATTERN.fullmatch(username) or username in {".", ".."}:
        raise ValueError("Invalid username")
    return username

def validate_playlist_id(playlist_id: str) -> str:
    if not isinstance(playlist_id, str) or not PLAYLIST_PATTERN.fullmatch(playlist_id):
        raise ValueError("Invalid playlist ID")
    return playlist_id

def playlist_id_from_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("Invalid playlist URL")
    parsed_url = urlsplit(url)
    if parsed_url.scheme != "https" or (parsed_url.hostname or "").lower() not in {
        "youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com"
    } or parsed_url.username or parsed_url.password or parsed_url.port not in (None, 443):
        raise ValueError("Expected a supported HTTPS playlist URL")
    values = parse_qs(parsed_url.query).get("list", [])
    if len(values) != 1:
        raise ValueError("Missing playlist ID in URL")
    return validate_playlist_id(values[0])

def clean_name(text: str, fallback: str, max_chars: int = 100) -> str:
    cleaned_name = INVALID_COMPONENT.sub("_", str(text or ""))
    cleaned_name = " ".join(cleaned_name.split()).strip(" .")
    return (cleaned_name[:max_chars].rstrip(" .") or fallback)

def user_library(users_root: Path, username: str) -> Path:
    validate_username(username)
    root = users_root.resolve(strict=True)
    candidate = root / username
    if not candidate.is_dir() or candidate.is_symlink():
        raise FileNotFoundError("Navidrome user library does not exist")
    user_dir = candidate.resolve(strict=True)
    if user_dir.parent != root:
        raise PermissionError("User library escapes configured root")
    return user_dir

def safe_child(parent: Path, name: str) -> Path:
    dest = parent / name
    if dest.is_symlink():
        raise PermissionError("Symbolic links are not allowed")
    if dest.resolve(strict=False).parent != parent.resolve(strict=True):
        raise PermissionError("Invalid directory destination")
    return dest
