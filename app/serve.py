from __future__ import annotations

import os
import sys

import uvicorn

from app.logging_config import configure_logging, configured_log_level_name

def listener_settings() -> tuple[str, int]:
    host = os.environ.get("MUSICASYT_HOST", "0.0.0.0").strip() or "0.0.0.0"
    raw_port = os.environ.get("MUSICASYT_PORT", "8080").strip() or "8080"
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("MUSICASYT_PORT must be a number from 1 to 65535") from exc
    if not 1 <= port <= 65535:
        raise ValueError("MUSICASYT_PORT must be a number from 1 to 65535")
    return host, port

def main() -> None:
    configure_logging(stream=sys.stdout)
    host, port = listener_settings()
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        workers=1,
        log_level=configured_log_level_name().lower(),
        access_log=True,
    )

if __name__ == "__main__":
    main()
