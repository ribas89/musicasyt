from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from http.cookies import SimpleCookie
from urllib.parse import urlsplit
from urllib.request import Request as URLRequest

from app.common import clean_name, safe_child, user_library, validate_playlist_id
from app.metadata import read, modify, patch, timestamp, register_track
from app.logging_config import configure_logging

logger = logging.getLogger("musicasyt.downloader")

LEGACY_MARKER = ".musicasyt.json"

def emit(event: str, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)

def artist_name(track: dict) -> str:
    artists = track.get("artists") or []
    return (artists[0] or {}).get("name") if artists else "Unknown Artist"


def track_video_id(track: dict, index: int) -> str:
    candidates = [
        track.get("videoId"),
        track.get("video_id"),
        (track.get("watchEndpoint") or {}).get("videoId")
            if isinstance(track.get("watchEndpoint"), dict) else None,
        ((track.get("navigationEndpoint") or {}).get("watchEndpoint") or {}).get("videoId")
            if isinstance(track.get("navigationEndpoint"), dict)
            and isinstance((track.get("navigationEndpoint") or {}).get("watchEndpoint"), dict)
            else None,
    ]
    for value in candidates:
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{5,128}", value):
            return value

    thumbnails = track.get("thumbnails") or []
    if isinstance(thumbnails, list):
        for thumbnail in thumbnails:
            if not isinstance(thumbnail, dict):
                continue
            thumbnail_url = thumbnail.get("url")
            if not isinstance(thumbnail_url, str):
                continue
            path = urlsplit(thumbnail_url).path
            match = re.search(r"/vi(?:_webp)?/([A-Za-z0-9_-]{5,128})(?:/|$)", path)
            if match:
                logger.debug("[ytmusicapi] Recovered video ID from thumbnail URL: %s", match.group(1))
                return match.group(1)

    return f"null-index-{index}"


def track_is_unavailable(track: dict) -> bool:
    return track.get("isAvailable") is False or track.get("is_available") is False


def log_ytmusicapi_playlist_response(response: dict) -> None:
    if not debug_logging_enabled():
        return
    serialized = json.dumps(response, ensure_ascii=False, indent=2, default=str)
    logger.debug("[ytmusicapi] get_playlist response:")
    for line in serialized.splitlines():
        logger.debug("[ytmusicapi] %s", line)

def directory_for_playlist(user_dir: Path, title: str, playlist_id: str) -> Path:
    records = read(user_dir)["playlists"]
    saved_name = records.get(playlist_id, {}).get("folder_name")
    if isinstance(saved_name, str) and saved_name not in {"", ".", ".."} and Path(saved_name).name == saved_name:
        saved_path = safe_child(user_dir, saved_name)
        if saved_path.is_dir():
            return saved_path

    for folder in user_dir.iterdir():
        if not folder.is_dir() or folder.is_symlink():
            continue
        marker = folder / LEGACY_MARKER
        if marker.is_file() and not marker.is_symlink():
            try:
                if json.loads(marker.read_text(encoding="utf-8")).get("playlist_id") == playlist_id:
                    patch(user_dir, playlist_id, folder_name=folder.name, title=title)

                    marker.unlink(missing_ok=True)
                    return folder
            except (OSError, ValueError):
                continue

    name = clean_name(title, f"playlist_{playlist_id}")
    other_names = {
        entry.get("folder_name") for pid, entry in records.items()
        if pid != playlist_id and isinstance(entry, dict)
    }

    candidate = user_dir / name
    old_marker = candidate / LEGACY_MARKER
    if candidate.is_dir() and old_marker.is_file() and not old_marker.is_symlink():
        try:
            if json.loads(old_marker.read_text(encoding="utf-8")).get("playlist_id") != playlist_id:
                other_names.add(name)
        except (OSError, ValueError):
            other_names.add(name)
    if name in other_names:
        name = clean_name(f"{name} [{playlist_id}]", playlist_id)
    target = safe_child(user_dir, name)
    if target.exists() and (not target.is_dir() or target.is_symlink()):
        raise PermissionError("Unsafe playlist folder")
    target.mkdir(mode=0o755, exist_ok=True)
    patch(user_dir, playlist_id, title=title, folder_name=name)
    return target

def existing_mp3(folder: Path, base_name: str, video_id: str) -> bool:
    return (safe_child(folder, f"{base_name}.mp3").is_file()
            or safe_child(folder, f"{base_name} [{video_id}].mp3").is_file())

