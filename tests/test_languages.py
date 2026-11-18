"""Other languages: translated captions, titles and descriptions, and dubbed versions (Claude and YouTube faked)."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

import pipeline as pl
from helpers import H, new_project, project, run_job

SCRIPT = {"title": "Why is Mars red?", "description": "Rust.", "tags": ["mars"], "thumbnail_text": "RED PLANET",
          "scenes": [{"narration": "Mars is red.", "visual": {"source": "nasa", "query": "mars"}},
                     {"narration": "Its dust is rusty iron.", "visual": {"source": "nasa", "query": "mars dust"}}]}
TIMELINE = [{"duration": 3.0, "speech": 2.5, "frames": 90}, {"duration": 4.0, "speech": 3.5, "frames": 120}]


@pytest.fixture()
def claude(monkeypatch):
    calls = []

    def fake(model, system, prompt, schema, max_tokens=16000):
        calls.append(prompt)
        n = int(re.search(r"Narration, (\d+) scenes", prompt).group(1))
        scenes = (["Marte es rojo.", "Su polvo es hierro oxidado."] + [f"Escena {k}." for k in range(2, n)])[:n]
        return schema(title="¿Por qué Marte es rojo?", description="Óxido.", tags=["marte"], thumbnail_text="PLANETA ROJO",
                      scenes=scenes)

    monkeypatch.setattr(pl, "claude_json", fake)
    return calls


def test_translations_are_cached_until_the_script_changes(tmp_path, claude):
    tr = pl.translate_script(tmp_path, SCRIPT, "es")
    assert tr["scenes"] == ["Marte es rojo.", "Su polvo es hierro oxidado."] and tr["title"].startswith("¿Por qué")
    assert "into Spanish" in claude[0] and "natural spoken Spanish" in claude[0]
    pl.translate_script(tmp_path, SCRIPT, "es")
    assert len(claude) == 1                                           # reused
    changed = dict(SCRIPT, title="Why is Mars so red?")
    pl.translate_script(tmp_path, changed, "es")
    assert len(claude) == 2                                           # translated again
    with pytest.raises(ValueError):
        pl.translate_script(tmp_path, SCRIPT, "xx")


def test_translated_captions_follow_each_scenes_timing(tmp_path, claude):
    srt = pl.write_translated_captions(tmp_path, SCRIPT, TIMELINE, "es")
    text = srt.read_text(encoding="utf-8")
    assert srt.name == "captions_es.srt" and "Marte es rojo." in text and "Su polvo es hierro oxidado." in text
    starts = [line.split(" --> ")[0] for line in text.splitlines() if " --> " in line]
    assert starts[0] == "00:00:00,000" and starts[1] == "00:00:03,000"   # scene 2 starts when its narration does


def test_translations_are_added_to_the_uploaded_video(tmp_path, claude, monkeypatch):
    pl.save_json(tmp_path / "youtube.json", {"video_id": "V1", "url": "https://youtu.be/V1"})
    pl.save_json(tmp_path / "timeline.json", TIMELINE)
    sent = {"captions": [], "update": None}

    class Captions:
        def insert(self, part, body, media_body):
            sent["captions"].append(body["snippet"])
            return SimpleNamespace(execute=lambda: None)

    class Videos:
        def update(self, part, body):
            sent["update"] = (part, body)
            return SimpleNamespace(execute=lambda: None)

    monkeypatch.setattr(pl, "get_youtube", lambda interactive: SimpleNamespace(captions=lambda: Captions(),
                                                                             videos=lambda: Videos()))
    done = pl.upload_translations(tmp_path, SCRIPT, [{"credit": "NASA image"}], ["es", "en"])
    assert [c["language"] for c in sent["captions"]] == ["es"]        # the video's own language is skipped
    part, body = sent["update"]
    assert part == "localizations" and body["id"] == "V1"
    assert body["localizations"]["es"]["title"] == "¿Por qué Marte es rojo?"
    assert body["localizations"]["es"]["description"].startswith("Óxido.") and "NASA image" in body["localizations"]["es"]["description"]
    assert done == {"es": "captions+title"} and pl.load_json(tmp_path / "youtube.json")["translations"] == done


def test_a_dubbed_version_is_a_new_video_with_the_same_visuals(tmp_path, claude, monkeypatch):
    monkeypatch.setattr(pl, "PROJECTS_DIR", tmp_path)
    src = tmp_path / "why-mars"
    (src / "assets").mkdir(parents=True)
    (src / "assets" / "scene_00.jpg").write_bytes(b"img")
    pl.save_json(src / "script.json", dict(SCRIPT, chapters=[{"scene": 0, "title": "Intro"}]))
    pl.save_json(src / "assets.json", [{"kind": "image", "id": "nasa:1", "credit": "NASA", "path": "/x/scene_00.jpg"}])
    pl.save_meta(src, {"topic": "Mars", "channel": "science", "music": "calm.mp3"})
    dest = pl.make_dub(src, "es")
    assert dest.name == "why-mars-es" and (dest / "assets" / "scene_00.jpg").read_bytes() == b"img"
    new = pl.load_json(dest / "script.json")
    assert [s["narration"] for s in new["scenes"]] == ["Marte es rojo.", "Su polvo es hierro oxidado."]
    assert new["scenes"][0]["visual"] == SCRIPT["scenes"][0]["visual"] and "chapters" not in new
    assert pl.load_manifest(dest)[0]["path"] == str(dest / "assets" / "scene_00.jpg")
    meta = pl.load_meta(dest)
    assert meta["language"] == "es" and meta["translation_of"] == "why-mars" and meta["music"] == "calm.mp3"
    assert pl.video_language(dest) == "es" and (dest / "review.md").exists()
    with pytest.raises(RuntimeError, match="already"):
        pl.make_dub(src, "es")
    with pytest.raises(ValueError, match="already in English"):
        pl.make_dub(src, "en")


def test_languages_in_the_web_ui(client, fakes, claude):
    slug, P = new_project(client)
    assert client.put("/api/settings", headers=H, json={"CAPTION_LANGUAGES": ["es", "fr"]}).status_code == 200
    assert pl.CAPTION_LANGUAGES == ["es", "fr"]
    assert client.put("/api/settings", headers=H, json={"CAPTION_LANGUAGES": ["klingon"]}).status_code == 400
    j = run_job(client, slug, want="failed", action="translate")
    assert "voiceover first" in j["error"]
    assert client.post(f"/api/projects/{slug}/jobs", headers=H, json={"action": "dub", "language": "xx"}).status_code == 400
    run_job(client, slug, action="dub", language="es")
    p = project(client, slug)
    assert p["dubs"] == [{"slug": f"{slug}-es", "language": "es", "title": "¿Por qué Marte es rojo?"}]
    assert p["translations"]["languages"] == ["es", "fr"]
    d = project(client, f"{slug}-es")
    assert d["language"] == "es" and d["translation_of"] == slug and d["step"] == "review"
    assert d["script"]["scenes"][3]["narration"] == "Escena 3."
