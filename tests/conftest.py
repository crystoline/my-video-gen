"""Shared setup for the tests.

Everything runs in a temporary folder with fake versions of the paid services (Claude, ElevenLabs,
stock searches, YouTube), so the tests never read your API keys, never touch projects/, topics.txt
or .env, and never spend anything. Tests that need FFmpeg are skipped when it is not installed.

Run them with:  .venv/bin/python -m pytest
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="mvg-tests-"))

# Set before pipeline is imported. load_dotenv never overrides variables that already exist,
# so these win over anything in your .env.
TEST_ENV = {
    "PROJECTS_DIR": str(TMP / "projects"),
    "MUSIC_PATH": "", "MUSIC_VOLUME": "0.1", "MUSIC_DUCKING": "true", "VOICE_TONE": "soft", "CROSSFADE": "0.5",
    "VOICE_STABILITY": "0.5", "VOICE_SIMILARITY": "0.75", "VOICE_STYLE": "0", "VOICE_SPEAKER_BOOST": "true",
    "VOICE_SPEED": "1.0", "RENDER_JOBS": "", "SHOT_SECONDS": "", "AUTO_CHAPTERS": "false",
    "PEXELS_API_KEY": "", "PIXABAY_API_KEY": "", "UNSPLASH_ACCESS_KEY": "", "SMITHSONIAN_API_KEY": "",
    "VISUAL_PROVIDERS": "", "ALLOW_SHARE_ALIKE": "", "AUTO_PICK": "", "ANTHROPIC_VISION_MODEL": "",
    "FAL_KEY": "", "AI_VISUALS": "", "AI_IMAGE_MODEL": "", "AI_VIDEO_MODEL": "", "AI_IMAGE_COST": "", "AI_VIDEO_COST": "",
    "ANTHROPIC_API_KEY": "test-not-a-real-key", "ELEVENLABS_API_KEY": "test-not-a-real-key",
    "ELEVENLABS_VOICE_ID": "test-voice",
    "YT_CLIENT_SECRETS": str(TMP / "client_secret.json"), "YT_TOKEN": str(TMP / "youtube_token.json"),
}
os.environ.update(TEST_ENV)
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

import pipeline as pl  # noqa: E402
import webui as wu     # noqa: E402

_worker_started = False


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(TMP, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_settings():
    """Undo any setting a test changed (environment and pipeline globals)."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
    pl.configure()


@pytest.fixture()
def client(tmp_path):
    """Flask test client with the job worker running, and .env / topics.txt pointing at temporary copies."""
    global _worker_started
    if not _worker_started:
        threading.Thread(target=wu.worker, daemon=True, name="test-jobs").start()
        _worker_started = True
    old = wu.ENV_PATH, wu.TOPICS_PATH
    wu.ENV_PATH = tmp_path / ".env"
    shutil.copy(ROOT / ".env.example", wu.ENV_PATH)
    wu.TOPICS_PATH = tmp_path / "topics.txt"
    wu.TOPICS_PATH.write_text("# queue comment\nTopic A\nTopic B\n", encoding="utf-8")
    yield wu.app.test_client()
    wu.ENV_PATH, wu.TOPICS_PATH = old


@pytest.fixture()
def fakes(monkeypatch):
    """Fake every step that costs money. Returns a namespace that records calls."""
    from helpers import Fakes
    f = Fakes()
    monkeypatch.setattr(pl, "write_script", f.write_script)
    monkeypatch.setattr(pl, "fact_check", f.fact_check)
    monkeypatch.setattr(pl, "plan_visuals", f.plan_visuals)
    monkeypatch.setattr(pl, "find_visual", f.find_visual)
    monkeypatch.setattr(pl, "download", f.download)
    monkeypatch.setattr(pl, "tts", f.tts)
    monkeypatch.setattr(pl, "upload", f.upload)
    monkeypatch.setattr(pl, "upload_short", f.upload_short)
    return f