def resolve_track_file(folder: Path, base: str, video_id: str, tracks: dict) -> tuple[str, bool]:
    already = tracks.get(video_id)
    if isinstance(already, str) and already.endswith(".mp3") and Path(already).name == already:
        target = safe_child(folder, already)
        if target.is_file():
            return already, True
    claimed = {name for vid, name in tracks.items() if vid != video_id and isinstance(name, str)}
    legacy = safe_child(folder, f"{base} [{video_id}].mp3")
    target_name = f"{base}.mp3"
    target = safe_child(folder, target_name)

    if target.is_file() and target_name not in claimed:
        return target_name, True

    counter = 2
    while target.exists() or target_name in claimed:
        target_name = f"{base} ({counter}).mp3"
        target = safe_child(folder, target_name)
        counter += 1
    if legacy.is_file() and not legacy.is_symlink():
        legacy.rename(target)
        return target_name, True
    return target_name, False

def cookie_file_from_env() -> Path | None:

    value = os.environ.get("MUSICASYT_COOKIES", "").strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("MUSICASYT_COOKIES must be an absolute path, e.g. /data/cookies.txt")
    if path.is_symlink() or not path.is_file():
        raise ValueError("MUSICASYT_COOKIES does not point to a regular cookies file")
    try:
        with path.open("r", encoding="utf-8-sig") as cookies:
            header = cookies.readline().strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError("MUSICASYT_COOKIES cannot be read") from exc
    if header not in {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}:
        raise ValueError("MUSICASYT_COOKIES must be a Netscape/Mozilla cookies.txt export")
    return path

def user_agent_from_env() -> str | None:

    value = os.environ.get("MUSICASYT_USER_AGENT", "").strip()
    if not value:
        return None
    if "\r" in value or "\n" in value:
        raise ValueError("MUSICASYT_USER_AGENT must be a single-line value")
    if len(value) > 1024:
        raise ValueError("MUSICASYT_USER_AGENT is too long (maximum 1024 characters)")
    return value

def ytmusic_auth_headers(cookie_file: Path, user_agent: str | None = None) -> dict[str, str]:

    jar = http.cookiejar.MozillaCookieJar(str(cookie_file))
    try:

        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError) as exc:
        raise ValueError("MUSICASYT_COOKIES could not be parsed as a Netscape cookie file") from exc

    for cookie in jar:
        if cookie.expires == 0:
            cookie.expires = None
            cookie.discard = True

    request = URLRequest("https://music.youtube.com/")
    jar.add_cookie_header(request)
    cookie_header = request.get_header("Cookie")
    if not cookie_header:
        raise ValueError("MUSICASYT_COOKIES contains no cookies valid for music.youtube.com")

    parsed_cookies = SimpleCookie()
    parsed_cookies.load(cookie_header)
    sapisid_cookie = parsed_cookies.get("__Secure-3PAPISID")
    if sapisid_cookie is None or not sapisid_cookie.value:
        raise ValueError(
            "MUSICASYT_COOKIES is missing __Secure-3PAPISID, required for ytmusicapi browser authentication"
        )

    origin = "https://music.youtube.com"
    timestamp = str(int(time.time()))
    digest = hashlib.sha1(
        f"{timestamp} {sapisid_cookie.value} {origin}".encode("utf-8")
    ).hexdigest()

    from ytmusicapi.helpers import initialize_headers

    headers = {str(name): str(value) for name, value in initialize_headers().items()}
    headers.update({
        "Authorization": f"SAPISIDHASH {timestamp}_{digest}",
        "X-Goog-AuthUser": "0",
        "x-origin": origin,
        "Cookie": cookie_header,
    })
    if user_agent:
        for name in headers:
            if name.lower() == "user-agent":
                headers[name] = user_agent
                break
        else:
            headers["User-Agent"] = user_agent
    return headers

def debug_logging_enabled() -> bool:
    return os.environ.get("LOG_LEVEL", "INFO").strip().upper() == "DEBUG"

def _safe_request_url(url: str) -> str:
    parsed = urlsplit(str(url))
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _redact_ytdlp_log_line(line: str) -> str:
    clean = re.sub(
        r"(?i)(\b(?:cookie|authorization|proxy-authorization)\s*:\s*).*$",
        r"\1[REDACTED]",
        line,
    )
    clean = re.sub(
        r"(?i)(SAPISIDHASH\s+)[A-Za-z0-9_-]+",
        r"\1[REDACTED]",
        clean,
    )
    return clean

