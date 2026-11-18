"""The archive and photo providers (Wikimedia Commons, Openverse, Smithsonian, Unsplash), their licence checks,
and switching providers on, off and around in Settings. HTTP is faked with the providers' real response shapes."""
from __future__ import annotations

import pytest
import requests

import pipeline as pl
import webui as wu
from helpers import H, png_bytes


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self.data


@pytest.fixture()
def http(monkeypatch):
    calls = []

    def install(routes):
        def get(url, params=None, headers=None, timeout=None, **kw):
            calls.append((url, dict(params or {}), dict(headers or {})))
            for prefix, data in routes.items():
                if url.startswith(prefix):
                    return data if isinstance(data, Resp) else Resp(data)
            raise AssertionError(f"unexpected URL {url}")
        monkeypatch.setattr(pl.HTTP, "get", get)
        return calls
    return install


def commons_page(pageid, title, lic, mime="image/jpeg", w=4000, h=3000, **info):
    return {"pageid": pageid, "title": f"File:{title}", "index": pageid, "imageinfo": [dict({
        "url": f"https://upload.wikimedia.org/x/{title}?utm_content=original", "width": w, "height": h, "mime": mime,
        "thumburl": f"https://upload.wikimedia.org/thumb/x/{title}/1920px-{title}",
        "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{title}",
        "extmetadata": {"LicenseShortName": {"value": lic}, "ObjectName": {"value": title.rsplit('.', 1)[0]},
                        "Artist": {"value": '<a href="//x">Jane &amp; Co</a>'}}}, **info)]}


def test_licences_allowed_for_a_monetised_video(monkeypatch):
    for ok in ("Public domain", "PD-USGov", "CC0", "CC BY 4.0", "CC-BY 2.0", "cc by"):
        assert pl.license_ok(ok), ok
    for bad in ("CC BY-NC 2.0", "CC BY-ND 4.0", "CC BY-SA 3.0", "Fair use", "", "GFDL"):
        assert not pl.license_ok(bad), bad
    monkeypatch.setattr(pl, "ALLOW_SHARE_ALIKE", True)
    assert pl.license_ok("CC BY-SA 3.0") and not pl.license_ok("CC BY-NC-SA 2.0")


def test_wikimedia_commons_images_and_videos(http):
    calls = http({"https://commons.wikimedia.org/w/api.php": {"query": {"pages": [
        commons_page(2, "Eruption.jpg", "CC BY 4.0"),
        commons_page(1, "Portrait.jpg", "CC BY 4.0", w=900, h=1400),          # portrait and small: skipped
        commons_page(3, "SA.jpg", "CC BY-SA 3.0"),                             # share-alike: skipped by default
        commons_page(4, "Map.tif", "Public domain", mime="image/tiff"),        # not a web image: skipped
        commons_page(5, "Lava.webm", "CC0", mime="video/webm", w=3840, h=2160, duration=23.8,
                     thumburl="https://thumb/lava.jpg")]}}})
    images = pl.commons_image_candidates("volcano eruption")
    assert [c["id"] for c in images] == ["commons:2"]
    c = images[0]
    assert c["url"].endswith("/1920px-Eruption.jpg") and c["thumb"].endswith("/640px-Eruption.jpg")
    assert c["credit"].startswith("Eruption by Jane & Co (CC BY 4.0), via Wikimedia Commons: https://commons")
    assert c["source"] == "archive" and c["kind"] == "image"
    params = calls[-1][1]
    assert params["gsrsearch"] == "volcano eruption filetype:bitmap" and params["gsrnamespace"] == 6
    videos = pl.commons_video_candidates("lava")
    assert [(v["id"], v["kind"], v["duration"]) for v in videos] == [("commons:5", "video", 24)]
    assert calls[-1][1]["gsrsearch"] == "lava filetype:video"


def test_openverse_filters_licences_and_uses_its_attribution(http, monkeypatch):
    item = {"id": "abc", "title": "Raikoke", "url": "https://live.staticflickr.com/x_b.jpg", "creator": "NASA",
            "license": "by", "license_version": "2.0", "width": 1600, "height": 1000, "mature": False,
            "thumbnail": "https://api.openverse.org/v1/images/abc/thumb/", "source": "nasa", "provider": "flickr",
            "attribution": '"Raikoke" by NASA is licensed under CC BY 2.0.'}
    sa = dict(item, id="sa", license="by-sa")
    calls = http({"https://api.openverse.org/v1/images/": {"results": [item, sa, dict(item, id="m", mature=True)]}})
    found = pl.openverse_candidates("volcano")
    assert [c["id"] for c in found] == ["openverse:abc"]
    assert found[0]["credit"] == item["attribution"] and found[0]["provider"] == "Openverse (nasa)"
    assert calls[-1][1]["license_type"] == "commercial,modification"
    monkeypatch.setattr(pl, "ALLOW_SHARE_ALIKE", True)
    assert [c["id"] for c in pl.openverse_candidates("volcano")] == ["openverse:abc", "openverse:sa"]
    http({"https://api.openverse.org/v1/images/": Resp({}, 400)})
    assert pl.openverse_candidates("volcano", page=9) == []


