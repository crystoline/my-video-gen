"""Voiceover, rendering and re-rendering with real FFmpeg (the voice is a fake sine-tone narrator)."""
from __future__ import annotations

import time

import pipeline as pl
from helpers import H, needs_ffmpeg, new_project, project, run_job

pytestmark = needs_ffmpeg


def seg_times(P, n):
    return {k: (P / "segments" / f"seg_{k:02d}.mp4").stat().st_mtime_ns for k in range(n)}


def test_full_flow_from_voiceover_to_upload(client, fakes, monkeypatch):
    slug, P = new_project(client)

    run_job(client, slug, action="voiceover")
    p = project(client, slug)
    assert len(fakes.tts_calls) == 4 and p["voice_todo"] == [] and p["approved"] and p["step"] == "visuals"

    run_job(client, slug, action="visuals")
    run_job(client, slug, action="render")
    p = project(client, slug)
    assert p["video"] == "ready" and p["step"] == "upload" and p["final_url"] and p["thumbnail_url"]
    tl = pl.load_json(P / "timeline.json")
    frames = sum(t["frames"] for t in tl)
    assert pl.video_frames(P / "video_only.mp4") == frames
    assert abs(pl.media_duration(P / "final.mp4") - frames / pl.FPS) < 0.15

    # Rendering again without changes skips every scene and the join, and only re-mixes
    before = seg_times(P, 4)
    j = run_job(client, slug, action="render")
    assert not any("Joining" in line or "Rendering scene" in line for line in j["lines"]), j["lines"]
    assert any("Mixing final video" in line for line in j["lines"])
    assert seg_times(P, 4) == before

    # A swapped visual re-renders only its scene
    time.sleep(1.1)
    run_job(client, slug, action="find_visual", scene=2)
    j = run_job(client, slug, action="render")
    assert any("Scene 3 has a new visual" in line for line in j["lines"]), j["lines"]
    now = seg_times(P, 4)
    assert now[2] != before[2] and all(now[k] == before[k] for k in (0, 1, 3))
    assert project(client, slug)["video"] == "ready"

    # Re-voicing one scene narrates only that scene and marks the video out of date
    run_job(client, slug, action="revoice", scene=0)
    assert fakes.tts_calls[-1] == "scene_00.mp3" and len(fakes.tts_calls) == 5
    assert project(client, slug)["video"] == "outdated"
    run_job(client, slug, action="render")
    assert pl.video_frames(P / "video_only.mp4") == sum(t["frames"] for t in pl.load_json(P / "timeline.json"))

    # The sound preview mixes a short MP3 with the music bed
    music = P.parent / "bed.mp3"
    pl.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anoisesrc=color=pink:duration=3:amplitude=0.5", "-ac", "2", music])
    monkeypatch.setattr(pl, "MUSIC_PATH", music)
    run_job(client, slug, action="preview", seconds=6)
    assert abs(pl.media_duration(P / "preview_mix.mp3") - 6) < 0.3 and project(client, slug)["preview_url"]
    monkeypatch.setattr(pl, "MUSIC_PATH", None)

    # The thumbnail job refreshes the thumbnail after its text changes
    d = project(client, slug)["script"]
    d["thumbnail_text"] = "NEW WORDS"
    client.put(f"/api/projects/{slug}/script", json={"script": d}, headers=H)
    assert project(client, slug)["thumbnail_outdated"]
    run_job(client, slug, action="thumbnail")
    assert not project(client, slug)["thumbnail_outdated"]

    # Upload runs non-interactively, converts the schedule to UTC and never uploads twice
    run_job(client, slug, action="upload", privacy="unlisted", publish_at="2099-01-01T10:00:00+02:00")
    args = fakes.uploads[-1]
    assert args.privacy == "unlisted" and args.publish_at == "2099-01-01T08:00:00Z" and args.yes
    p = project(client, slug)
    assert p["youtube"]["url"] == "https://youtu.be/VID123" and p["step"] == "uploaded"
    j = run_job(client, slug, want="failed", action="upload")
    assert "Already uploaded" in j["error"]

    # Rewriting starts over and keeps the old narration and renders in .trash
    monkeypatch.setattr(pl, "write_script", lambda topic, minutes, previous:
                        dict(fakes.write_script(topic, minutes, previous), title="Rewritten"))
    run_job(client, slug, action="rewrite", minutes=2)
    p = project(client, slug)
    assert p["title"] == "Rewritten" and p["step"] == "review"
    assert not (P / "audio").exists() and not (P / "final.mp4").exists()
    assert any((t / "audio").exists() for t in (P / ".trash").iterdir())