def create_ytmusic_client():
    from ytmusicapi import YTMusic

    cookies = cookie_file_from_env()
    user_agent = user_agent_from_env()
    auth_headers = ytmusic_auth_headers(cookies, user_agent) if cookies else None
    debug = debug_logging_enabled()

    if cookies:
        logger.info("[ytmusicapi] Browser authentication enabled from MUSICASYT_COOKIES")
    if user_agent:
        logger.info("[ytmusicapi] Custom shared User-Agent enabled")
    logger.debug(
        "[ytmusicapi] Client configuration: authenticated=%s custom_user_agent=%s request_tracing=%s",
        bool(auth_headers),
        bool(user_agent),
        debug,
    )

    if not debug and not user_agent:
        return YTMusic(auth=auth_headers) if auth_headers else YTMusic()

    import requests
    from requests.structures import CaseInsensitiveDict

    class LoggingSession(requests.Session):
        def request(self, method, url, **kwargs):
            if user_agent:
                headers = CaseInsensitiveDict(kwargs.get("headers") or {})
                headers["User-Agent"] = user_agent
                kwargs["headers"] = headers

            if not debug:
                return super().request(method, url, **kwargs)

            safe_url = _safe_request_url(url)
            started = time.monotonic()
            request_body = kwargs.get("data", kwargs.get("json"))
            if request_body is None:
                body_bytes = 0
            elif isinstance(request_body, bytes):
                body_bytes = len(request_body)
            elif isinstance(request_body, str):
                body_bytes = len(request_body.encode("utf-8"))
            else:
                try:
                    body_bytes = len(json.dumps(request_body, separators=(",", ":")).encode("utf-8"))
                except (TypeError, ValueError):
                    body_bytes = -1
            logger.debug(
                "[ytmusicapi] -> %s %s body_bytes=%s",
                method.upper(),
                safe_url,
                body_bytes,
            )
            try:
                response = super().request(method, url, **kwargs)
            except Exception as error:
                logger.error(
                    "[ytmusicapi] %s %s failed after %.2fs (%s)",
                    method.upper(),
                    safe_url,
                    time.monotonic() - started,
                    type(error).__name__,
                )
                raise

            content_length = response.headers.get("Content-Length")
            if content_length is None:
                content_length = len(response.content)
            if response.status_code >= 400:
                logger.warning(
                    "[ytmusicapi] <- %s %s HTTP %s (%.2fs)",
                    method.upper(),
                    _safe_request_url(response.url),
                    response.status_code,
                    time.monotonic() - started,
                )
            else:
                logger.debug(
                    "[ytmusicapi] <- %s %s HTTP %s (%s bytes, %.2fs)",
                    method.upper(),
                    _safe_request_url(response.url),
                    response.status_code,
                    content_length,
                    time.monotonic() - started,
                )
            return response

    if debug:
        logger.debug("[ytmusicapi] DEBUG HTTP tracing enabled")
    return YTMusic(auth=auth_headers, requests_session=LoggingSession())

def _run_ytdlp_debug(args: list[str], timeout: int) -> tuple[int | None, str, bool]:

    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None

    output_queue: queue.Queue[str | None] = queue.Queue()

    def forward_output() -> None:
        try:
            for output_line in process.stdout:
                output_queue.put(output_line)
        finally:
            output_queue.put(None)

    reader = threading.Thread(target=forward_output, name="yt-dlp-log-reader", daemon=True)
    reader.start()

    tail: deque[str] = deque(maxlen=40)
    deadline = time.monotonic() + timeout
    timed_out = False

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            logger.error("[yt-dlp] Download exceeded timeout of %s seconds; terminating", timeout)
            process.kill()
            break

        try:
            output_line = output_queue.get(timeout=min(0.5, remaining))
        except queue.Empty:
            if process.poll() is not None and not reader.is_alive():
                break
            continue

        if output_line is None:
            break

        clean_line = _redact_ytdlp_log_line(output_line.rstrip("\r\n"))
        if clean_line:
            tail.append(clean_line)
            logger.debug("[yt-dlp] %s", clean_line)

    try:
        return_code = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        return_code = process.wait()
    reader.join(timeout=2)

    return (None if timed_out else return_code), "\n".join(tail)[-600:], timed_out

