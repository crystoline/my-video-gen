"""Where viewers leave (audience retention per scene) and topic ideas from results (YouTube and Claude faked)."""
from __future__ import annotations

from types import SimpleNamespace

import googleapiclient.discovery

import pipeline as pl
from helpers import H, new_project, project


def test_retention_is_mapped_onto_the_scenes():
    timeline = [{"duration": 10}, {"duration": 30}, {"duration": 60}]   # 0-10%, 10-40%, 40-100%
    curve = [[0.0, 1.0], [0.1, 0.8], [0.4, 0.5], [1.0, 0.2]]
    r = pl.scene_retention(timeline, curve)
    assert r == [{"start": 100.0, "end": 80.0, "drop": 20.0}, {"start": 80.0, "end": 50.0, "drop": 30.0},
                 {"start": 50.0, "end": 20.0, "drop": 30.0}]
    assert pl.scene_retention(timeline, []) == []
    half = pl.scene_retention([{"duration": 5}, {"duration": 5}], [[0.0, 1.0], [1.0, 0.5]])
    assert half[0]["end"] == 75.0


def test_youtube_stats_fetch_retention_for_full_videos(monkeypatch):
    queries = []

    def query(**kw):
        queries.append(kw)
        if kw["dimensions"] == "elapsedVideoTimeRatio":
            return SimpleNamespace(execute=lambda: {
                "columnHeaders": [{"name": "elapsedVideoTimeRatio"}, {"name": "audienceWatchRatio"}],
                "rows": [[0.01, 1.0], [0.5, 0.6], [1.0, 0.3]]})
        return SimpleNamespace(execute=lambda: {"columnHeaders": [{"name": "video"}, {"name": "views"}], "rows": []})

    fake = SimpleNamespace(
        videos=lambda: SimpleNamespace(list=lambda part, id: SimpleNamespace(execute=lambda: {"items": [
            {"id": v, "statistics": {"viewCount": "5"}} for v in id.split(",")]})),
        reports=lambda: SimpleNamespace(query=query))
    monkeypatch.setattr(pl, "youtube_credentials", lambda interactive: SimpleNamespace(scopes=pl.YT_SCOPES))
    monkeypatch.setattr(googleapiclient.discovery, "build", lambda *a, **kw: fake)
    stats, notes = pl.youtube_stats(["MAIN", "SHORT"], ["MAIN"])
    assert stats["MAIN"]["retention"] == [[0.01, 1.0], [0.5, 0.6], [1.0, 0.3]] and "retention" not in stats["SHORT"]
    q = queries[-1]
    assert q["filters"] == "video==MAIN" and q["metrics"] == "audienceWatchRatio"


def test_the_script_tab_gets_per_scene_retention(client, fakes):
    slug, P = new_project(client)
    pl.save_json(P / "stats.json", {"video": {"views": 10, "retention": [[0.0, 1.0], [1.0, 0.4]]}})
    r = project(client, slug)["retention"]
    assert len(r) == 4 and r[0]["start"] == 100.0 and r[-1]["end"] == 40.0


def test_topic_ideas_come_from_the_channel_and_its_results(client, fakes, monkeypatch):
    slug, P = new_project(client, "Why is Mars red")
    pl.save_json(P / "stats.json", {"video": {"views": 4200, "avg_view_percent": 51.0}})
    seen = {}

    def fake(model, system, prompt, schema, max_tokens=16000):
        seen.update(system=system, prompt=prompt)
        return schema(ideas=[{"topic": "Why is Venus so hot?", "why": "Planet colours did well."},
                             {"topic": "Topic B", "why": "Already queued."},
                             {"topic": "Title for Why is Mars red", "why": "Already made."}])

    monkeypatch.setattr(pl, "claude_json", fake)
    r = client.post("/api/queue/ideas", headers=H, json={})
    assert r.status_code == 200, r.get_json()
    assert [i["topic"] for i in r.get_json()["ideas"]] == ["Why is Venus so hot?"]   # queued and made ones dropped
    assert "4,200 views, viewers watch 51% on average" in seen["prompt"]
    assert "science, planet Earth and astronomy" in seen["system"] and "- Topic A" in seen["prompt"]
    assert client.post("/api/queue/ideas", headers=H, json={"channel": "nope"}).status_code == 400
