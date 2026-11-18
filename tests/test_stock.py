"""Stock footage searches (Pexels, Pixabay, NASA) with the HTTP layer faked: no network, no API keys."""
from __future__ import annotations

import inspect

import pytest
import requests

import pipeline as pl


class FakeResp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class FakeHTTP:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        for prefix, data in self.routes.items():
            if url.startswith(prefix):
                if isinstance(data, Exception):
                    raise data
                return FakeResp(data)
        raise AssertionError(f"unexpected URL {url}")


def vid(i, duration, widths=(3840, 1920, 1280, 960)):
    names = ("large", "medium", "small", "tiny")
    return {"id": i, "duration": duration, "user": f"user{i}",
            "videos": {n: {"url": f"https://cdn.pixabay.com/v{i}_{w}.mp4", "width": w, "height": w * 9 // 16,
                           "size": 1, "thumbnail": f"https://cdn.pixabay.com/v{i}_{w}.jpg"}
                       for n, w in zip(names, widths)}}


PIXABAY_VIDEOS = {"hits": [vid(1, 3), vid(2, 10)]}
PIXABAY_PHOTOS = {"hits": [
    {"id": 7, "user": "alice", "largeImageURL": "https://pixabay.com/get/7_1280.jpg",
     "webformatURL": "https://pixabay.com/get/7_640.jpg", "imageWidth": 4000, "imageHeight": 3000},
    {"id": 8, "user": "bob", "largeImageURL": "https://pixabay.com/get/8_1280.jpg",
     "webformatURL": "https://pixabay.com/get/8_640.jpg", "imageWidth": 4000, "imageHeight": 3000}]}
PEXELS_VIDEOS = {"videos": [
    {"id": 99, "duration": 12, "user": {"name": "Pat"}, "image": "https://images.pexels.com/99.jpg",
     "video_files": [{"file_type": "video/mp4", "width": 1920, "height": 1080, "link": "https://pexels/99_hd.mp4"},
                     {"file_type": "video/mp4", "width": 640, "height": 360, "link": "https://pexels/99_sd.mp4"}]}]}


@pytest.fixture()
def http(monkeypatch):
    def install(routes):
        fake = FakeHTTP(routes)
        monkeypatch.setattr(pl, "HTTP", fake)
        return fake
    return install


def test_schema_prompt_and_searchers_use_generic_stock_names():
    sources = {"nasa", "stock_video", "stock_photo", "archive"}
    assert set(pl.Visual.model_json_schema()["properties"]["source"]["enum"]) == sources
    src = inspect.getsource(pl.write_script)
    assert '"stock_video"' in src and '"archive"' in src and "pexels" not in src.lower()
    assert set(pl.SEARCHERS) == sources


def test_pixabay_video_picks_the_1080p_file_of_the_first_long_enough_unused_clip(http, monkeypatch):
    monkeypatch.setattr(pl, "PIXABAY_API_KEY", "pix-key")
    fake = http({"https://pixabay.com/api/videos/": PIXABAY_VIDEOS})
    r = pl.pixabay_video_search("ocean waves", set(), min_seconds=5)
    assert r == {"kind": "video", "url": "https://cdn.pixabay.com/v2_1920.mp4", "id": "pixabay-video:2",
                 "credit": "Video by user2 on Pixabay"}
    url, params, headers = fake.calls[-1]
    assert params["key"] == "pix-key" and params["q"] == "ocean waves" and params["video_type"] == "film"
    assert params["safesearch"] == "true" and params["min_width"] == 1280 and headers == {}
    r = pl.pixabay_video_search("ocean waves", {"pixabay-video:2"}, min_seconds=5)
    assert r["id"] == "pixabay-video:1" and r["url"].endswith("v1_1920.mp4")
    # a clip whose files are all too small is skipped
    http({"https://pixabay.com/api/videos/": {"hits": [vid(3, 9, widths=(960, 640, 480, 320)), vid(4, 9)]}})
    assert pl.pixabay_video_search("x", set())["id"] == "pixabay-video:4"


