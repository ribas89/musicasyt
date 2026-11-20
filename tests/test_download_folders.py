import json

from app.downloader import directory_for_playlist, existing_mp3, resolve_track_file
from app.metadata import read

def test_title_rename_keeps_original_folder(tmp_path):
    a = directory_for_playlist(tmp_path, '80s Coding', 'PLabcdef1234')
    assert a.name == '80s Coding'
    b = directory_for_playlist(tmp_path, 'Renamed Channel', 'PLabcdef1234')
    assert b == a
    assert read(tmp_path)['playlists']['PLabcdef1234']['folder_name'] == a.name
    assert not (a / '.musicasyt.json').exists()

def test_same_title_different_playlist(tmp_path):
    a = directory_for_playlist(tmp_path, 'Favorites', 'PLabcdef1234')
    b = directory_for_playlist(tmp_path, 'Favorites', 'PLfedcba5678')
    assert a != b
    assert b.name == 'Favorites [PLfedcba5678]'

def test_old_ytmd_names_and_new_names(tmp_path):
    assert not existing_mp3(tmp_path, 'Artist - Track', 'abcdef1234')
    (tmp_path / 'Artist - Track.mp3').touch()
    assert existing_mp3(tmp_path, 'Artist - Track', 'abcdef1234')

def test_legacy_id_name_is_migrated_to_clean_name(tmp_path):
    (tmp_path / 'Artist - Track [abcDE123].mp3').write_bytes(b'music')
    name, exists = resolve_track_file(tmp_path, 'Artist - Track', 'abcDE123', {})
    assert (name, exists) == ('Artist - Track.mp3', True)
    assert (tmp_path / name).read_bytes() == b'music'
    assert not (tmp_path / 'Artist - Track [abcDE123].mp3').exists()

def test_duplicate_track_titles_get_unique_clean_filenames(tmp_path):
    (tmp_path / 'Artist - Track.mp3').write_bytes(b'old')
    name, exists = resolve_track_file(tmp_path, 'Artist - Track', 'differentVid', {'firstVid': 'Artist - Track.mp3'})
    assert (name, exists) == ('Artist - Track (2).mp3', False)
    assert '[' not in name

def test_old_folder_marker_can_be_migrated(tmp_path):
    folder = tmp_path / 'Old Folder'
    folder.mkdir()
    (folder / '.musicasyt.json').write_text(json.dumps({'playlist_id': 'PLabcdef1234', 'title': 'Old Folder'}))
    assert directory_for_playlist(tmp_path, 'New title', 'PLabcdef1234') == folder
    assert read(tmp_path)['playlists']['PLabcdef1234']['folder_name'] == 'Old Folder'
    assert not (folder / '.musicasyt.json').exists()

def test_legacy_marker_with_other_id_is_not_adopted(tmp_path):
    folder = tmp_path / 'Favorites'
    folder.mkdir()
    (folder / '.musicasyt.json').write_text(json.dumps({'playlist_id': 'PLother12345'}))
    new_folder = directory_for_playlist(tmp_path, 'Favorites', 'PLabcdef1234')
    assert new_folder != folder
    assert new_folder.name == 'Favorites [PLabcdef1234]'

def test_full_sync_writes_snapshots_and_reuses_tracks(tmp_path, monkeypatch):
    import sys
    import types
    from app import downloader
    playlist_id = 'PLabcdef1234'
    userdir = tmp_path / 'users' / 'alice'
    userdir.mkdir(parents=True)
    data = tmp_path / 'data'
    data.mkdir()
    content = {'title': 'Synth coding', 'tracks': [
        {'title': 'Make This Right', 'artists': [{'name': 'The Toxic Avenger'}], 'videoId': 'M-LMYdj-wqo'}
    ]}
    monkeypatch.setitem(sys.modules, 'ytmusicapi', types.SimpleNamespace(YTMusic=lambda: types.SimpleNamespace(get_playlist=lambda pid, limit: content)))
    downloaded_names = []
    def fake_download(videoid, folder, name, data_dir):
        downloaded_names.append(name)
        (folder / name).write_bytes(b'MP3')
        return True, ''
    monkeypatch.setattr(downloader, 'download_track', fake_download)
    downloader.run_playlist_download('alice', playlist_id, tmp_path / 'users', data)
    entry = read(userdir)['playlists'][playlist_id]
    assert entry['title'] == 'Synth coding'
    assert entry['status'] == 'completed'
    assert entry['downloaded'] == 1
    assert entry['remaining'] == 0
    assert entry['last_synced_at']
    assert downloaded_names == ['The Toxic Avenger - Make This Right.mp3']
    assert entry['tracks'] == {'M-LMYdj-wqo': downloaded_names[0]}
    downloader.run_playlist_download('alice', playlist_id, tmp_path / 'users', data)
    updated = read(userdir)['playlists'][playlist_id]
    assert updated['downloaded'] == 0
    assert updated['existing'] == 1
    assert len(downloaded_names) == 1