def download_track(video_id: str, folder: Path, file_name: str, data_dir: Path) -> tuple[bool, str]:
    output = safe_child(folder, file_name[:-4] + ".%(ext)s")
    debug = debug_logging_enabled()
    progress_args = ["--newline", "--verbose"] if debug else ["--no-progress"]
    args = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist", *progress_args, "--continue", "--no-overwrites",
        "--js-runtimes", "deno", "--remote-components", "ejs:github",
        "--socket-timeout", "30", "--retries", "3", "--fragment-retries", "3",
        "--sleep-interval", "1", "--max-sleep-interval", "3",
        "--cache-dir", str(data_dir / "yt-dlp-cache"),
        "-f", "bestaudio/best", "--extract-audio", "--audio-format", "mp3",
        "--audio-quality", "0", "--embed-metadata", "--embed-thumbnail",
        "--convert-thumbnails", "jpg", "-o", str(output),
        f"https://www.youtube.com/watch?v={video_id}",
    ]

    cookies = cookie_file_from_env()
    if cookies is not None:
        args[-1:-1] = ["--cookies", str(cookies)]

    user_agent = user_agent_from_env()
    if user_agent is not None:
        args[-1:-1] = ["--user-agent", user_agent]

    logger.debug(
        "[yt-dlp] Starting video_id=%s verbose=%s cookies_enabled=%s custom_user_agent=%s output=%s",
        video_id,
        debug,
        cookies is not None,
        user_agent is not None,
        output,
    )
    if debug:
        logger.debug("[yt-dlp] Full verbose output will be forwarded to container stderr")
        return_code, diagnostics, timed_out = _run_ytdlp_debug(args, timeout=900)
        if timed_out:
            return False, f"Download timed out. Last yt-dlp output: {diagnostics}"
    else:
        try:
            result = subprocess.run(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=900,
            )
        except subprocess.TimeoutExpired:
            return False, "Download timed out"
        return_code = result.returncode
        diagnostics = (result.stdout or "")[-600:]
        if return_code != 0:
            logger.warning("yt-dlp failed for video_id=%s (exit=%s): %s", video_id, return_code, diagnostics)

    success = return_code == 0 and safe_child(folder, file_name).is_file()
    if success:
        logger.debug("yt-dlp completed successfully for video_id=%s", video_id)
    elif return_code == 0:
        logger.warning("yt-dlp exited successfully but expected MP3 was not found: %s", file_name)
    return success, diagnostics[-600:]

def save_playlist_progress(user_dir: Path, playlist_id: str, counts: dict, status: str, **extra):
    total = extra.pop("total", None)
    if total is None:
        total = read(user_dir)["playlists"].get(playlist_id, {}).get("total")
    processed = sum(counts.values())
    fields = {
        **counts, "status": status, "total": total,
        "remaining": max(0, total - processed) if isinstance(total, int) else None,
        "percent": round(processed * 100 / total, 1) if total else (100.0 if total == 0 else None),
        **extra,
    }
    patch(user_dir, playlist_id, **fields)

