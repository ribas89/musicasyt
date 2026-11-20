from pathlib import Path
import re
import subprocess
import sys
import types
from types import SimpleNamespace

import pytest

from app import downloader

def valid_cookie_file(tmp_path: Path) -> Path:
    path = tmp_path / "cookies.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\t__Secure-3PAPISID\tsapisid-test-value\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsid-test-value\n",
        encoding="utf-8",
    )
    return path

def mock_run(captured):
    def fake_run(args, **kwargs):
        captured.append(args)
        return SimpleNamespace(returncode=0, stdout="")
    return fake_run

def test_empty_shared_cookies_env_does_not_add_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("MUSICASYT_COOKIES", raising=False)
    captures = []
    monkeypatch.setattr(subprocess, "run", mock_run(captures))
    downloader.download_track("abcdefghijk", tmp_path, "Song.mp3", tmp_path)
    assert "--cookies" not in captures[0]

def test_whitespace_shared_cookies_env_does_not_add_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICASYT_COOKIES", "   ")
    captures = []
    monkeypatch.setattr(subprocess, "run", mock_run(captures))
    downloader.download_track("abcdefghijk", tmp_path, "Song.mp3", tmp_path)
    assert "--cookies" not in captures[0]

def test_shared_cookie_file_adds_ytdlp_flag(tmp_path, monkeypatch):
    path = valid_cookie_file(tmp_path)
    monkeypatch.setenv("MUSICASYT_COOKIES", str(path))
    captures = []
    monkeypatch.setattr(subprocess, "run", mock_run(captures))
    downloader.download_track("abcdefghijk", tmp_path, "Song.mp3", tmp_path)
    argv = captures[0]
    assert argv[argv.index("--cookies") + 1] == str(path)
    assert argv.index("--cookies") < len(argv) - 1
    assert argv[-1] == "https://www.youtube.com/watch?v=abcdefghijk"

def test_invalid_cookie_file_fails_once(tmp_path, monkeypatch):
    invalid = tmp_path / "bad.txt"
    invalid.write_text('{"cookies": []}')
    monkeypatch.setenv("MUSICASYT_COOKIES", str(invalid))
    with pytest.raises(ValueError, match="Netscape/Mozilla"):
        downloader.cookie_file_from_env()

