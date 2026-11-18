"""Channels: the writer, fact-checker, voice, music and YouTube upload follow the channel a video belongs to."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import pipeline as pl
from helpers import H, needs_ffmpeg, new_project, project, run_job, wait

HISTORY = {"id": "new-1", "name": "History Bites", "about": "world history and the people who shaped it",
           "voice": "Vivid storytelling.", "accuracy": "Prefer peer-reviewed historians and primary documents.",
           "visuals": "Prefer archive photos and paintings of the period.", "category": "27", "language": "es",
           "voice_id": "VoiceHistory123", "music": "none", "minutes": 8}


@pytest.fixture(autouse=True)
def no_channels_file():
    pl.channels_path().unlink(missing_ok=True)
    yield
    pl.channels_path().unlink(missing_ok=True)


def save_history():
    pl.save_channels({"default": "science", "channels": [dict(pl.DEFAULT_CHANNEL),
                                                         pl.clean_channel(dict(HISTORY, id=None))]})


def test_the_built_in_science_channel_keeps_todays_prompts():
    data = pl.load_channels()
    assert data["default"] == "science" and [c["id"] for c in data["channels"]] == ["science"]
    system = pl.writer_system()
    assert "science, planet Earth and astronomy" in system and "NASA, ESA, USGS, NOAA" in system
    assert "narration in English" in system
    assert "NASA, ESA, USGS, NOAA" in pl.factcheck_system()


def test_a_channel_shapes_the_writer_and_fact_checker(monkeypatch):
    save_history()
    ch = pl.get_channel("history-bites")
    assert ch["name"] == "History Bites" and ch["category"] == "27"
    seen = {}

    def fake(model, system, prompt, schema, max_tokens=16000):
        seen.update(system=system, prompt=prompt)
        return schema(title="T", description="D", tags=["a"], thumbnail_text="X",
                      scenes=[{"narration": "Hola.", "visual": {"source": "archive", "query": "roman forum"}}])

    monkeypatch.setattr(pl, "claude_json", fake)
    script = pl.write_script("The fall of Rome", 3, [], channel=ch)
    assert script["scenes"][0]["visual"]["source"] == "archive"
    assert "world history and the people who shaped it" in seen["system"] and "Vivid storytelling." in seen["system"]
    assert "narration in Spanish" in seen["system"] and "primary documents" in seen["system"]
    assert "Prefer archive photos and paintings of the period." in seen["prompt"]
    assert "world history" in pl.factcheck_system(ch)


def test_channels_are_edited_in_settings(client):
    r = client.get("/api/channels").get_json()
    assert [c["id"] for c in r["channels"]] == ["science"] and r["categories"]["27"] == "Education"
    science = r["channels"][0]
    r = client.put("/api/channels", headers=H, json={"default": "new-1", "channels": [science, HISTORY]})
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert [c["id"] for c in data["channels"]] == ["science", "history-bites"] and data["default"] == "history-bites"
    assert client.get("/api/status").get_json()["channels"]["default"] == "history-bites"
    for bad in ({"channels": []}, {"channels": [science, dict(science)]},
                {"channels": [dict(science, category="99")]}, {"channels": [dict(science, voice_id="bad id!")]},
                {"channels": [dict(science, about="")]}, {"channels": [dict(science, minutes=90)]}):
        assert client.put("/api/channels", headers=H, json=bad).status_code == 400, bad


def test_new_videos_belong_to_a_channel_and_avoid_its_own_titles(client, fakes):
    save_history()
    r = client.post("/api/projects", headers=H, json={"topic": "The fall of Rome", "channel": "history-bites"})
    assert r.status_code == 200, r.get_json()
    wait(client, r.get_json()["job"]["id"])
    p = project(client, r.get_json()["slug"])
    assert p["channel"] == {"id": "history-bites", "name": "History Bites"} and p["language"] == "es"
    assert fakes.channels_used[-1] == "history-bites"
    assert client.post("/api/projects", headers=H, json={"topic": "X", "channel": "nope"}).status_code == 400
    slug, P = new_project(client)   # default channel
    assert project(client, slug)["channel"]["id"] == "science"
    titles = pl.previous_titles(P, channel_id="history-bites")
    assert titles and all("Rome" in t for t in titles)
    r = client.put(f"/api/projects/{slug}/channel", headers=H, json={"channel": "history-bites"})
    assert r.get_json()["channel"]["id"] == "history-bites"


@needs_ffmpeg
def test_a_channels_voice_is_used_for_its_narration(client, fakes):
    save_history()
    r = client.post("/api/projects", headers=H, json={"topic": "Carthage", "channel": "history-bites"})
    slug = r.get_json()["slug"]
    wait(client, r.get_json()["job"]["id"])
    run_job(client, slug, action="voiceover")
    assert set(fakes.voices) == {"VoiceHistory123"}


def test_a_channels_music_and_youtube_settings(monkeypatch, tmp_path):
    save_history()
    lib = tmp_path / "music"
    lib.mkdir()
    (lib / "calm.mp3").write_bytes(b"x")
    monkeypatch.setattr(pl, "MUSIC_PATH", lib / "calm.mp3")
    monkeypatch.setattr(pl, "PROJECTS_DIR", tmp_path / "projects")
    proj = tmp_path / "projects" / "rome"
    pl.save_meta(proj, {"channel": "history-bites"})
    assert pl.project_music(proj) is None                     # the channel says no music
    pl.save_meta(tmp_path / "projects" / "mars", {})
    assert pl.project_music(tmp_path / "projects" / "mars") == lib / "calm.mp3"

    sent = {}

    class Request:
        def next_chunk(self):
            return None, {"id": "V1"}

    class Videos:
        def insert(self, part, body, media_body):
            sent["video"] = body
            return Request()

    class Captions:
        def insert(self, part, body, media_body):
            sent["captions"] = body
            return SimpleNamespace(execute=lambda: None)

    youtube = SimpleNamespace(videos=lambda: Videos(), captions=lambda: Captions(),
                              thumbnails=lambda: SimpleNamespace(set=lambda **kw: SimpleNamespace(execute=lambda: None)))
    monkeypatch.setattr(pl, "get_youtube", lambda interactive: youtube)
    for name in ("final.mp4", "thumbnail.jpg", "captions.srt"):
        (proj / name).write_bytes(b"x")
    args = SimpleNamespace(yes=True, privacy="private", publish_at=None, synthetic=False)
    pl.upload(proj, {"title": "Roma", "description": "D", "tags": [], "scenes": []}, [], args)
    snippet = sent["video"]["snippet"]
    assert snippet["categoryId"] == "27" and snippet["defaultLanguage"] == "es" and snippet["defaultAudioLanguage"] == "es"
    assert sent["captions"]["snippet"]["language"] == "es" and sent["captions"]["snippet"]["name"] == "Spanish"