def run_playlist_download(username: str, playlist_id: str, users_root: Path, data_dir: Path):
    validate_playlist_id(playlist_id)
    user_dir = user_library(users_root, username)
    counts = {"downloaded": 0, "existing": 0, "failed": 0, "unavailable": 0}
    patch(user_dir, playlist_id, status="reading_playlist", error=None)
    logger.info("Starting playlist sync: user=%s playlist_id=%s", username, playlist_id)
    try:
        cookie_file_from_env()
        emit("state", status="reading_playlist")
        logger.info("Reading playlist metadata")
        client = create_ytmusic_client()
        logger.debug("[ytmusicapi] get_playlist started: playlist_id=%s limit=None", playlist_id)
        info = client.get_playlist(playlist_id, limit=None)
        log_ytmusicapi_playlist_response(info)
        title = info.get("title") or f"playlist_{playlist_id}"
        tracks = info.get("tracks") or []
        if not isinstance(tracks, list):
            raise ValueError("Invalid playlist metadata")
        logger.info("Playlist loaded: title=%r tracks=%d", title, len(tracks))
        logger.debug("[ytmusicapi] get_playlist completed: playlist_id=%s tracks=%d", playlist_id, len(tracks))
        patch(
            user_dir, playlist_id, title=title, total=len(tracks), status="checking_files",
            tracks_unavailable={}, tracks_error={},
        )
        emit("state", status="checking_files")
        folder = directory_for_playlist(user_dir, title, playlist_id)
        logger.info("Checking local files in %s", folder)
        emit("metadata", title=title, folder=str(folder), total=len(tracks))
        patch(user_dir, playlist_id, folder_name=folder.name, title=title, status="downloading")
        emit("state", status="downloading")
        for index, track in enumerate(tracks, start=1):
            title_track = clean_name(track.get("title") or "Unknown Title", "Unknown Title", 90)
            artist = clean_name(artist_name(track) or "Unknown Artist", "Unknown Artist", 60)
            video_id = track_video_id(track, index)
            missing_video_id = video_id.startswith("null-index-")
            logger.debug("Track %d/%d metadata: artist=%r title=%r video_id=%r", index, len(tracks), artist, title_track, video_id)
            if missing_video_id or track_is_unavailable(track):
                counts["unavailable"] += 1
                unavailable_name = clean_name(f"{artist} - {title_track}", video_id, 145) + ".mp3"
                register_track(user_dir, playlist_id, "tracks_unavailable", video_id, unavailable_name)
                if missing_video_id:
                    logger.warning(
                        "Track %d/%d has no video ID in metadata or thumbnail URL; registered as %s: %s",
                        index, len(tracks), video_id, title_track,
                    )
                else:
                    logger.debug("Registered unavailable track by video ID: %s", video_id)
                logger.warning("Track %d/%d is unavailable: %s", index, len(tracks), title_track)
                save_playlist_progress(user_dir, playlist_id, counts, "downloading", current_track=title_track)
                emit("progress", **counts, current_track=title_track)
                continue
            base = clean_name(f"{artist} - {title_track}", video_id, 145)
            record = read(user_dir)["playlists"].get(playlist_id, {})
            tracks_index = record.get("tracks", {})
            if not isinstance(tracks_index, dict):
                tracks_index = {}
            file_name, exists = resolve_track_file(folder, base, video_id, tracks_index)
            if not exists:
                logger.info("Downloading track %d/%d: %s - %s", index, len(tracks), artist, title_track)
                logger.debug("Target file: %s", folder / file_name)
                patch(user_dir, playlist_id, current_track=title_track, status="downloading")
                emit("track", current_track=title_track)
                success, diagnostics = download_track(video_id, folder, file_name, data_dir)
                if success:
                    counts["downloaded"] += 1
                    logger.info("Downloaded: %s", file_name)
                else:
                    counts["failed"] += 1
                    register_track(user_dir, playlist_id, "tracks_error", video_id, file_name)
                    logger.error("Track download failed: %s; details: %s", title_track, diagnostics[-240:])
                    emit("track_error", current_track=title_track, message=diagnostics[-240:])
            else:
                success = True
                counts["existing"] += 1
                logger.debug("Already present; skipping: %s", file_name)
            if success:
                register_track(user_dir, playlist_id, "tracks", video_id, file_name)
            save_playlist_progress(user_dir, playlist_id, counts, "downloading", current_track=title_track)
            emit("progress", **counts, current_track=title_track)
        result = "completed" if counts["failed"] == 0 and counts["unavailable"] == 0 else "completed_with_errors"
        save_playlist_progress(user_dir, playlist_id, counts, result, current_track=None, last_synced_at=timestamp())
        logger.info(
            "Playlist sync %s: downloaded=%d existing=%d failed=%d unavailable=%d total=%d",
            result, counts["downloaded"], counts["existing"], counts["failed"], counts["unavailable"], len(tracks),
        )
        emit("done", status=result, **counts)
    except Exception as exc:
        logger.exception("Playlist sync failed: user=%s playlist_id=%s", username, playlist_id)
        patch(user_dir, playlist_id, status="error", error=str(exc)[:400], current_track=None)
        raise

def main():
    configure_logging(stream=sys.stderr)
    logger.info("Downloader process started with LOG_LEVEL=%s", os.environ.get("LOG_LEVEL", "INFO").strip().upper())
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True)
    parser.add_argument("--playlist-id", required=True)
    parser.add_argument("--users-root", default="/users")
    parser.add_argument("--data-dir", default="/data")
    args = parser.parse_args()
    try:
        run_playlist_download(
            args.username,
            args.playlist_id,
            Path(args.users_root),
            Path(args.data_dir),
        )
    except Exception as error:
        emit("error", message=str(error)[:400])
        sys.exit(1)

if __name__ == "__main__":
    main()
