import logging

from app import downloader
from app.metadata import read


class FakeClient:
    def get_playlist(self, playlist_id, limit=None):
        return {
            "title": "Test Playlist",
            "tracks": [
                {"videoId": "abcde123", "title": "Success", "artists": [{"name": "Artist"}]},
                {"title": "No Media ID", "artists": [{"name": "Missing"}]},
                {"videoId": "pgiWm5jaaa", "isAvailable": False, "title": "Unavailable", "artists": [{"name": "packattack04082"}]},
                {
                    "videoId": None,
                    "isAvailable": False,
                    "title": "Thumbnail fallback",
                    "artists": [{"name": "Thumbnail Artist"}],
                    "thumbnails": [{"url": "https://i.ytimg.com/vi/vT3AaQ77ges/hqdefault.jpg"}],
                },
                {"videoId": "error123", "title": "Fails", "artists": [{"name": "Artist"}]},
            ],
        }


def test_playlist_tracks_are_registered_by_video_id(tmp_path, monkeypatch, caplog):
    users_root = tmp_path / "users"
    (users_root / "alice").mkdir(parents=True)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    monkeypatch.setattr(downloader, "cookie_file_from_env", lambda: None)
    monkeypatch.setattr(downloader, "create_ytmusic_client", lambda: FakeClient())
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    download_ids = []

    def fake_download(video_id, folder, file_name, data_dir):
        download_ids.append(video_id)
        if video_id == "error123":
            return False, "simulated download error"
        return True, ""

    monkeypatch.setattr(downloader, "download_track", fake_download)

    with caplog.at_level(logging.DEBUG, logger="musicasyt.downloader"):
        downloader.run_playlist_download("alice", "PLabcdef1234", users_root, data_dir)

    messages = "\n".join(caplog.messages)
    assert "[ytmusicapi] get_playlist response:" in messages
    assert '"videoId": "pgiWm5jaaa"' in messages
    assert '"title": "Unavailable"' in messages

    record = read(users_root / "alice")["playlists"]["PLabcdef1234"]
    assert record["tracks"]["abcde123"] == "Artist - Success.mp3"
    assert record["tracks_unavailable"]["pgiWm5jaaa"] == "packattack04082 - Unavailable.mp3"
    assert record["tracks_unavailable"]["vT3AaQ77ges"] == "Thumbnail Artist - Thumbnail fallback.mp3"
    assert record["tracks_unavailable"]["null-index-2"] == "Missing - No Media ID.mp3"
    assert record["tracks_error"]["error123"] == "Artist - Fails.mp3"
    assert "index:2" not in record["tracks_unavailable"]
    assert record["downloaded"] == 1
    assert record["unavailable"] == 3
    assert record["failed"] == 1
    assert download_ids == ["abcde123", "error123"]


def test_track_video_id_supports_common_field_shapes():
    assert downloader.track_video_id({"videoId": "abcde123"}, 1) == "abcde123"
    assert downloader.track_video_id({"video_id": "pgiWm5jaaa"}, 1) == "pgiWm5jaaa"
    assert downloader.track_video_id({"watchEndpoint": {"videoId": "watch123"}}, 1) == "watch123"
    assert downloader.track_video_id({"navigationEndpoint": {"watchEndpoint": {"videoId": "nav123"}}}, 1) == "nav123"


def test_track_video_id_falls_back_to_thumbnail_url():
    track = {
        "videoId": None,
        "thumbnails": [
            {"url": "https://i.ytimg.com/vi/vT3AaQ77ges/hqdefault.jpg"}
        ],
    }
    assert downloader.track_video_id(track, 22) == "vT3AaQ77ges"


def test_video_id_field_takes_priority_over_thumbnail_url():
    track = {
        "videoId": "direct123",
        "thumbnails": [
            {"url": "https://i.ytimg.com/vi/thumbnail123/hqdefault.jpg"}
        ],
    }
    assert downloader.track_video_id(track, 22) == "direct123"


def test_track_video_id_supports_webp_thumbnail_url():
    track = {
        "videoId": None,
        "thumbnails": [
            {"url": "https://i.ytimg.com/vi_webp/vT3AaQ77ges/maxresdefault.webp"}
        ],
    }
    assert downloader.track_video_id(track, 22) == "vT3AaQ77ges"


def test_track_video_id_returns_null_index_when_no_id_exists():
    assert downloader.track_video_id({"title": "No ID"}, 22) == "null-index-22"
    assert downloader.track_video_id({"thumbnails": [{"url": "https://example.com/image.jpg"}]}, 22) == "null-index-22"


def test_get_playlist_response_is_logged_only_at_debug(monkeypatch, caplog):
    import logging

    response = {
        "title": "Test Playlist",
        "tracks": [{"videoId": "pgiWm5jaaa", "title": "Example Track"}],
    }

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    with caplog.at_level(logging.DEBUG, logger="musicasyt.downloader"):
        downloader.log_ytmusicapi_playlist_response(response)

    assert "[ytmusicapi] get_playlist response:" in caplog.text
    assert '"videoId": "pgiWm5jaaa"' in caplog.text
    assert '"title": "Example Track"' in caplog.text

    caplog.clear()
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    with caplog.at_level(logging.DEBUG, logger="musicasyt.downloader"):
        downloader.log_ytmusicapi_playlist_response(response)

    assert "get_playlist response" not in caplog.text
