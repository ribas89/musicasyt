import logging

import pytest

from app.logging_config import configured_log_level, configured_log_level_name

@pytest.mark.parametrize(
    ("value", "expected_name", "expected_level"),
    [
        ("DEBUG", "DEBUG", logging.DEBUG),
        ("info", "INFO", logging.INFO),
        ("WARNING", "WARNING", logging.WARNING),
        ("error", "ERROR", logging.ERROR),
        ("CRITICAL", "CRITICAL", logging.CRITICAL),
        ("invalid", "INFO", logging.INFO),
    ],
)
def test_log_level_environment(monkeypatch, value, expected_name, expected_level):
    monkeypatch.setenv("LOG_LEVEL", value)
    assert configured_log_level_name() == expected_name
    assert configured_log_level() == expected_level

def test_log_level_defaults_to_info(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    assert configured_log_level_name() == "INFO"
    assert configured_log_level() == logging.INFO

def test_ytmusicapi_debug_traces_redacted_http_metadata(monkeypatch, caplog):
    import sys
    import types

    import requests

    from app import downloader

    captured = {}

    class FakeYTMusic:
        def __init__(self, auth=None, requests_session=None):
            captured["auth"] = auth
            captured["session"] = requests_session

    class FakeResponse:
        status_code = 200
        url = "https://music.youtube.com/youtubei/v1/browse?key=secret-value"
        headers = {}
        content = b"ok"

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setitem(sys.modules, "ytmusicapi", types.SimpleNamespace(YTMusic=FakeYTMusic))
    monkeypatch.setattr(requests.Session, "request", lambda self, method, url, **kwargs: FakeResponse())
    caplog.set_level(logging.DEBUG)

    downloader.create_ytmusic_client()
    session = captured["session"]
    assert isinstance(session, requests.Session)
    session.get("https://music.youtube.com/youtubei/v1/browse?key=secret-value")

    assert "youtubei/v1/browse" in caplog.text
    assert "HTTP 200" in caplog.text
    assert "secret-value" not in caplog.text

def test_ytdlp_debug_output_is_forwarded_to_logger(caplog):
    import sys

    from app.downloader import _run_ytdlp_debug

    caplog.set_level(logging.DEBUG)
    return_code, tail, timed_out = _run_ytdlp_debug(
        [sys.executable, "-c", "print('yt-dlp diagnostic line')"],
        timeout=5,
    )

    assert return_code == 0
    assert timed_out is False
    assert "yt-dlp diagnostic line" in tail
    assert "[yt-dlp] yt-dlp diagnostic line" in caplog.text


def test_ytdlp_debug_lines_redact_authentication_values():
    from app.downloader import _redact_ytdlp_log_line

    assert _redact_ytdlp_log_line("Cookie: SID=secret") == "Cookie: [REDACTED]"
    assert _redact_ytdlp_log_line("Authorization: SAPISIDHASH secret") == "Authorization: [REDACTED]"
    assert _redact_ytdlp_log_line("SAPISIDHASH secret-value") == "SAPISIDHASH [REDACTED]"


def test_download_track_uses_verbose_ytdlp_when_debug(monkeypatch, tmp_path):
    from app import downloader

    folder = tmp_path / "playlist"
    data_dir = tmp_path / "data"
    folder.mkdir()
    data_dir.mkdir()
    captured = {}

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(downloader, "cookie_file_from_env", lambda: None)
    monkeypatch.setattr(downloader, "user_agent_from_env", lambda: None)

    def fake_run(args, timeout):
        captured["args"] = args
        (folder / "track.mp3").write_bytes(b"audio")
        return 0, "[debug] simulated verbose output", False

    monkeypatch.setattr(downloader, "_run_ytdlp_debug", fake_run)

    success, diagnostics = downloader.download_track(
        "video12345",
        folder,
        "track.mp3",
        data_dir,
    )

    assert success is True
    assert "--verbose" in captured["args"]
    assert "--newline" in captured["args"]
    assert "--no-progress" not in captured["args"]
    assert "simulated verbose output" in diagnostics
