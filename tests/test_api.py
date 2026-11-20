import concurrent.futures
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main

@pytest.fixture
def client(monkeypatch, tmp_path):
    users = tmp_path / 'users'
    users.mkdir()
    (users / 'alice').mkdir()
    (users / 'bob').mkdir()
    monkeypatch.setattr(main, 'USERS_ROOT', users)
    monkeypatch.setenv('MUSICASYT_API_KEY', 'unit-test-secret')
    monkeypatch.setattr(main.updater, 'is_ready', True)
    monkeypatch.setattr(main.updater, 'python_bin', Path('/bin/false'))
    monkeypatch.setattr(main._executor, 'submit', lambda fn, *args: None)
    main._jobs.clear()

    return TestClient(main.app, headers={'X-API-Key': 'unit-test-secret'})

def test_sync_deduplicates(client):
    payload = {'username': 'alice', 'playlist_url': 'https://music.youtube.com/playlist?list=PLabcdef1234'}
    a = client.post('/sync', json=payload)
    assert a.status_code == 202, a.text
    assert a.json()['job_id'] == 'alice:PLabcdef1234'
    assert a.json()['existing_job'] is False
    b = client.post('/sync', json=payload)
    assert b.status_code == 202
    assert b.json()['existing_job'] is True
    assert len(main._jobs) == 1
    c = client.get('/jobs/alice/PLabcdef1234')
    assert c.status_code == 200
    assert c.json()['status'] == 'queued'

def test_missing_job_and_not_ready(client, monkeypatch):
    assert client.get('/jobs/alice/PLabcdef1234').status_code == 404
    monkeypatch.setattr(main.updater, 'is_ready', False)
    r = client.post('/sync', json={'username': 'alice', 'playlist_url': 'https://music.youtube.com/playlist?list=PLabcdef1234'})
    assert r.status_code == 503

def test_restart_only_explicit(client):
    payload = {'username': 'alice', 'playlist_url': 'https://music.youtube.com/playlist?list=PLabcdef1234'}
    client.post('/sync', json=payload)
    main.update_job('alice:PLabcdef1234', {'status': 'completed', 'total': 2, 'downloaded': 2})
    assert client.post('/sync', json=payload).json()['existing_job'] is True
    payload['restart'] = True
    r = client.post('/sync', json=payload)
    assert r.status_code == 202
    assert r.json()['existing_job'] is False
    assert r.json()['status'] == 'queued'

def test_input_rejections(client):
    request = {'username': '../bob', 'playlist_url': 'https://music.youtube.com/playlist?list=PLabcdef1234'}
    assert client.post('/sync', json=request).status_code == 400
    request['username'] = 'does-not-exist'
    assert client.post('/sync', json=request).status_code == 400

def test_api_key_required_on_sync_and_jobs(client):
    payload = {'username': 'alice', 'playlist_url': 'https://music.youtube.com/playlist?list=PLabcdef1234'}
    assert client.post('/sync', json=payload, headers={'X-API-Key': ''}).status_code == 401
    assert client.post('/sync', json=payload, headers={'X-API-Key': 'wrong'}).status_code == 401
    assert client.get('/jobs/alice/PLabcdef1234', headers={'X-API-Key': ''}).status_code == 401
    assert client.get('/jobs/alice/PLabcdef1234', headers={'X-API-Key': 'wrong'}).status_code == 401

    assert not main._jobs

def test_status_public_and_job_auth_works(client):
    assert client.get('/status', headers={'X-API-Key': ''}).status_code == 200
    payload = {'username': 'alice', 'playlist_url': 'https://music.youtube.com/playlist?list=PLabcdef1234'}
    response = client.post('/sync', json=payload)
    assert response.status_code == 202
    assert client.get('/jobs/alice/PLabcdef1234').status_code == 200

def test_missing_key_config_fails_closed(client, monkeypatch):
    monkeypatch.delenv('MUSICASYT_API_KEY')
    payload = {'username': 'alice', 'playlist_url': 'https://music.youtube.com/playlist?list=PLabcdef1234'}
    assert client.post('/sync', json=payload).status_code == 503
    assert client.get('/jobs/alice/PLabcdef1234').status_code == 503
    assert not main._jobs

def test_playlist_delete_removes_folder_and_metadata(client, tmp_path):
    from app.metadata import patch, read
    user_dir = main.USERS_ROOT / 'alice'
    folder = user_dir / 'Synth coding'
    folder.mkdir()
    (folder / 'Song.mp3').write_bytes(b'music')
    patch(user_dir, 'PLabcdef1234', title='Synth coding', folder_name='Synth coding', status='completed')
    response = client.delete('/playlists/alice/PLabcdef1234')
    assert response.status_code == 200, response.text
    assert not folder.exists()
    assert 'PLabcdef1234' not in read(user_dir)['playlists']

def test_playlist_delete_rejects_active_job(client):
    payload = {'username': 'alice', 'playlist_url': 'https://music.youtube.com/playlist?list=PLabcdef1234'}
    assert client.post('/sync', json=payload).status_code == 202
    response = client.delete('/playlists/alice/PLabcdef1234')
    assert response.status_code == 409

def test_playlist_delete_rejects_path_escape(client):
    assert client.delete('/playlists/alice/../../bob').status_code in {400, 404}

def test_playlist_delete_requires_key(client):
    assert client.delete('/playlists/alice/PLabcdef1234', headers={'X-API-Key': 'wrong'}).status_code == 401

def test_snapshot_survives_job_memory_clear(client):
    from app.metadata import read
    payload = {'username': 'alice', 'playlist_url': 'https://music.youtube.com/playlist?list=PLabcdef1234'}
    assert client.post('/sync', json=payload).status_code == 202
    user_dir = main.USERS_ROOT / 'alice'
    assert read(user_dir)['playlists']['PLabcdef1234']['status'] == 'queued'
    main._jobs.clear()
    assert client.get('/jobs/alice/PLabcdef1234').status_code == 404
    assert read(user_dir)['playlists']['PLabcdef1234']['playlist_url'] == payload['playlist_url']


def test_run_job_forwards_downloader_stderr(monkeypatch):
    import sys

    job_id = "alice:PLabcdef1234"
    main._jobs.clear()
    main._jobs[job_id] = {
        "job_id": job_id,
        "username": "alice",
        "playlist_id": "PLabcdef1234",
        "status": "queued",
        "downloaded": 0,
        "existing": 0,
        "failed": 0,
        "unavailable": 0,
    }
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(main.updater, "python_bin", Path(sys.executable))
    captured = {}

    class Process:
        stdout = iter([
            '{"event":"done","status":"completed","downloaded":0,"existing":0,"failed":0,"unavailable":0}\n'
        ])

        def wait(self):
            return 0

    def fake_popen(command, **kwargs):
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr(main.subprocess, "Popen", fake_popen)
    main.run_job(job_id)

    assert captured["stderr"] is sys.stderr
    assert captured["env"]["LOG_LEVEL"] == "DEBUG"
    assert main._jobs[job_id]["status"] == "completed"
    main._jobs.clear()
