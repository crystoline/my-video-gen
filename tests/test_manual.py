"""Videos from narration you wrote yourself: the words are kept exactly, then visuals and fact-check."""
from __future__ import annotations

from types import SimpleNamespace

import pipeline as pl
from helpers import H, project, wait

LONG = ("Four billion years ago, a day on Earth lasted just six hours. The Moon was much closer then. "
        "Its pull raised tides hundreds of metres high, twice a day. Over time those tides acted like a brake. "
        "Earth's spin slowed, and the Moon slowly drifted away. It is still leaving us today, about four "
        "centimetres every year. In a few billion years a day will last much longer than it does now. "
        "And the Moon will look smaller in the sky.")


def narrations(scenes):
    return [s["narration"] for s in scenes]


def test_blank_lines_and_scene_labels_separate_scenes_and_visual_lines_set_the_search():
    text = ("Scene 1: Mars is red.\nIt is covered in rust.\nVisual: mars surface\n\n"
            "[Scene 2] Its sky is butterscotch.\r\nVisual: nasa: Mars sky rover\n"
            "Scene 3 - Dust storms can cover the whole planet.\n\n\n"
            "Scenes like these are common.\nVisual: photo: dust storm\n\nVisual: ignored because scene 4 has one")
    scenes = pl.parse_manual_script(text)
    assert narrations(scenes) == ["Mars is red. It is covered in rust.", "Its sky is butterscotch.",
                                  "Dust storms can cover the whole planet.", "Scenes like these are common."]
    assert [s["visual"] for s in scenes] == [
        {"source": "stock_video", "query": "mars surface"}, {"source": "nasa", "query": "Mars sky rover"},
        {"source": "stock_video", "query": ""}, {"source": "stock_photo", "query": "dust storm"}]
    assert pl.parse_manual_script("  \n\n ") == []


def test_one_long_block_is_split_into_scenes_of_about_two_sentences():
    scenes = pl.parse_manual_script("Visual: early earth\n" + LONG)
    texts = narrations(scenes)
    assert " ".join(texts) == LONG                                   # not a word changed or lost
    assert len(texts) >= 3 and all(len(t.split()) <= 50 for t in texts)
    assert all(t.endswith((".", "!", "?")) for t in texts)          # sentences are never cut in half
    assert scenes[0]["visual"]["query"] == "early earth" and scenes[1]["visual"]["query"] == ""
    short = "Just one sentence here. And another."
    assert narrations(pl.parse_manual_script(short)) == [short]


def test_script_from_text_keeps_the_narration_and_fills_only_missing_visuals(fakes):
    text = "A day lasted six hours.\nVisual: nasa: early Earth\n\nThe Moon was closer.\n\nTides were huge."
    script = pl.script_from_text("The day the Moon saved Earth", text, suggest=True)
    assert narrations(script["scenes"]) == ["A day lasted six hours.", "The Moon was closer.", "Tides were huge."]
    assert [s["visual"] for s in script["scenes"]] == [
        {"source": "nasa", "query": "early Earth"}, {"source": "stock_photo", "query": "planned 1"},
        {"source": "stock_photo", "query": "planned 2"}]
    assert script["description"] == "About The day the Moon saved Earth" and script["thumbnail_text"] == "WOW"
    assert fakes.plans == [narrations(script["scenes"])]

    plain = pl.script_from_text("Moon", "The ancient tides were enormous.", suggest=False)
    assert plain["scenes"][0]["visual"] == {"source": "stock_video", "query": "ancient tides enormous"}
    assert len(fakes.plans) == 1 and plain["description"] == ""


def test_creating_a_video_from_your_script_in_the_web_ui(client, fakes):
    r = client.post("/api/projects", headers=H, json={"mode": "manual", "title": "The Moon Brake",
                                                       "script": LONG, "suggest": False, "factcheck": False})
    assert r.status_code == 200, r.get_json()
    slug = r.get_json()["slug"]
    assert slug == "the-moon-brake" and r.get_json()["scenes"] >= 3
    j = wait(client, r.get_json()["job"]["id"])
    assert any("Fact-check skipped" in line for line in j["lines"])
    p = project(client, slug)
    assert p["step"] == "review" and p["title"] == "The Moon Brake" and p["source"] == "manual"
    assert " ".join(s["narration"] for s in p["script"]["scenes"]) == LONG
    assert p["factcheck"] is None and fakes.factchecks == 0 and not fakes.plans
    P = pl.PROJECTS_DIR / slug
    assert (P / "review.md").exists() and (P / "my_script.txt").read_text(encoding="utf-8") == LONG

    # With Claude's visual choices and the fact-check
    r = client.post("/api/projects", headers=H, json={"mode": "manual", "title": "Moon Brake Two", "script": LONG})
    wait(client, r.get_json()["job"]["id"])
    p = project(client, r.get_json()["slug"])
    assert fakes.factchecks == 1 and len(fakes.plans) == 1 and p["factcheck"]["issues"]
    assert all(s["visual"]["source"] == "stock_photo" for s in p["script"]["scenes"][1:])

    for bad in ({"mode": "manual", "title": "", "script": LONG}, {"mode": "manual", "title": "X", "script": "  "},
                {"mode": "manual", "title": "Y" * 101, "script": LONG}):
        assert client.post("/api/projects", headers=H, json=bad).status_code == 400
    assert client.post("/api/projects", headers=H, json={"mode": "manual", "title": "The Moon Brake",
                                                         "script": LONG}).status_code == 409


def test_a_failed_preparation_can_be_tried_again_without_retyping(client, fakes, monkeypatch):
    def broken(title, narrations):
        raise RuntimeError("Claude is overloaded")

    monkeypatch.setattr(pl, "plan_visuals", broken)
    r = client.post("/api/projects", headers=H, json={"mode": "manual", "title": "Retry Me", "script": LONG,
                                                       "factcheck": False})
    slug = r.get_json()["slug"]
    wait(client, r.get_json()["job"]["id"], want="failed")
    assert project(client, slug)["step"] == "empty"
    monkeypatch.setattr(pl, "plan_visuals", fakes.plan_visuals)
    r = client.post(f"/api/projects/{slug}/prepare", headers=H)
    assert r.status_code == 200
    wait(client, r.get_json()["id"])
    p = project(client, slug)
    assert p["step"] == "review" and " ".join(s["narration"] for s in p["script"]["scenes"]) == LONG
    assert client.post(f"/api/projects/{slug}/prepare", headers=H).status_code == 409


def test_the_command_line_takes_a_script_file(fakes, tmp_path):
    f = tmp_path / "speech.txt"
    f.write_text("Scene 1: The Moon was closer.\n\nScene 2: Tides were huge.", encoding="utf-8")
    project_dir = tmp_path / "moon"
    project_dir.mkdir()
    args = SimpleNamespace(script_file=f, yes=False, minutes=6)
    script = pl.create_script(project_dir, "Moon tides", args)
    assert narrations(script["scenes"]) == ["The Moon was closer.", "Tides were huge."]
    assert pl.load_json(project_dir / "script.json")["title"] == "Moon tides" and fakes.factchecks == 1
    assert pl.load_meta(project_dir)["source"] == "manual" and (project_dir / "review.md").exists()