def test_smithsonian_cc0_images_at_a_sensible_size(http, monkeypatch):
    assert pl.smithsonian_candidates("locomotive") == []   # no key: no request at all
    monkeypatch.setattr(pl, "SMITHSONIAN_API_KEY", "si-key")
    media = {"id": "m1", "type": "Images", "usage": {"access": "CC0"}, "width": 2880, "height": 2290,
             "content": "https://ids.si.edu/ids/deliveryService?id=NMAH-1",
             "thumbnail": "https://ids.si.edu/ids/deliveryService?id=NMAH-1&max=150"}
    rows = [{"title": "Steam Locomotive, <I>Pioneer</I>", "content": {"descriptiveNonRepeating": {
                "data_source": "National Museum of American History", "online_media": {"media": [media]}}}},
            {"title": "Restricted", "content": {"descriptiveNonRepeating": {"online_media": {"media": [
                dict(media, usage={"access": "Usage conditions apply"})]}}}}]
    calls = http({"https://api.si.edu/openaccess/api/v1.0/search": {"response": {"rows": rows}}})
    [c] = pl.smithsonian_candidates("locomotive")
    assert c["url"] == "https://ids.si.edu/ids/deliveryService?id=NMAH-1&max=1920"
    assert c["thumb"].endswith("&max=640") and c["title"] == "Steam Locomotive, Pioneer"
    assert "Smithsonian Open Access, CC0" in c["credit"] and calls[-1][1]["api_key"] == "si-key"
    ark = {"content": "https://ids.si.edu/ids/deliveryService/id/ark:/1/abc",
           "thumbnail": "https://ids.si.edu/ids/deliveryService/id/ark:/1/abc/90"}
    assert pl._si_sized(ark, 1920) == "https://ids.si.edu/ids/deliveryService/id/ark:/1/abc/1920"


def test_unsplash_photos_are_credited_and_their_use_is_reported(http, monkeypatch, tmp_path):
    assert pl.unsplash_candidates("forest") == []
    monkeypatch.setattr(pl, "UNSPLASH_ACCESS_KEY", "un-key")
    calls = http({"https://api.unsplash.com/search/photos": {"results": [{
        "id": "u1", "width": 6000, "height": 4000, "alt_description": "misty forest",
        "urls": {"raw": "https://images.unsplash.com/photo-1?ixid=x", "small": "https://images.unsplash.com/s"},
        "user": {"name": "Ana"}, "links": {"download_location": "https://api.unsplash.com/photos/u1/download?ixid=x"}}]},
        "https://api.unsplash.com/photos/u1/download": {}})
    [c] = pl.unsplash_candidates("forest")
    assert c["url"] == "https://images.unsplash.com/photo-1?ixid=x&w=1920&fm=jpg&q=85"
    assert c["credit"] == "Photo by Ana on Unsplash" and calls[-1][2]["Authorization"] == "Client-ID un-key"
    monkeypatch.setattr(pl, "download", lambda url, dest: dest.write_bytes(png_bytes()))
    asset = pl._first_unused([c], set())
    pl.store_asset(tmp_path, [], 0, asset, download_from=asset["url"])
    assert calls[-1][0].startswith("https://api.unsplash.com/photos/u1/download")   # the use was reported
    assert "track" not in pl.load_json(tmp_path / "assets.json")[0]


def test_a_broken_download_moves_on_to_the_next_result(monkeypatch, tmp_path):
    results = iter([{"kind": "image", "url": "https://a/1.jpg", "id": "x:1", "credit": "one"},
                    {"kind": "image", "url": "https://a/2.jpg", "id": "x:2", "credit": "two"}])
    monkeypatch.setattr(pl, "find_visual", lambda scene, used, min_seconds: next(results))
    monkeypatch.setattr(pl, "download", lambda url, dest: dest.write_bytes(
        b"<html>captcha</html>" if url.endswith("1.jpg") else png_bytes()))
    used = set()
    asset = pl.fetch_into(tmp_path, [], 0, {"visual": {}}, used, 5)
    assert asset["id"] == "x:2" and used == {"x:1", "x:2"} and (tmp_path / "assets" / "scene_00.jpg").exists()


def test_providers_are_switched_and_ordered_in_settings(client, monkeypatch):
    monkeypatch.setenv("PIXABAY_API_KEY", "k")   # (saving settings re-reads the environment)
    pl.configure()
    st = client.get("/api/status").get_json()
    ids = [p["id"] for p in st["providers"]]
    assert ids == ["pexels", "pixabay", "unsplash", "wikimedia", "openverse", "smithsonian", "nasa"]
    wiki = next(p for p in st["providers"] if p["id"] == "wikimedia")
    assert wiki["ready"] and wiki["key"] is None and "archive & museums" in wiki["kinds"]
    r = client.put("/api/settings", headers=H, json={"VISUAL_PROVIDERS": ["openverse", "pixabay", "nasa"],
                                                     "ALLOW_SHARE_ALIKE": True})
    assert r.status_code == 200, r.get_json()
    assert pl.VISUAL_PROVIDERS == ["openverse", "pixabay", "nasa"] and pl.ALLOW_SHARE_ALIKE
    assert "VISUAL_PROVIDERS=openverse,pixabay,nasa" in wu.ENV_PATH.read_text(encoding="utf-8")
    assert [n for n, _ in pl.providers_for("archive")] == ["Openverse"]
    assert [n for n, _ in pl.providers_for("stock_video")] == ["Pixabay"]   # Wikimedia is off now
    st = client.get("/api/status").get_json()
    assert [p["id"] for p in st["providers"]][:3] == ["openverse", "pixabay", "nasa"]
    assert not next(p for p in st["providers"] if p["id"] == "wikimedia")["enabled"]
    assert client.put("/api/settings", headers=H, json={"VISUAL_PROVIDERS": ["bogus"]}).status_code == 400
    r = client.put("/api/settings", headers=H, json={"VISUAL_PROVIDERS": []})
    assert r.status_code == 200 and pl.VISUAL_PROVIDERS == []
