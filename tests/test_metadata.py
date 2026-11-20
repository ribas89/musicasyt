import json

import pytest

from app.metadata import read, patch, modify

def test_metadata_persists_and_updates_atomically(tmp_path):
    patch(tmp_path, 'PLabcdef1234', title='Synth coding', status='queued')
    patch(tmp_path, 'PLabcdef1234', total=5, downloaded=2, status='downloading')
    record = read(tmp_path)['playlists']['PLabcdef1234']
    assert record['title'] == 'Synth coding'
    assert record['total'] == 5
    assert record['downloaded'] == 2
    assert record['tracks'] == {}
    assert record['tracks_unavailable'] == {}
    assert record['tracks_error'] == {}
    assert not any(tmp_path.glob('.musicasyt-*.tmp'))

def test_symlink_metadata_rejected(tmp_path):
    outside = tmp_path.parent / 'metadata-outside-test.json'

    (tmp_path / 'musicasyt.json').symlink_to(outside)
    with pytest.raises(PermissionError):
        patch(tmp_path, 'PLabcdef1234', status='queued')

def test_invalid_metadata_never_overwritten(tmp_path):
    path = tmp_path / 'musicasyt.json'
    path.write_text('NOT JSON')
    with pytest.raises(ValueError):
        modify(tmp_path, lambda playlists: playlists.clear())
    assert path.read_text() == 'NOT JSON'


def test_register_track_moves_between_collections(tmp_path):
    from app.metadata import register_track

    register_track(tmp_path, "PLabcdef1234", "tracks_error", "abcde123", "Artist - Song.mp3")
    register_track(tmp_path, "PLabcdef1234", "tracks", "abcde123", "Artist - Song.mp3")

    record = read(tmp_path)["playlists"]["PLabcdef1234"]
    assert record["tracks"]["abcde123"] == "Artist - Song.mp3"
    assert "abcde123" not in record["tracks_error"]
    assert "abcde123" not in record["tracks_unavailable"]


def test_register_track_rejects_unknown_collection(tmp_path):
    from app.metadata import register_track

    with pytest.raises(ValueError):
        register_track(tmp_path, "PLabcdef1234", "other", "abcde123", "Artist - Song.mp3")
