from __future__ import annotations

import json
import logging
import shlex
import shutil
import subprocess
import sys
import time
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("musicasyt.updater")

UPSTREAMS = {
    "ytmusicapi": ("https://github.com/sigma67/ytmusicapi.git", "main"),
    "yt-dlp": ("https://github.com/yt-dlp/yt-dlp.git", "master"),
}

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def atomic_json(path: Path, payload: dict) -> None:
    temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)

class PackageUpdater:
    def __init__(self, data_root: Path):
        self.data_root = data_root
        self._lock = threading.RLock()
        self.state = "starting"
        self.message = "Preparing downloader environment"
        self.error: str | None = None
        self.commit_ids: dict[str, str] = {}
        self.last_checked: str | None = None
        self.using_cached = False
        self.python_bin: Path | None = None
        self.is_ready = False

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "ready": self.is_ready,
                "state": self.state,
                "message": self.message,
                "commits": dict(self.commit_ids),
                "last_checked": self.last_checked,
                "using_cached": self.using_cached,
                "error": self.error,
            }

    def _set(self, *, state: str, message: str, error: str | None = None) -> None:
        with self._lock:
            self.state = state
            self.message = message
            self.error = error
        if state == "error":
            logger.error("Dependency updater state=%s: %s", state, message)
        else:
            logger.info("Dependency updater state=%s: %s", state, message)
        if error:
            logger.error("Dependency updater details: %s", error)

    def _existing(self):
        manifest_path = self.data_root / "current.json"
        if not manifest_path.is_file():
            return None
        try:
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            name = manifest_data["environment"]

            if not isinstance(name, str) or not name.startswith("env-") or "/" in name or "\\" in name:
                return None
            binary = self.data_root / "venvs" / name / "bin" / "python"
            if binary.exists() and not binary.is_symlink():
                return manifest_data, binary
        except (ValueError, KeyError, OSError, TypeError):
            return None
        return None

    @staticmethod
    def _run(argv: list[str], timeout: int) -> str:
        started = time.monotonic()
        logger.debug("Running setup command: %s", shlex.join(argv))
        try:
            process_result = subprocess.run(
                argv, check=True, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.CalledProcessError as error:
            logger.error(
                "Setup command failed (exit=%s, elapsed=%.1fs): %s\n%s",
                error.returncode,
                time.monotonic() - started,
                shlex.join(argv),
                (error.stderr or error.stdout or "")[-5000:],
            )
            raise
        except Exception:
            logger.exception("Setup command raised an exception: %s", shlex.join(argv))
            raise

        logger.debug(
            "Setup command completed (elapsed=%.1fs): %s",
            time.monotonic() - started,
            shlex.join(argv),
        )
        if process_result.stdout.strip():
            logger.debug("Command stdout:\n%s", process_result.stdout.strip()[-5000:])
        if process_result.stderr.strip():
            logger.debug("Command stderr:\n%s", process_result.stderr.strip()[-5000:])
        return process_result.stdout.strip()

    def _get_latest(self) -> dict[str, str]:
        latest_commits = {}
        for package, (repo, branch) in UPSTREAMS.items():
            remote_output = self._run(
                ["git", "ls-remote", "--exit-code", repo, f"refs/heads/{branch}"],
                timeout=35,
            )
            commit_id = remote_output.split()[0] if remote_output else ""
            if len(commit_id) not in (40, 64) or any(
                character not in "0123456789abcdef" for character in commit_id.lower()
            ):
                raise RuntimeError(f"Cannot identify upstream commit for {package}")
            latest_commits[package] = commit_id
        return latest_commits

    def _build(self, commits: dict[str, str]) -> tuple[str, Path]:
        env_name = f"env-{uuid.uuid4().hex[:12]}"
        directory = self.data_root / "venvs" / env_name
        directory.parent.mkdir(parents=True, exist_ok=True)
        python_bin = directory / "bin" / "python"
        try:
            self._run([sys.executable, "-m", "venv", str(directory)], timeout=60)
            self._run(
                [
                    str(python_bin),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-cache-dir",
                    f"git+{UPSTREAMS['ytmusicapi'][0]}@{commits['ytmusicapi']}",
                    f"yt-dlp[default] @ git+{UPSTREAMS['yt-dlp'][0]}@{commits['yt-dlp']}",
                ],
                timeout=900,
            )
            self._run([
                str(python_bin), "-c",
                "from ytmusicapi import YTMusic; import yt_dlp; import importlib.metadata as m; "
                "print(m.version('yt-dlp-ejs'))",
            ], timeout=30)
            self._run([str(python_bin), "-m", "yt_dlp", "--version"], timeout=30)
            return env_name, python_bin
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def _prune(self, keep: set[str]):
        root = self.data_root / "venvs"
        for folder in root.glob("env-*"):
            if folder.is_dir() and not folder.is_symlink() and folder.name not in keep:
                shutil.rmtree(folder, ignore_errors=True)

    def boot(self):
        logger.info("Starting dependency updater; data directory=%s", self.data_root)
        self.data_root.mkdir(parents=True, exist_ok=True)
        existing = self._existing()
        try:
            self._set(state="checking_updates", message="Checking upstream Git commits")
            latest = self._get_latest()
            with self._lock:
                self.last_checked = utc_now()
            if existing and existing[0].get("commits") == latest:
                self._set(state="ready", message="Downloader is up to date")
                self._activate(existing[1], latest, cached=False)
                return
            self._set(state="building", message="Installing changed upstream dependencies")
            name, python_bin = self._build(latest)
            manifest = {"environment": name, "commits": latest, "installed_at": utc_now()}
            atomic_json(self.data_root / "current.json", manifest)
            keep = {name}
            if existing:
                keep.add(existing[0]["environment"])
            self._prune(keep)
            self._set(state="ready", message="Downloader ready")
            self._activate(python_bin, latest, cached=False)
        except Exception as error:
            error_message = str(error).strip()[:500]
            if existing:
                self._set(
                    state="ready",
                    message="Running last working dependencies (upstream check/update failed)",
                    error=error_message,
                )
                self._activate(existing[1], existing[0].get("commits", {}), cached=True)
            else:
                self._set(state="error", message="Could not install downloader dependencies", error=error_message)

    def _activate(self, executable: Path, commits: dict[str, str], cached: bool):
        with self._lock:
            self.python_bin = executable
            self.commit_ids = dict(commits)
            self.using_cached = cached
            self.is_ready = True
        logger.info(
            "Downloader dependencies ready: python=%s cached=%s commits=%s",
            executable,
            cached,
            {name: commit[:12] for name, commit in commits.items()},
        )
