"""On-screen extras: callouts burned into their scene and bar charts as visuals (Claude faked)."""
from __future__ import annotations

from PIL import Image

import pipeline as pl
from helpers import H, needs_ffmpeg, new_project, project, run_job


def test_suggested_extras_fill_only_empty_scenes_and_are_cleaned(monkeypatch):
    script = {"scenes": [{"narration": "The Moon drifts away by four centimetres a year."},
                         {"narration": "Mercury is 58 million km from the Sun; Earth 150 million.",
                          "callout": {"text": "Mine", "anchor": ""}},
                         {"narration": "Nothing to show here."}]}

    def fake(model, system, prompt, schema, max_tokens=16000):
        assert "Never invent numbers" in prompt
        return schema(callouts=[{"scene": 0, "text": "4 cm a year", "anchor": "centimetres"},
                                {"scene": 1, "text": "Theirs", "anchor": "km"},
                                {"scene": 7, "text": "Out of range", "anchor": ""}],
                      charts=[{"scene": 1, "title": "Distance from the Sun", "unit": "million km",
                               "items": [{"label": "Mercury", "value": 58}, {"label": "Earth", "value": 150}]},
                              {"scene": 2, "title": "One bar", "unit": "", "items": [{"label": "A", "value": 1}]}])

    monkeypatch.setattr(pl, "claude_json", fake)
    assert pl.suggest_extras(script) == {"callouts": 1, "charts": 1}
    assert script["scenes"][0]["callout"] == {"text": "4 cm a year", "anchor": "centimetres"}
    assert script["scenes"][1]["callout"]["text"] == "Mine"                     # yours is kept
    assert script["scenes"][1]["chart"]["items"][1] == {"label": "Earth", "value": 150.0}
    assert "chart" not in script["scenes"][2]                                    # one bar is not a chart
    assert pl.clean_chart({"title": "x", "items": [{"label": "a", "value": -1}, {"label": "b", "value": "nan"}]}) is None
    assert pl.clean_callout({"text": "  " }) is None


def test_a_callout_appears_at_its_word(tmp_path):
    (tmp_path / "audio").mkdir()
    text = "The Moon drifts away by four centimetres a year."
    step = 0.1
    pl.save_json(tmp_path / "audio" / "scene_00.json", {"characters": list(text),
                                                         "start": [k * step for k in range(len(text))],
                                                         "end": [(k + 1) * step for k in range(len(text))]})
    scene = {"narration": text, "callout": {"text": "4 cm a year", "anchor": "Centimetres"}}
    start, end = pl.callout_timing(tmp_path, 0, scene, {"duration": 6.0, "speech": 4.9})
    assert start == round(text.index("centimetres") * step, 2) and end == 5.85   # 3.2 s, but not past the scene
    start, end = pl.callout_timing(tmp_path, 0, dict(scene, callout={"text": "x", "anchor": "nowhere"}),
                                   {"duration": 2.0, "speech": 1.6})
    assert (start, end) == (0.4, 1.85)
    assert pl.callout_timing(tmp_path, 0, {"narration": text}, {"duration": 6.0, "speech": 5}) is None


@needs_ffmpeg
def test_a_callout_is_burned_into_its_scene_only(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "CROSSFADE", 0)
    (tmp_path / "assets").mkdir()
    for k in (0, 1):
        Image.new("RGB", (1600, 900), (30, 60, 120)).save(tmp_path / "assets" / f"scene_{k:02d}.png")
    assets = [{"kind": "image", "path": str(tmp_path / "assets" / f"scene_{k:02d}.png")} for k in (0, 1)]
    timeline = [{"frames": 90, "duration": 3.0}, {"frames": 90, "duration": 3.0}]
    callouts = [{"text": "4 cm a year", "start": 0.5, "end": 2.5}, None]
    pl.render_segments(tmp_path, timeline, assets, callouts=callouts)
    seg0, seg1 = (tmp_path / "segments" / f"seg_{k:02d}.mp4" for k in (0, 1))
    assert pl.video_frames(seg0) == 90 and (tmp_path / "segments" / "callout_00.ass").exists()

    def top_left(seg, t):   # the top-left corner, where the callout box sits, as small raw pixels
        return pl.subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(seg), "-frames:v", "1", "-vf",
                                  "crop=640:160:90:70,scale=64:16", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                                 capture_output=True).stdout

    def diff(a, b):
        return sum(abs(x - y) for x, y in zip(a, b)) / len(a)

    with_box, before, other = top_left(seg0, 1.5), top_left(seg0, 0.1), top_left(seg1, 1.5)
    assert diff(before, other) < 3                                 # no box before it appears, nor in scene 2
    assert diff(with_box, before) > 15                             # the box and its white text change that corner
    logs = []
    monkeypatch.setattr(pl, "LOG_HOOKS", [logs.append])
    pl.render_segments(tmp_path, timeline, assets, callouts=[dict(callouts[0], text="5 cm a year"), None])
    assert [line for line in logs if "Rendering scene" in line] == [logs[0]] and "1/2" in logs[0]


def test_a_chart_picture(tmp_path):
    chart = {"title": "Distance from the Sun", "unit": "million km",
             "items": [{"label": "Mercury", "value": 58}, {"label": "Earth", "value": 150}, {"label": "Mars", "value": 228}]}
    out = pl.render_chart(chart, tmp_path / "c.png")
    img = Image.open(out).convert("RGB")
    assert img.size == (1920, 1080) and img.getpixel((5, 5)) == (0x1a, 0x1a, 0x19)
    blue = sum(1 for x in range(0, 1920, 8) for y in range(300, 1000, 8) if img.getpixel((x, y)) == (0x39, 0x87, 0xe5))
    assert blue > 300                                               # the bars are there
    assert pl.compact_number(150) == "150" and pl.compact_number(12900) == "12.9K" and pl.compact_number(2.5) == "2.5"


def test_charts_and_callouts_through_the_web_ui(client, fakes):
    slug, P = new_project(client)
    script = project(client, slug)["script"]
    script["scenes"][1]["callout"] = {"text": "58 million km", "anchor": "number"}
    script["scenes"][2]["chart"] = {"title": "Distances", "unit": "km",
                                    "items": [{"label": "A", "value": 1}, {"label": "B", "value": 2}]}
    script["scenes"][3]["chart"] = {"title": "Bad", "items": [{"label": "A", "value": 1}]}
    r = client.put(f"/api/projects/{slug}/script", headers=H, json={"script": script})
    saved = r.get_json()["script"]["scenes"]
    assert saved[1]["callout"]["text"] == "58 million km" and saved[2]["chart"]["title"] == "Distances"
    assert "chart" not in saved[3]
    assert client.post(f"/api/projects/{slug}/jobs", headers=H, json={"action": "use_chart", "scene": 3}).status_code == 400
    run_job(client, slug, action="use_chart", scene=2)
    a = pl.load_json(P / "assets.json")[2]
    assert a["source"] == "chart" and a["kind"] == "image" and a["credit"] == "" and (P / "assets" / Path(a["path"]).name).exists()
    assert client.put("/api/settings", headers=H, json={"CALLOUTS": False}).status_code == 200 and not pl.CALLOUTS


from pathlib import Path  # noqa: E402