def test_pixabay_photo_returns_the_large_image_and_skips_used_ones(http, monkeypatch):
    monkeypatch.setattr(pl, "PIXABAY_API_KEY", "pix-key")
    fake = http({"https://pixabay.com/api/": PIXABAY_PHOTOS})
    r = pl.pixabay_photo_search("volcano", {"pixabay-photo:7"})
    assert r == {"kind": "image", "url": "https://pixabay.com/get/8_1280.jpg", "id": "pixabay-photo:8",
                 "credit": "Photo by bob on Pixabay"}
    params = fake.calls[-1][1]
    assert params["image_type"] == "photo" and params["orientation"] == "horizontal" and params["q"] == "volcano"


def test_stock_tries_pexels_first_and_falls_back_to_pixabay(http, monkeypatch):
    monkeypatch.setattr(pl, "PIXABAY_API_KEY", "pix-key")
    monkeypatch.setattr(pl, "PEXELS_API_KEY", "")
    fake = http({"https://pixabay.com/api/videos/": PIXABAY_VIDEOS, "https://pixabay.com/api/": PIXABAY_PHOTOS})
    assert pl.stock_video_search("ocean", set())["id"].startswith("pixabay-video:")
    assert pl.stock_photo_search("ocean", set())["id"].startswith("pixabay-photo:")
    assert all(c[0].startswith("https://pixabay.com") for c in fake.calls), "Pexels must not be called without a key"

    monkeypatch.setattr(pl, "PEXELS_API_KEY", "pex-key")
    fake = http({"https://api.pexels.com/": requests.ConnectionError("pexels down"),
                 "https://pixabay.com/api/videos/": PIXABAY_VIDEOS})
    assert pl.stock_video_search("ocean", set())["id"] == "pixabay-video:1"
    assert fake.calls[0][0].startswith("https://api.pexels.com/videos/search")

    fake = http({"https://api.pexels.com/videos/search": PEXELS_VIDEOS})
    r = pl.stock_video_search("ocean", set())
    assert r["id"] == "pexels-video:99" and r["url"] == "https://pexels/99_hd.mp4"
    assert len(fake.calls) == 1, "Pixabay must not be called when Pexels delivers"
    assert pl.stock_providers() == ["Pexels", "Pixabay"]
    monkeypatch.setattr(pl, "PEXELS_API_KEY", "")
    assert pl.stock_providers() == ["Pixabay"]


def test_old_pexels_source_names_still_work(http, monkeypatch):
    monkeypatch.setattr(pl, "PIXABAY_API_KEY", "pix-key")
    http({"https://pixabay.com/api/videos/": PIXABAY_VIDEOS})
    r = pl.find_visual({"visual": {"source": "pexels_video", "query": "ocean waves"}}, set(), 5)
    assert r["source"] == "stock_video" and r["id"] == "pixabay-video:2" and r["query"] == "ocean waves"


def test_picker_candidates_carry_preview_images_and_clips(http, monkeypatch):
    monkeypatch.setattr(pl, "PEXELS_API_KEY", "pex-key")
    monkeypatch.setattr(pl, "PIXABAY_API_KEY", "pix-key")
    http({"https://api.pexels.com/videos/search": PEXELS_VIDEOS,
          "https://pixabay.com/api/videos/": PIXABAY_VIDEOS, "https://pixabay.com/api/": PIXABAY_PHOTOS,
          "https://api.pexels.com/v1/search": {"photos": [{"id": 5, "photographer": "Kim", "width": 6000, "height": 4000,
                                                          "src": {"original": "https://pexels/5.jpeg",
                                                                  "medium": "https://pexels/5_m.jpeg"}}]}})
    pv = pl.pexels_video_candidates("ocean")[0]
    assert pv["thumb"] == "https://images.pexels.com/99.jpg" and pv["preview"] == "https://pexels/99_sd.mp4"
    assert pv["url"] == "https://pexels/99_hd.mp4" and pv["duration"] == 12 and pv["source"] == "stock_video"
    xv = pl.pixabay_video_candidates("ocean")
    assert [c["id"] for c in xv] == ["pixabay-video:1", "pixabay-video:2"]
    assert xv[1]["thumb"] == "https://cdn.pixabay.com/v2_1920.jpg" and xv[1]["preview"] == "https://cdn.pixabay.com/v2_960.mp4"
    xp = pl.pixabay_photo_candidates("volcano")[0]
    assert xp["thumb"] == "https://pixabay.com/get/7_640.jpg" and xp["url"] == "https://pixabay.com/get/7_1280.jpg"
    pp = pl.pexels_photo_candidates("volcano")[0]
    assert pp["thumb"] == "https://pexels/5_m.jpeg" and pp["credit"] == "Photo by Kim on Pexels"


