"""Claude as picture editor: ranking search results from their previews (Claude and the previews are faked)."""
from __future__ import annotations

import pipeline as pl
import webui as wu
from helpers import H, new_project


def cand(n, kind="image", **kw):
    return dict({"kind": kind, "url": f"https://cdn/{n}.jpg", "id": f"x:{n}", "credit": f"credit {n}",
                 "thumb": f"https://cdn/{n}_t.jpg", "title": f"title {n}", "source": "stock_photo"}, **kw)


class FakeClaude:
    def __init__(self, ranking, reason="the volcano is erupting"):
        self.ranking, self.reason, self.calls = ranking, reason, []

    def __call__(self, model, system, prompt, schema, max_tokens=16000):
        self.calls.append(prompt)
        return schema(ranking=self.ranking, reason=self.reason)


def test_claude_ranks_the_previews_it_can_see(monkeypatch):
    monkeypatch.setattr(pl, "preview_data", lambda url: None if "2_t" in url else "QUJD")
    claude = FakeClaude([3, 1, 9])   # image 9 does not exist and is ignored
    monkeypatch.setattr(pl, "claude_json", claude)
    cands = [cand(1), cand(2), cand(3, kind="video", duration=12), cand(4, thumb=None)]
    order, reason = pl.rank_visuals("Lava pours into the sea.", "lava ocean", cands)
    # Shown to Claude: image 1 = result 0, image 2 = result 2 (result 1's preview failed, result 3 has none)
    assert order == [0] and reason == "the volcano is erupting"
    content = claude.calls[0]
    texts = [b["text"] for b in content if b["type"] == "text"]
    images = [b for b in content if b["type"] == "image"]
    assert len(images) == 2 and images[0]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}
    assert "Lava pours into the sea." in texts[0] and "Image 2 (video, 12 s): title 3" in texts


def test_claude_ranking_maps_numbers_back_to_results(monkeypatch):
    monkeypatch.setattr(pl, "preview_data", lambda url: "QUJD")
    monkeypatch.setattr(pl, "claude_json", FakeClaude([3, 1, 3]))
    order, _ = pl.rank_visuals("n", "q", [cand(1), cand(2), cand(3)])
    assert order == [2, 0]


def test_automatic_fetch_uses_claudes_pick_and_falls_back(monkeypatch):
    monkeypatch.setattr(pl, "AUTO_PICK", "auto")
    monkeypatch.setattr(pl, "search_candidates", lambda source, query, page=1: ([cand(1), cand(2), cand(3)], []))
    monkeypatch.setattr(pl, "rank_visuals", lambda narration, query, cands: ([len(cands) - 1], "best match"))
    scene = {"narration": "A volcano.", "visual": {"source": "stock_photo", "query": "volcano"}}
    picked = pl.find_visual(scene, {"x:2"}, 5)
    assert picked["id"] == "x:3" and picked["query"] == "volcano" and picked["source"] == "stock_photo"
    assert set(picked) <= {"kind", "url", "id", "credit", "query", "source", "track"}

    # Claude finds nothing suitable, or fails: the usual search order is used instead
    monkeypatch.setattr(pl, "rank_visuals", lambda narration, query, cands: ([], "none fit"))
    monkeypatch.setitem(pl.SEARCHERS, "stock_photo", lambda q, used, s: cand(7))
    assert pl.find_visual(scene, set(), 5)["id"] == "x:7"

    def broken(*a, **kw):
        raise RuntimeError("overloaded")

    monkeypatch.setattr(pl, "rank_visuals", broken)
    assert pl.find_visual(scene, set(), 5)["id"] == "x:7"
    monkeypatch.setattr(pl, "AUTO_PICK", "rank")   # rank mode never changes automatic fetches
    monkeypatch.setattr(pl, "rank_visuals", lambda *a: ([0], "x"))
    assert pl.find_visual(scene, set(), 5)["id"] == "x:7"


def test_the_picker_asks_claude_to_rank_cached_results(client, fakes, monkeypatch):
    slug, P = new_project(client)
    with wu._candidates_lock:
        for n in (1, 2, 3):
            wu.CANDIDATES[f"x:{n}"] = cand(n)
    seen = {}

    def fake_rank(narration, query, cands):
        seen.update(narration=narration, query=query, ids=[c["id"] for c in cands])
        return [2, 0], "clearest view"

    monkeypatch.setattr(pl, "rank_visuals", fake_rank)
    r = client.post(f"/api/projects/{slug}/scenes/1/rank", headers=H,
                    json={"ids": ["x:1", "x:2", "x:3", "x:unknown"], "query": "lava"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json() == {"order": ["x:3", "x:1"], "reason": "clearest view"}
    assert seen["ids"] == ["x:1", "x:2", "x:3"] and seen["query"] == "lava" and "narration number 1" in seen["narration"]
    assert client.post(f"/api/projects/{slug}/scenes/1/rank", headers=H, json={"ids": ["nope"]}).status_code == 400


def test_the_auto_pick_setting_is_validated(client):
    assert client.put("/api/settings", headers=H, json={"AUTO_PICK": "rank"}).status_code == 200
    assert pl.AUTO_PICK == "rank"
    assert client.put("/api/settings", headers=H, json={"AUTO_PICK": "always"}).status_code == 400
    assert pl.AUTO_PICK == "rank"
