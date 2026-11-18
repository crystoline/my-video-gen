"""Shorts: a vertical cut of a run of scenes with burned-in captions, made from the existing narration."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pipeline as pl
from helpers import H, needs_ffmpeg, new_project, project, run_job


def test_the_default_short_is_the_opening_scenes_up_to_about_a_minute(tmp_path):
    script = {"title": "Why is Mars red?", "scenes": [{"narration": "x"}] * 4}
    timeline = [{"duration": d} for d in (20, 25, 30, 10)]
    s = pl.short_settings(tmp_path, script, timeline)
    assert (s["first"], s["last"], s["title"], s["seconds"]) == (0, 1, "Why is Mars red?", 45)
    pl.save_meta(tmp_path, {"short": {"first": 2, "last": 3, "title": "Rust everywhere"}})
    s = pl.short_settings(tmp_path, script, timeline)
    assert (s["first"], s["last"], s["title"], s["seconds"]) == (2, 3, "Rust everywhere", 40)
    pl.save_meta(tmp_path, {"short": {"first": 3, "last": 9}})   # out of range after scenes were deleted
    s = pl.short_settings(tmp_path, script, timeline)
    assert (s["first"], s["last"]) == (3, 3)


def test_short_captions_are_short_timed_chunks_with_the_title_on_top(tmp_path):
    (tmp_path / "audio").mkdir()
    text = "Mars is red because of rust {really} in its dust."
    step = 0.1
    pl.save_json(tmp_path / "audio" / "scene_01.json",
                 {"characters": list(text), "start": [k * step for k in range(len(text))],
                  "end": [(k + 1) * step for k in range(len(text))]})
    script = {"title": "T", "scenes": [{"narration": "Skipped scene."}, {"narration": text}]}
    timeline = [{"duration": 3.0, "speech": 2.0}, {"duration": 6.0, "speech": 5.0}]
    out = pl.write_short_captions(tmp_path, script, timeline, [1], "Why is Mars red?", tmp_path / "c.ass")
    ass = out.read_text(encoding="utf-8")
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    lines = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert lines[0].endswith(",Title,,0,0,0,,Why is Mars red?") and lines[0].split(",")[2] == "0:00:06.00"
    captions = [l.split(",", 9)[9] for l in lines[1:]]
    assert all(len(c.split()) <= 3 for c in captions) and "{" not in "".join(captions)
    assert " ".join(captions).split() == text.replace("{", "").replace("}", "").split()
    assert lines[1].split(",")[1] == "0:00:00.00"   # times start at the Short's own beginning


def test_short_settings_through_the_api(client, fakes):
    slug, P = new_project(client)
    url = f"/api/projects/{slug}/short"
    for bad in ({"first": 2, "last": 1}, {"first": 0, "last": 9}, {"first": "0", "last": 1},
                {"first": 0, "last": 1, "title": "x" * 91}):
        assert client.put(url, json=bad, headers=H).status_code == 400, bad
    r = client.put(url, json={"first": 1, "last": 2, "title": "  A  title "}, headers=H)
    assert r.status_code == 200, r.get_json()
    short = r.get_json()["short"]
    assert (short["first"], short["last"], short["title"], short["state"]) == (1, 2, "A title", "missing")
    assert len(short["scene_seconds"]) == 4 and short["max_seconds"] == 180


@needs_ffmpeg
def test_making_and_uploading_a_short(client, fakes, monkeypatch, tmp_path):
    music = tmp_path / "bed.wav"   # with music, so the caption and ducking filter graphs must coexist
    pl.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anoisesrc=color=pink:duration=3:amplitude=0.3", "-ac", "2", music])
    monkeypatch.setattr(pl, "MUSIC_PATH", music)
    slug, P = new_project(client)
    run_job(client, slug, action="voiceover")
    client.put(f"/api/projects/{slug}/short", json={"first": 1, "last": 2, "title": "Short title"}, headers=H)
    j = run_job(client, slug, action="short")
    assert any("Making the Short" in line for line in j["lines"]), j["lines"]
    video = P / "short" / "short.mp4"
    info = json.loads(pl.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                              "-show_entries", "format=duration", "-of", "json", video]))
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1080, 1920) and any(s["codec_type"] == "audio" for s in info["streams"])
    tl = pl.load_json(P / "timeline.json")
    assert abs(float(info["format"]["duration"]) - (tl[1]["duration"] + tl[2]["duration"])) < 0.15
    assert "Short title" in (P / "short" / "captions.ass").read_text(encoding="utf-8")
    p = project(client, slug)
    assert p["short"]["state"] == "ready" and p["short"]["url"]

    # Changing the range makes it out of date; the main video's scenes are untouched by the Short
    client.put(f"/api/projects/{slug}/short", json={"first": 0, "last": 1, "title": "Short title"}, headers=H)
    assert project(client, slug)["short"]["state"] == "outdated"
    j = run_job(client, slug, want="failed", action="upload_short", privacy="private")
    assert "Make it first" in j["error"]
    run_job(client, slug, action="short")
    run_job(client, slug, action="upload_short", privacy="unlisted")
    kind, args = fakes.uploads[-1]
    assert kind == "short" and args.privacy == "unlisted" and args.yes
    p = project(client, slug)
    assert p["short"]["youtube"]["url"] == "https://youtube.com/shorts/SHORT1" and p["youtube"] is None
    assert "already uploaded" in run_job(client, slug, want="failed", action="upload_short")["error"]


def test_the_short_upload_is_tagged_and_links_to_the_full_video(tmp_path, monkeypatch):
    (tmp_path / "short").mkdir()
    (tmp_path / "short" / "short.mp4").write_bytes(b"x")
    pl.save_json(tmp_path / "youtube.json", {"video_id": "MAIN", "url": "https://youtu.be/MAIN"})
    pl.save_meta(tmp_path, {"short": {"first": 0, "last": 0, "title": "Mars <is> red"}})
    sent = {}

    class Request:
        def next_chunk(self):
            return None, {"id": "S1"}

    class Videos:
        def insert(self, part, body, media_body):
            sent.update(body)
            return Request()

    monkeypatch.setattr(pl, "get_youtube", lambda interactive: SimpleNamespace(videos=lambda: Videos()))
    monkeypatch.setattr(pl, "MediaFileUpload", lambda *a, **kw: None, raising=False)
    script = {"title": "Why is Mars red?", "description": "First paragraph.\n\nSecond paragraph.",
              "tags": ["mars"], "scenes": [{"narration": "x"}]}
    timeline = [{"duration": 3.0}]
    pl.save_json(tmp_path / "timeline.json", timeline)
    args = SimpleNamespace(yes=True, privacy="private", publish_at=None, synthetic=False)
    assert pl.upload_short(tmp_path, script, [{"credit": "Photo by Ann"}], args) == "S1"
    snippet = sent["snippet"]
    assert snippet["title"] == "Mars is red #Shorts"
    assert "First paragraph." in snippet["description"] and "Second paragraph." not in snippet["description"]
    assert "Full video: https://youtu.be/MAIN" in snippet["description"] and "Photo by Ann" in snippet["description"]
    assert sent["status"]["privacyStatus"] == "private"
