"""Extra shots for long scenes (SHOT_SECONDS): fetching, keeping, moving, crediting and rendering them."""
from __future__ import annotations

import json

from PIL import Image

import pipeline as pl
from helpers import H, needs_ffmpeg, new_project, png_bytes, project, run_job


def test_how_many_shots_a_scene_gets(monkeypatch):
    assert pl.shots_wanted(40) == 1                       # off by default
    monkeypatch.setattr(pl, "SHOT_SECONDS", 10)
    assert [pl.shots_wanted(d) for d in (6, 12, 21, 29, 35, 90)] == [1, 1, 2, 2, 3, 3]


def test_long_scenes_get_extra_shots_that_survive_changes(client, fakes, monkeypatch):
    monkeypatch.setattr(pl, "SHOT_SECONDS", 1)   # the fake narration lasts a few seconds per scene
    slug, P = new_project(client)
    run_job(client, slug, action="visuals")
    m = pl.load_json(P / "assets.json")
    assert all(len(a["shots"]) == 2 for a in m)                       # 3 shots per scene
    ids = [a["id"] for a in m] + [s["id"] for a in m for s in a["shots"]]
    assert len(set(ids)) == len(ids) == 12                             # never the same visual twice
    assert (P / "assets" / "scene_00_s2.png").exists() and (P / "assets" / "scene_03_s3.png").exists()
    info = project(client, slug)["scene_info"][0]
    assert info["shots_wanted"] == 3 and len(info["asset"]["shots"]) == 2
    desc = pl.build_description(project(client, slug)["script"], pl.load_manifest(P))
    assert all(f"Fake credit {n}" in desc for n in range(1, 13))       # extra shots are credited too

    # A new main visual keeps the scene's extra shots; a picked shot replaces just that shot
    run_job(client, slug, action="find_visual", scene=0)
    m2 = pl.load_json(P / "assets.json")
    assert m2[0]["id"] != m[0]["id"] and m2[0]["shots"] == m[0]["shots"]
    import webui as wu
    with wu._candidates_lock:
        wu.CANDIDATES["x:shot"] = {"kind": "image", "url": "https://cdn/s.png", "id": "x:shot", "credit": "Shot credit",
                                   "source": "stock_photo", "provider": "Test"}
    run_job(client, slug, action="pick_visual", scene=0, candidate="x:shot", shot=3)
    m3 = pl.load_json(P / "assets.json")
    assert m3[0]["shots"][1]["id"] == "x:shot" and m3[0]["shots"][0] == m[0]["shots"][0]
    assert m3[0]["id"] == m2[0]["id"]   # the main visual is untouched
    assert client.post(f"/api/projects/{slug}/jobs", headers=H, json={"action": "pick_visual", "scene": 0,
                                                                     "candidate": "x:shot", "shot": 7}).status_code == 400

    # Reordering scenes moves their extra shots with them
    script = project(client, slug)["script"]
    s = script["scenes"]
    r = client.put(f"/api/projects/{slug}/script", headers=H,
                   json={"script": dict(script, scenes=[s[1], s[0], s[2], s[3]]), "origin": [1, 0, 2, 3]})
    assert r.status_code == 200, r.get_json()
    m4 = pl.load_json(P / "assets.json")
    assert m4[1]["shots"][1]["id"] == "x:shot" and m4[1]["shots"][1]["path"].endswith("scene_01_s3.png")
    assert (P / "assets" / "scene_01_s3.png").exists() and (P / "assets" / "scene_00_s2.png").exists()


def solid(path, color):
    Image.new("RGB", (1600, 900), color).save(path)
    return {"kind": "image", "path": str(path)}


def mean_color(video, t):
    raw = pl.subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(video), "-frames:v", "1",
                             "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    return tuple(raw[:3])


@needs_ffmpeg
def test_a_scene_with_three_shots_renders_as_one_exact_segment(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "SHOT_SECONDS", 1)
    monkeypatch.setattr(pl, "CROSSFADE", 0.5)
    (tmp_path / "assets").mkdir()
    red = solid(tmp_path / "assets" / "scene_00.png", (220, 30, 30))
    clip = tmp_path / "assets" / "scene_00_s2.mp4"
    pl.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x1e1edc:size=1280x720:rate=30:duration=1",
            "-pix_fmt", "yuv420p", clip])   # 1 s of blue: it must loop to fill its share
    green = solid(tmp_path / "assets" / "scene_00_s3.png", (30, 200, 30))
    scene0 = dict(red, shots=[{"kind": "video", "path": str(clip)}, green])
    scene1 = solid(tmp_path / "assets" / "scene_01.png", (240, 240, 240))
    timeline = [{"frames": 120, "duration": 4.0}, {"frames": 60, "duration": 2.0}]
    xf = pl.render_segments(tmp_path, timeline, [scene0, scene1])
    seg = tmp_path / "segments" / "seg_00.mp4"
    assert pl.video_frames(seg) == 120 + xf
    r, g, b = mean_color(seg, 0.3)
    assert r > 150 and g < 90                                            # shot 1: red
    r, g, b = mean_color(seg, 1.9)
    assert b > 150 and r < 90                                            # shot 2: the blue clip, looped
    r, g, b = mean_color(seg, 3.7)
    assert g > 150 and r < 90                                            # shot 3: green
    out = tmp_path / "joined.mp4"
    pl.join_segments(tmp_path / "segments", timeline, xf, out)
    assert pl.video_frames(out) == 180
    # The render cache notices a changed extra shot
    logs = []
    monkeypatch.setattr(pl, "LOG_HOOKS", [logs.append])
    solid(tmp_path / "assets" / "scene_00_s3.png", (200, 200, 30))
    pl.render_segments(tmp_path, timeline, [scene0, scene1])
    assert any("Scene 1 has a new visual" in line for line in logs)
    probe = json.loads(pl.run(["ffprobe", "-v", "error", "-show_entries", "stream=color_space", "-of", "json", seg]))
    assert probe["streams"][0]["color_space"] == "bt709"
