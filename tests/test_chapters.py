"""Chapters: YouTube's rules, the description, Claude's suggestions (faked) and editing them in the web UI."""
from __future__ import annotations

import pipeline as pl
from helpers import H, new_project, project, run_job

SCRIPT = {"title": "T", "description": "About it.", "scenes": [{"narration": f"Scene {k}."} for k in range(6)]}
TIMELINE = [{"duration": d} for d in (30, 25, 40, 5, 50, 70)]   # starts: 0, 30, 55, 95, 100, 150


def test_chapter_lines_follow_youtubes_rules():
    s = dict(SCRIPT, chapters=[{"scene": 2, "title": "Middle"}, {"scene": 0, "title": "Intro"},
                               {"scene": 4, "title": "End"}, {"scene": 3, "title": "Too soon"}])
    # scene 3 starts 40 s after scene 2 (fine), scene 4 only 5 s after scene 3: dropped
    assert pl.chapter_lines(s, TIMELINE) == ["0:00 Intro", "0:55 Middle", "1:35 Too soon"]
    two = dict(SCRIPT, chapters=[{"scene": 0, "title": "A"}, {"scene": 2, "title": "B"}])
    assert pl.chapter_lines(two, TIMELINE) == []                      # fewer than 3: none
    late = dict(SCRIPT, chapters=[{"scene": 1, "title": "A"}, {"scene": 2, "title": "B"}, {"scene": 5, "title": "C"}])
    assert pl.chapter_lines(late, TIMELINE)[0] == "0:00 A"            # the first always starts at 0:00
    assert pl.clock(3725) == "1:02:05" and pl.clock(65.9) == "1:05"


def test_the_description_gets_the_chapters():
    s = dict(SCRIPT, chapters=[{"scene": 0, "title": "Intro"}, {"scene": 2, "title": "Middle"},
                               {"scene": 5, "title": "<Finale>"}])
    desc = pl.build_description(s, [{"credit": "Photo by A"}], TIMELINE)
    assert "About it.\n\nChapters:\n0:00 Intro\n0:55 Middle\n2:30 Finale" in desc
    assert desc.index("Chapters:") < desc.index("Visual credits:")
    assert "Chapters" not in pl.build_description(s, [], None)


def test_clean_chapters_orders_dedupes_and_starts_at_zero():
    raw = [{"scene": 3, "title": " Later  part "}, {"scene": 1, "title": "Start"}, {"scene": 3, "title": "dup"},
           {"scene": 9, "title": "out of range"}, {"scene": 2, "title": " "}, "junk"]
    assert pl.clean_chapters(raw, 6) == [{"scene": 0, "title": "Start"}, {"scene": 3, "title": "Later part"}]


def test_suggest_chapters_uses_claude_and_cleans_its_answer(monkeypatch):
    seen = {}

    def fake(model, system, prompt, schema, max_tokens=16000):
        seen["prompt"] = prompt
        return schema(chapters=[{"scene": 2, "title": "Middle"}, {"scene": 0, "title": "Intro"},
                                {"scene": 7, "title": "Bogus"}])

    monkeypatch.setattr(pl, "claude_json", fake)
    assert pl.suggest_chapters(SCRIPT, TIMELINE) == [{"scene": 0, "title": "Intro"}, {"scene": 2, "title": "Middle"}]
    assert "2. [0:55] Scene 2." in seen["prompt"] and "3 to 5" in seen["prompt"]


def test_ensure_chapters_only_when_switched_on_and_missing(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(pl, "suggest_chapters", lambda s, t: calls.append(1) or [{"scene": 0, "title": "X"}])
    script = dict(SCRIPT)
    pl.ensure_chapters(tmp_path, script, TIMELINE)              # AUTO_CHAPTERS is off in the tests
    assert not calls and "chapters" not in script
    monkeypatch.setattr(pl, "AUTO_CHAPTERS", True)
    pl.ensure_chapters(tmp_path, script, TIMELINE)
    assert script["chapters"] == [{"scene": 0, "title": "X"}] and pl.load_json(tmp_path / "script.json")["chapters"]
    pl.ensure_chapters(tmp_path, script, TIMELINE)              # already has chapters: no new request
    assert len(calls) == 1


def test_chapters_are_saved_with_the_script_and_suggested_by_a_job(client, fakes, monkeypatch):
    slug, P = new_project(client)
    script = project(client, slug)["script"]
    r = client.put(f"/api/projects/{slug}/script", headers=H, json={"script": dict(script, chapters=[
        {"scene": 2, "title": "Second part"}, {"scene": 0, "title": "Opening"}, {"scene": 2, "title": "dup"}])})
    assert r.status_code == 200
    assert r.get_json()["script"]["chapters"] == [{"scene": 0, "title": "Opening"}, {"scene": 2, "title": "Second part"}]
    monkeypatch.setattr(pl, "suggest_chapters", lambda s, t: [{"scene": 0, "title": "A"}, {"scene": 1, "title": "B"},
                                                             {"scene": 3, "title": "C"}])
    run_job(client, slug, action="chapters")
    p = project(client, slug)
    assert [c["title"] for c in p["script"]["chapters"]] == ["A", "B", "C"]
    assert isinstance(p["chapter_lines"], list)
