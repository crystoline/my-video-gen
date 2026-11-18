"""Helpers shared by the tests: fakes for the paid services and small API shortcuts."""
from __future__ import annotations

import io
import itertools
import shutil
import time
from pathlib import Path

import pytest
from PIL import Image

import pipeline as pl

H = {"X-Requested-By": "myvideogen"}
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="FFmpeg is not installed")
_topics = itertools.count(1)


def png_bytes(color=(40, 80, 160), size=(1600, 900)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class Fakes:
    """Stand-ins for Claude, ElevenLabs, the stock searches, downloads and the YouTube upload."""

    def __init__(self):
        self.visuals = 0
        self.tts_calls: list[str] = []
        self.uploads: list = []
        self.factcheck_delay = 0.0
        self.scene_count = 4

    def write_script(self, topic, minutes, previous):
        return {"title": f"Title for {topic}", "description": "Desc", "tags": ["a", "b"],
                "thumbnail_text": "BIG TEXT", "topic": topic,
                "scenes": [{"narration": f"This is narration number {k} for the test video.",
                            "visual": {"source": "nasa", "query": f"query {k}"}} for k in range(self.scene_count)]}

    def fact_check(self, script, project_dir):
        time.sleep(self.factcheck_delay)
        (project_dir / "factcheck_notes.md").write_text("Full notes", encoding="utf-8")
        return {"issues": [{"scene": 2, "problem": "Wrong number", "fixed_narration": "Corrected scene two.",
                            "source": "https://example.org"}], "claims_to_verify": ["Check this"], "searches": 3}

    def find_visual(self, scene, used, min_seconds):
        self.visuals += 1
        return {"kind": "image", "url": f"https://example.org/img{self.visuals}.png", "id": f"fake:{self.visuals}",
                "credit": f"Fake credit {self.visuals}"}

    def download(self, url, dest):
        shade = (self.visuals * 37) % 255
        dest.write_bytes(png_bytes((shade, 100, 255 - shade)))

    def tts(self, text, mp3, align_path, prev_text="", next_text=""):
        """A sine tone as long as the text would take to say, with evenly spread character timing."""
        self.tts_calls.append(mp3.name)
        dur = 0.8 + 0.035 * len(text)
        pl.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=330:duration={dur:.2f}",
                "-ar", "44100", "-ac", "1", mp3])
        step = dur / len(text)
        pl.save_json(align_path, {"characters": list(text), "start": [i * step for i in range(len(text))],
                                  "end": [(i + 1) * step for i in range(len(text))]})

    def upload(self, project, script, assets, args):
        self.uploads.append(args)
        return "VID123"

    def upload_short(self, project, script, assets, args):
        self.uploads.append(("short", args))
        return "SHORT1"


def wait(client, job_id: str, want: str = "done", timeout: float = 900) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{job_id}?since=0").get_json()
        if j["status"] in ("done", "failed"):
            assert j["status"] == want, f"job {j['label']} ended {j['status']}: {j['error']}\n" + "\n".join(j["lines"])
            return j
        time.sleep(0.05)
    raise AssertionError("job timed out")


def job(client, slug: str, **body) -> str:
    r = client.post(f"/api/projects/{slug}/jobs", json=body, headers=H)
    assert r.status_code == 200, r.get_json()
    return r.get_json()["id"]


def run_job(client, slug: str, want: str = "done", **body) -> dict:
    return wait(client, job(client, slug, **body), want)


def project(client, slug: str) -> dict:
    r = client.get(f"/api/projects/{slug}")
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def new_project(client, topic: str | None = None) -> tuple[str, Path]:
    """Create a video through the API (with the fake writer and fact-checker) and wait for its script."""
    topic = topic or f"Test topic {next(_topics)}"
    r = client.post("/api/projects", json={"topic": topic, "minutes": 3}, headers=H)
    assert r.status_code == 200, r.get_json()
    slug = r.get_json()["slug"]
    wait(client, r.get_json()["job"]["id"])
    return slug, pl.PROJECTS_DIR / slug