def test_a_pixabay_page_past_the_last_result_is_just_empty(http, monkeypatch):
    class Past(FakeResp):
        status_code = 400

        def raise_for_status(self):
            raise requests.HTTPError("400")

    monkeypatch.setattr(pl, "PIXABAY_API_KEY", "pix-key")
    fake = http({})
    fake.get = lambda url, **kw: Past({})
    assert pl.pixabay_video_candidates("ocean", page=3) == [] and pl.pixabay_photo_candidates("ocean", page=3) == []


def test_search_candidates_interleaves_providers_and_reports_failures(monkeypatch):
    monkeypatch.setattr(pl, "PEXELS_API_KEY", "k")
    monkeypatch.setattr(pl, "PIXABAY_API_KEY", "k")
    monkeypatch.setattr(pl, "VISUAL_PROVIDERS", ["pexels", "pixabay"])

    def mk(prefix, n):
        return lambda q, per_page, page: [{"id": f"{prefix}{k}", "kind": "video"} for k in range(n)]

    monkeypatch.setattr(pl, "pexels_video_candidates", mk("p", 2))
    monkeypatch.setattr(pl, "pixabay_video_candidates", mk("x", 3))
    results, errors = pl.search_candidates("stock_video", "ocean")
    assert [c["id"] for c in results] == ["p0", "x0", "p1", "x1", "x2"] and errors == []

    def down(q, per_page, page):
        raise requests.ConnectionError("pixabay down")

    monkeypatch.setattr(pl, "pixabay_video_candidates", down)
    results, errors = pl.search_candidates("stock_video", "ocean")
    assert [c["id"] for c in results] == ["p0", "p1"] and len(errors) == 1 and "Pixabay" in errors[0]


def test_nasa_candidates_show_a_preview_and_look_up_the_large_image_when_used(http):
    http({"https://images-api.nasa.gov/search": {"collection": {"items": [
        {"href": "http://images-assets.nasa.gov/image/PIA1/collection.json",
         "data": [{"nasa_id": "PIA1", "title": "Mars", "center": "JPL"}],
         "links": [{"href": "http://images-assets.nasa.gov/image/PIA1/PIA1~thumb.jpg", "rel": "preview", "render": "image"}]},
        {"data": [{"title": "no id"}]}]}},
        "https://images-assets.nasa.gov/image/PIA1/collection.json": [
            "http://images-assets.nasa.gov/image/PIA1/PIA1~small.jpg",
            "http://images-assets.nasa.gov/image/PIA1/PIA1~large.jpg"]})
    [c] = pl.nasa_candidates("mars")
    assert c["id"] == "nasa:PIA1" and c["url"] is None and c["thumb"].startswith("https://") and c["source"] == "nasa"
    assert pl.nasa_image_url(c["collection"]) == "https://images-assets.nasa.gov/image/PIA1/PIA1~large.jpg"
    r = pl.nasa_search("mars", set())
    assert r == {"kind": "image", "url": "https://images-assets.nasa.gov/image/PIA1/PIA1~large.jpg", "id": "nasa:PIA1",
                 "credit": "Mars (JPL; NASA image PIA1)"}
    assert pl.nasa_search("mars", {"nasa:PIA1"}) is None


def test_without_stock_keys_scenes_fall_back_to_nasa(http, monkeypatch):
    monkeypatch.setattr(pl, "VISUAL_PROVIDERS", ["pexels", "pixabay", "nasa"])
    fake = http({})
    nasa = lambda q, used, min_seconds=0: {"kind": "image", "url": "https://nasa/x.jpg", "id": "nasa:x", "credit": "NASA"}
    monkeypatch.setitem(pl.SEARCHERS, "nasa", nasa)
    r = pl.find_visual({"visual": {"source": "stock_video", "query": "ocean waves"}}, set(), 5)
    assert r["source"] == "nasa" and fake.calls == []
    assert pl.stock_providers() == []
