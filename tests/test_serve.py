import pytest

from app import serve

def test_default_listener(monkeypatch):
    monkeypatch.delenv("MUSICASYT_HOST", raising=False)
    monkeypatch.delenv("MUSICASYT_PORT", raising=False)
    assert serve.listener_settings() == ("0.0.0.0", 8080)

def test_custom_listener(monkeypatch):
    monkeypatch.setenv("MUSICASYT_HOST", "127.0.0.1")
    monkeypatch.setenv("MUSICASYT_PORT", "8090")
    assert serve.listener_settings() == ("127.0.0.1", 8090)

def test_empty_listener_uses_defaults(monkeypatch):
    monkeypatch.setenv("MUSICASYT_HOST", "")
    monkeypatch.setenv("MUSICASYT_PORT", "")
    assert serve.listener_settings() == ("0.0.0.0", 8080)

@pytest.mark.parametrize("port", ["abc", "0", "65536", "-2", "22.5"])
def test_invalid_ports(monkeypatch, port):
    monkeypatch.setenv("MUSICASYT_PORT", port)
    with pytest.raises(ValueError, match="MUSICASYT_PORT"):
        serve.listener_settings()

def test_serve_starts_single_worker(monkeypatch):
    monkeypatch.setenv("MUSICASYT_HOST", "0.0.0.0")
    monkeypatch.setenv("MUSICASYT_PORT", "8090")
    called = []
    monkeypatch.setattr(serve.uvicorn, "run", lambda *a, **kw: called.append((a, kw)))
    serve.main()
    assert called == [( ("app.main:app",), {
        "host": "0.0.0.0", "port": 8090, "workers": 1,
        "log_level": "info", "access_log": True,
    })]
