"""ElevenLabs characters left and YouTube statistics, with the services faked."""
from __future__ import annotations

from types import SimpleNamespace

import googleapiclient.discovery
import pytest

import pipeline as pl
from helpers import H, new_project, wait


class Resp:
    def __init__(self, status, data):
        self.status_code, self.data = status, data

    def json(self):
        return self.data


def test_elevenlabs_usage_reports_characters_left(monkeypatch):
    calls = []

    def get(url, headers=None, timeout=None):
        calls.append((url, headers))
        return Resp(200, {"character_count": 12000, "character_limit": 30000,
                          "next_character_count_reset_unix": 1767225600, "tier": "creator"})

    monkeypatch.setattr(pl.HTTP, "get", get)
    u = pl.elevenlabs_usage()
    assert u == {"used": 12000, "limit": 30000, "left": 18000, "resets": 1767225600, "tier": "creator"}
    assert calls[0][0].endswith("/v1/user/subscription") and calls[0][1]["xi-api-key"] == pl.ELEVENLABS_API_KEY

    monkeypatch.setattr(pl.HTTP, "get", lambda url, **kw: Resp(401, {"detail": {"status": "missing_permissions"}}))
    assert "permission" in pl.elevenlabs_usage()["error"]
    monkeypatch.setattr(pl, "ELEVENLABS_API_KEY", "")
    assert "not set" in pl.elevenlabs_usage()["error"]


def test_the_usage_endpoint_caches_for_a_minute(client, monkeypatch):
    import webui as wu
    calls = []
    monkeypatch.setattr(pl, "elevenlabs_usage", lambda: calls.append(1) or {"used": 1, "limit": 10, "left": 9})
    monkeypatch.setitem(wu._usage_cache, "at", 0.0)
    assert client.get("/api/elevenlabs").get_json()["left"] == 9
    client.get("/api/elevenlabs")
    assert len(calls) == 1
    client.get("/api/elevenlabs?refresh=1")
    assert len(calls) == 2


class FakeYouTube:
    """Enough of the YouTube Data and Analytics clients for youtube_stats()."""

    def __init__(self, analytics_error=None):
        self.analytics_error = analytics_error
        self.queries = []

    def videos(self):
        def list_(part, id):
            items = [{"id": vid, "statistics": {"viewCount": "1500", "likeCount": "40", "commentCount": "3"},
                      "contentDetails": {"duration": "PT6M5S"}, "snippet": {"publishedAt": "2026-11-01T10:00:00Z"}}
                     for vid in id.split(",") if vid != "GONE"]
            return SimpleNamespace(execute=lambda: {"items": items})
        return SimpleNamespace(list=list_)

    def reports(self):
        def query(**kw):
            self.queries.append(kw)
            if self.analytics_error:
                raise self.analytics_error
            rows = [[vid, 1500, 4500.0, 180, 49.6] for vid in kw["filters"].split("==")[1].split(",")]
            return SimpleNamespace(execute=lambda: {
                "columnHeaders": [{"name": n} for n in ("video", "views", "estimatedMinutesWatched",
                                                        "averageViewDuration", "averageViewPercentage")],
                "rows": rows})
        return SimpleNamespace(query=query)


@pytest.fixture()
def youtube(monkeypatch):
    fake = FakeYouTube()
    monkeypatch.setattr(pl, "youtube_credentials", lambda interactive: SimpleNamespace(
        scopes=pl.YT_SCOPES, has_scopes=lambda scopes: True))
    monkeypatch.setattr(googleapiclient.discovery, "build", lambda name, version, credentials=None: fake)
    return fake


def test_youtube_stats_merge_views_and_watch_time(youtube):
    stats, notes = pl.youtube_stats(["A", "B", "GONE"])
    assert stats["A"]["views"] == 1500 and stats["A"]["likes"] == 40 and stats["A"]["seconds"] == 365
    assert stats["A"]["minutes_watched"] == 4500.0 and stats["A"]["avg_view_seconds"] == 180
    assert stats["B"]["avg_view_percent"] == 49.6
    assert any("not found" in n for n in notes)
    q = youtube.queries[0]
    assert q["ids"] == "channel==MINE" and q["dimensions"] == "video" and q["filters"] == "video==A,B,GONE"


def test_youtube_stats_still_report_views_when_analytics_is_unavailable(youtube):
    youtube.analytics_error = RuntimeError("YouTube Analytics API has not been used in project 123")
    stats, notes = pl.youtube_stats(["A"])
    assert stats["A"]["views"] == 1500 and "minutes_watched" not in stats["A"]
    assert any("Watch time" in n and "YouTube Analytics API" in n for n in notes)


def test_refreshing_statistics_saves_them_per_video(client, fakes, monkeypatch):
    slug, P = new_project(client)
    pl.save_json(P / "youtube.json", {"video_id": "MAIN", "url": "https://youtu.be/MAIN"})
    (P / "short").mkdir()
    pl.save_json(P / "short" / "youtube.json", {"video_id": "SHORT", "url": "https://youtube.com/shorts/SHORT"})
    monkeypatch.setattr(pl, "youtube_stats", lambda ids: ({
        "MAIN": {"views": 1200, "minutes_watched": 900.0}, "SHORT": {"views": 5000}}, ["a note"]))
    r = client.post("/api/stats/refresh", headers=H)
    assert r.status_code == 200
    j = wait(client, r.get_json()["id"])
    assert any("a note" in line for line in j["lines"])
    saved = pl.load_json(P / "stats.json")
    assert saved["video"]["views"] == 1200 and saved["short"]["views"] == 5000 and saved["fetched"]
    listed = {x["slug"]: x for x in client.get("/api/projects").get_json()}[slug]
    assert listed["stats"]["video"]["minutes_watched"] == 900.0
    assert listed["short_youtube"]["video_id"] == "SHORT"