def test_configured_missing_cookie_file_is_error(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICASYT_COOKIES", str(tmp_path / "missing.txt"))
    with pytest.raises(ValueError, match="regular cookies file"):
        downloader.cookie_file_from_env()

def test_relative_cookie_path_rejected(monkeypatch):
    monkeypatch.setenv("MUSICASYT_COOKIES", "cookies.txt")
    with pytest.raises(ValueError, match="absolute path"):
        downloader.cookie_file_from_env()

def test_cookie_file_converts_to_ytmusic_browser_auth(tmp_path, monkeypatch):
    path = valid_cookie_file(tmp_path)
    monkeypatch.setenv("MUSICASYT_COOKIES", str(path))
    helpers_module = types.ModuleType("ytmusicapi.helpers")
    helpers_module.initialize_headers = lambda: {
        "user-agent": "ytmusicapi-default-agent",
        "accept": "*/*",
        "accept-encoding": "gzip, deflate",
        "content-type": "application/json",
        "content-encoding": "gzip",
        "origin": "https://music.youtube.com",
    }
    monkeypatch.setitem(sys.modules, "ytmusicapi.helpers", helpers_module)
    headers = downloader.ytmusic_auth_headers(path)

    assert headers["X-Goog-AuthUser"] == "0"
    assert headers["x-origin"] == "https://music.youtube.com"
    assert headers["Cookie"].find("__Secure-3PAPISID=sapisid-test-value") >= 0
    assert headers["user-agent"] == "ytmusicapi-default-agent"
    assert headers["accept-encoding"] == "gzip, deflate"
    assert re.fullmatch(r"SAPISIDHASH \d+_[0-9a-f]{40}", headers["Authorization"])
    assert "sapisid-test-value" not in headers["Authorization"]

def test_cookie_conversion_requires_secure_3papisid(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsid-test-value\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="__Secure-3PAPISID"):
        downloader.ytmusic_auth_headers(path)

def test_shared_user_agent_added_to_ytdlp(tmp_path, monkeypatch):
    monkeypatch.delenv("MUSICASYT_COOKIES", raising=False)
    monkeypatch.setenv("MUSICASYT_USER_AGENT", "MusicasYT-Test/1.0")
    captures = []
    monkeypatch.setattr(subprocess, "run", mock_run(captures))
    downloader.download_track("abcdefghijk", tmp_path, "Song.mp3", tmp_path)
    argv = captures[0]
    assert argv[argv.index("--user-agent") + 1] == "MusicasYT-Test/1.0"

def test_user_agent_rejects_newlines(monkeypatch):
    monkeypatch.setenv("MUSICASYT_USER_AGENT", "bad-agent\nInjected: true")
    with pytest.raises(ValueError, match="single-line"):
        downloader.user_agent_from_env()

def test_user_agent_rejects_oversized_value(monkeypatch):
    monkeypatch.setenv("MUSICASYT_USER_AGENT", "x" * 1025)
    with pytest.raises(ValueError, match="too long"):
        downloader.user_agent_from_env()

def test_ytmusic_client_uses_shared_cookie_and_user_agent(tmp_path, monkeypatch):
    path = valid_cookie_file(tmp_path)
    monkeypatch.setenv("MUSICASYT_COOKIES", str(path))
    monkeypatch.setenv("MUSICASYT_USER_AGENT", "MusicasYT-Test/1.0")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    captured = {}

    fake_module = types.ModuleType("ytmusicapi")
    helpers_module = types.ModuleType("ytmusicapi.helpers")
    helpers_module.initialize_headers = lambda: {
        "user-agent": "ytmusicapi-default-agent",
        "accept": "*/*",
        "accept-encoding": "gzip, deflate",
        "content-type": "application/json",
        "content-encoding": "gzip",
        "origin": "https://music.youtube.com",
    }

    def fake_ytmusic(**kwargs):
        captured.update(kwargs)
        return object()

    fake_module.YTMusic = fake_ytmusic
    monkeypatch.setitem(sys.modules, "ytmusicapi", fake_module)
    monkeypatch.setitem(sys.modules, "ytmusicapi.helpers", helpers_module)

    downloader.create_ytmusic_client()

    assert captured["auth"]["X-Goog-AuthUser"] == "0"
    assert "__Secure-3PAPISID=sapisid-test-value" in captured["auth"]["Cookie"]
    assert captured["auth"]["user-agent"] == "MusicasYT-Test/1.0"

    session = captured["requests_session"]
    assert session is not None

    import requests
    original_request = requests.Session.request
    observed = {}

    def fake_parent_request(self, method, url, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(
            headers={"Content-Length": "0"},
            url=url,
            status_code=200,
            content=b"",
        )

    monkeypatch.setattr(requests.Session, "request", fake_parent_request)
    session.request("GET", "https://music.youtube.com/", headers={"User-Agent": "default"})
    assert observed["headers"]["User-Agent"] == "MusicasYT-Test/1.0"
    monkeypatch.setattr(requests.Session, "request", original_request)

def test_ytmusic_client_without_cookies_keeps_unauthenticated_mode(monkeypatch):
    monkeypatch.delenv("MUSICASYT_COOKIES", raising=False)
    monkeypatch.delenv("MUSICASYT_USER_AGENT", raising=False)
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    captured = {}

    fake_module = types.ModuleType("ytmusicapi")
    helpers_module = types.ModuleType("ytmusicapi.helpers")
    helpers_module.initialize_headers = lambda: {
        "user-agent": "ytmusicapi-default-agent",
        "accept": "*/*",
        "accept-encoding": "gzip, deflate",
        "content-type": "application/json",
        "content-encoding": "gzip",
        "origin": "https://music.youtube.com",
    }

    def fake_ytmusic(**kwargs):
        captured.update(kwargs)
        return object()

    fake_module.YTMusic = fake_ytmusic
    monkeypatch.setitem(sys.modules, "ytmusicapi", fake_module)
    monkeypatch.setitem(sys.modules, "ytmusicapi.helpers", helpers_module)

    downloader.create_ytmusic_client()
    assert captured == {}

def test_debug_download_uses_verbose_streaming_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.delenv("MUSICASYT_COOKIES", raising=False)
    monkeypatch.delenv("MUSICASYT_USER_AGENT", raising=False)
    captured = {}

    def fake_stream(args, timeout):
        captured["args"] = args
        captured["timeout"] = timeout
        (tmp_path / "Song.mp3").write_bytes(b"mp3")
        return 0, "finished", False

    monkeypatch.setattr(downloader, "_run_ytdlp_debug", fake_stream)
    success, diagnostics = downloader.download_track("abcdefghijk", tmp_path, "Song.mp3", tmp_path)

    assert success is True
    assert diagnostics == "finished"
    assert "--verbose" in captured["args"]
    assert "--newline" in captured["args"]
    assert "--no-progress" not in captured["args"]
    assert captured["timeout"] == 900
