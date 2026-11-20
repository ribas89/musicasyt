import json
from pathlib import Path

import pytest

from app.common import clean_name, playlist_id_from_url, safe_child, user_library, validate_username

def test_playlist_urls():
    assert playlist_id_from_url('https://music.youtube.com/playlist?list=PLabcdef1234') == 'PLabcdef1234'
    assert playlist_id_from_url('https://www.youtube.com/playlist?list=PLabcdef1234&foo=bar') == 'PLabcdef1234'
    for bad in ('http://music.youtube.com/playlist?list=PLabcdef1234',
                'https://evil.youtube.com/playlist?list=PLabcdef1234',
                'https://music.youtube.com/playlist?list=..',
                'https://music.youtube.com/playlist',
                'https://music.youtube.com/playlist?list=PLabcdef1234&list=PLxyz12345'):
        with pytest.raises(ValueError):
            playlist_id_from_url(bad)

def test_user_permissions(tmp_path):
    root = tmp_path / 'users'
    root.mkdir()
    (root / 'alice').mkdir()
    assert user_library(root, 'alice') == root / 'alice'
    for name in ('../bob', 'a/b', 'a\\b', '..', 'user space'):
        with pytest.raises(ValueError):
            validate_username(name)
    with pytest.raises(FileNotFoundError):
        user_library(root, 'other')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root / 'evil').symlink_to(outside, target_is_directory=True)
    with pytest.raises(FileNotFoundError):
        user_library(root, 'evil')
    with pytest.raises(PermissionError):
        safe_child(root / 'alice', '..')

def test_names():
    assert clean_name('..', 'fallback') == 'fallback'
    assert clean_name('AC/DC: Greatest Hits', 'fallback') == 'AC_DC_ Greatest Hits'
