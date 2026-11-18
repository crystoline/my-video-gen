"""Voiceover, rendering and re-rendering with real FFmpeg (the voice is a fake sine-tone narrator)."""
from __future__ import annotations

import time

import pipeline as pl
from helpers import H, needs_ffmpeg, new_project, project, run_job

pytestmark = needs_ffmpeg


def clip(path, pattern, seconds=3):
    """A test clip full of motion (moving pattern plus a frame counter), so a frame out of place shows."""
    pl.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"{pattern}=duration={seconds}:size=1280x720:rate=30",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", path])
    return {"kind": "video", "path": str(path)}


def frame_psnr(a, b) -> list[float]:
    """Per-frame PSNR between two videos of the same length."""
    stats = a.with_name("psnr.log")
    pl.run(["ffmpeg", "-hide_banner", "-i", a, "-i", b, "-lavfi", f"[0:v][1:v]psnr=stats_file={stats.name}",
            "-f", "null", "-"], cwd=a.parent)
    values = []
    for line in stats.read_text().splitlines():
        field = next((f for f in line.split() if f.startswith("psnr_avg:")), None)
        if field:
            v = field.split(":")[1]
            values.append(99.0 if v == "inf" else float(v))
    return values


def test_the_quick_join_matches_a_full_re_encode_and_redoes_only_what_changed(tmp_path, monkeypatch):
    logs = []
    monkeypatch.setattr(pl, "LOG_HOOKS", [logs.append])
    monkeypatch.setattr(pl, "CROSSFADE", 0.5)
    monkeypatch.setattr(pl, "RENDER_JOBS", 3)
    (tmp_path / "assets").mkdir()
    assets = [clip(tmp_path / "assets" / f"scene_{k:02d}.mp4", p)
              for k, p in enumerate(("testsrc2", "testsrc", "smptebars", "testsrc2"))]
    timeline = [{"frames": f, "duration": f / pl.FPS} for f in (61, 47, 75, 52)]   # odd lengths on purpose
    xf = pl.render_segments(tmp_path, timeline, assets)
    assert xf == 15 and any("3 at a time" in line for line in logs)
    fast, slow = tmp_path / "fast.mp4", tmp_path / "slow.mp4"
    pl.join_segments(tmp_path / "segments", timeline, xf, fast)
    pl._join_chain(tmp_path / "segments", timeline, xf, slow)
    total = sum(t["frames"] for t in timeline)
    assert pl.video_frames(fast) == pl.video_frames(slow) == total
    assert any("3 of 3 fades re-made" in line for line in logs) and not any("slow way" in line for line in logs)
    psnr = frame_psnr(fast, slow)   # (the psnr filter may not report the very last frame)
    assert len(psnr) >= total - 1 and min(psnr) > 30, (len(psnr), min(psnr))
    # The test clips mix colour tags (smptebars is tagged BT.601, the others are not); every scene must come
    # out as BT.709, so the joined video never changes format midway
    decode = pl.subprocess.run(["ffmpeg", "-hide_banner", "-v", "verbose", "-i", str(fast), "-f", "null", "-"],
                               capture_output=True, text=True).stderr
    assert "Reconfiguring filter graph" not in decode
    assert "bt709" in pl.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=color_space", "-of", "csv=p=0", fast])

    # A new visual for scene 3 re-renders that scene only, and re-makes only its two fades
    logs.clear()
    assets[2] = clip(tmp_path / "assets" / "scene_02.mp4", "rgbtestsrc")
    pl.render_segments(tmp_path, timeline, assets)
    assert [line for line in logs if "re-rendering" in line or "Rendering scene" in line] == [
        logs[0]] and "Scene 3 has a new visual" in logs[0]
    pl.join_segments(tmp_path / "segments", timeline, xf, fast)
    assert any("2 of 3 fades re-made" in line for line in logs) and pl.video_frames(fast) == total
    pl._join_chain(tmp_path / "segments", timeline, xf, slow)
    assert min(frame_psnr(fast, slow)) > 30

    # If the quick join cannot cut the scenes, the slow join still produces the right video
    def broken(*a, **kw):
        raise RuntimeError("simulated split failure")

    monkeypatch.setattr(pl, "_split_segment", broken)
    for f in (tmp_path / "segments" / "parts").glob("*"):
        f.unlink()
    logs.clear()
    pl.join_segments(tmp_path / "segments", timeline, xf, fast)
    assert any("slow way" in line for line in logs) and pl.video_frames(fast) == total


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
    monkeypatch.setattr(pl, "write_script", lambda topic, minutes, previous, channel=None:
                        dict(fakes.write_script(topic, minutes, previous), title="Rewritten"))
    run_job(client, slug, action="rewrite", minutes=2)
    p = project(client, slug)
    assert p["title"] == "Rewritten" and p["step"] == "review"
    assert not (P / "audio").exists() and not (P / "final.mp4").exists()
    assert any((t / "audio").exists() for t in (P / ".trash").iterdir())
