"""画像入力: 分類・正規化（縮小・メタデータ除去）・保存期間・Slack 取得のテスト。"""

import base64
import io
import os
import struct
import time
import zlib
from datetime import timedelta
from pathlib import Path

import pytest
from PIL import Image

from classbot import images as im_mod
from classbot.db import utcnow
from classbot.images import (MAX_OUTPUT_BYTES, ImageError, ImageStore, NormalizedImage, classify_slack_file,
                             fetch_slack_image, normalize_image, visual_tokens)


def save(img: Image.Image, path: Path, fmt: str, **kw) -> Path:
    img.save(path, fmt, **kw)
    return path


def png_header_only(path: Path, w: int, h: int) -> Path:
    """巨大寸法を宣言しただけの小さな PNG（展開前の寸法検査用）。"""
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00" * 16)) + chunk(b"IEND", b"")
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------- 分類
@pytest.mark.parametrize("f,expected", [
    ({"mimetype": "image/png", "name": "a.png"}, "image"),
    ({"mimetype": "image/jpeg", "filetype": "jpg"}, "image"),
    ({"mimetype": "image/webp"}, "image"),
    ({"mimetype": "image/gif"}, "image"),
    ({"mimetype": "image/heic", "name": "IMG_0001.HEIC"}, "unsupported_image"),
    ({"mimetype": "", "filetype": "heic", "name": "x.heic"}, "unsupported_image"),
    ({"mimetype": "image/svg+xml", "name": "a.svg"}, "unsupported_image"),
    ({"mimetype": "image/bmp"}, "unsupported_image"),
    ({"mimetype": "", "filetype": "png", "name": "a.png"}, "image"),
    ({"mimetype": "application/octet-stream", "name": "photo.JPG"}, "image"),
    ({"mimetype": "application/pdf", "name": "a.pdf"}, "other"),
    ({"mimetype": "text/plain", "name": "a.png.txt"}, "other"),
])
def test_classify_slack_file(f, expected):
    assert classify_slack_file(f) == expected


def test_visual_tokens_matches_official_table():
    assert visual_tokens(200, 200) == 64
    assert visual_tokens(1000, 1000) == 1296
    assert visual_tokens(1092, 1092) == 1521
    assert visual_tokens(1456, 819) == 1560


# ---------------------------------------------------------------------------- 正規化
def test_png_with_alpha_kept_as_png(settings, tmp_path):
    img = Image.new("RGBA", (300, 200), (255, 0, 0, 0))
    img.putpixel((10, 10), (0, 0, 255, 255))
    out = normalize_image(save(img, tmp_path / "a.png", "PNG"), settings)
    assert out.media_type == "image/png" and (out.width, out.height) == (300, 200)
    assert Image.open(io.BytesIO(out.data)).mode == "RGBA"
    assert out.source_format == "png" and not out.animated


def test_jpeg_exif_gps_removed_and_orientation_applied(settings, tmp_path):
    img = Image.new("RGB", (400, 200), (200, 10, 10))
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: 90° 回転して表示
    exif[0x010F] = "SecretCameraMaker"
    gps = exif.get_ifd(0x8825)
    gps[2] = (35.0, 41.0, 0.0)  # GPSLatitude
    src = save(img, tmp_path / "photo.jpg", "JPEG", exif=exif.tobytes())
    assert b"SecretCameraMaker" in src.read_bytes()
    out = normalize_image(src, settings)
    assert out.media_type == "image/jpeg"
    assert (out.width, out.height) == (200, 400)  # 回転が反映されている
    assert b"SecretCameraMaker" not in out.data and b"Exif" not in out.data
    assert not Image.open(io.BytesIO(out.data)).getexif()


def test_png_text_and_icc_metadata_dropped(settings, tmp_path):
    from PIL.PngImagePlugin import PngInfo
    meta = PngInfo()
    meta.add_text("Author", "学籍番号12345")
    img = Image.new("RGB", (50, 50), "white")
    src = tmp_path / "m.png"
    img.save(src, "PNG", pnginfo=meta, icc_profile=b"\x00" * 128)
    out = normalize_image(src, settings)
    assert "学籍番号".encode() not in out.data and b"Author" not in out.data and b"iCCP" not in out.data


def test_animated_gif_first_frame(settings, tmp_path):
    frames = [Image.new("RGB", (64, 64), c) for c in ("red", "green", "blue")]
    src = tmp_path / "a.gif"
    frames[0].save(src, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    out = normalize_image(src, settings)
    assert out.animated and out.source_format == "gif" and out.media_type == "image/png"
    r, g, b = Image.open(io.BytesIO(out.data)).convert("RGB").getpixel((5, 5))
    assert r > 200 and g < 50 and b < 50  # 先頭フレーム（赤）


def test_animated_webp_first_frame(settings, tmp_path):
    frames = [Image.new("RGB", (64, 64), c) for c in ("blue", "red")]
    src = tmp_path / "a.webp"
    frames[0].save(src, "WEBP", save_all=True, append_images=frames[1:], duration=100, lossless=True)
    out = normalize_image(src, settings)
    assert out.source_format == "webp" and out.animated
    r, g, b = Image.open(io.BytesIO(out.data)).convert("RGB").getpixel((5, 5))
    assert b > 200 and r < 50


def test_static_webp_supported(settings, tmp_path):
    out = normalize_image(save(Image.new("RGB", (80, 60), "white"), tmp_path / "s.webp", "WEBP"), settings)
    assert out.source_format == "webp" and not out.animated


def test_huge_dimensions_rejected_before_decode(settings, tmp_path):
    with pytest.raises(ImageError, match="解像度"):
        normalize_image(png_header_only(tmp_path / "big.png", 9000, 100), settings)
    settings.image_max_source_pixels = 1_000_000
    with pytest.raises(ImageError, match="解像度"):
        normalize_image(png_header_only(tmp_path / "big2.png", 2000, 2000), settings)


def test_decompression_bomb_rejected(settings, tmp_path, monkeypatch):
    settings.image_max_source_pixels = 10**12  # 自前の検査を外しても Pillow の検査で止まる
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    with pytest.raises(ImageError):
        normalize_image(save(Image.new("RGB", (100, 100)), tmp_path / "b.png", "PNG"), settings)


def test_truncated_and_fake_images_rejected(settings, tmp_path):
    src = save(Image.effect_noise((300, 300), 50).convert("RGB"), tmp_path / "t.jpg", "JPEG")
    trunc = tmp_path / "trunc.jpg"
    trunc.write_bytes(src.read_bytes()[: src.stat().st_size // 2])
    with pytest.raises(ImageError, match="読み込めません"):
        normalize_image(trunc, settings)
    fake = tmp_path / "fake.png"
    fake.write_text("これは画像ではありません")
    with pytest.raises(ImageError, match="読み込めません"):
        normalize_image(fake, settings)
    bmp = save(Image.new("RGB", (10, 10)), tmp_path / "x.png", "BMP")  # 拡張子は png だが中身は BMP
    with pytest.raises(ImageError, match="対応していない"):
        normalize_image(bmp, settings)


def test_format_detected_by_content_not_name(settings, tmp_path):
    src = save(Image.new("RGB", (20, 20), "white"), tmp_path / "claims.jpg", "PNG")
    out = normalize_image(src, settings)
    assert out.source_format == "png"


def test_resize_long_edge_and_pixel_caps(settings, tmp_path):
    settings.image_max_long_edge = 1568
    settings.image_max_pixels = 10**9
    out = normalize_image(save(Image.new("RGB", (3136, 1000), "white"), tmp_path / "w.png", "PNG"), settings)
    assert (out.width, out.height) == (1568, 500)
    settings.image_max_pixels = 1_000_000
    out = normalize_image(save(Image.new("RGB", (2000, 2000), "white"), tmp_path / "s.png", "PNG"), settings)
    assert out.width * out.height <= 1_000_000 and out.width == out.height == 1000
    out = normalize_image(save(Image.new("RGB", (300, 100), "white"), tmp_path / "small.png", "PNG"), settings)
    assert (out.width, out.height) == (300, 100)  # 小さい画像は拡大しない


def test_noisy_large_image_output_within_limit(settings, tmp_path):
    settings.image_max_long_edge = 4000
    settings.image_max_pixels = 16_000_000
    noise = Image.merge("RGB", [Image.effect_noise((4000, 4000), 128) for _ in range(3)])
    out = normalize_image(save(noise, tmp_path / "noise.png", "PNG", compress_level=1), settings)
    assert len(out.data) <= MAX_OUTPUT_BYTES
    assert out.media_type == "image/jpeg"  # 不透明な写真状の PNG は JPEG に切り替える


# ---------------------------------------------------------------------------- 保存・保存期間
def norm(w=100, h=50) -> NormalizedImage:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return NormalizedImage(buf.getvalue(), "image/png", w, h, False, "png")


def store_one(store, fid="F1", ts="1.0"):
    return store.save(channel="C123", thread_ts="1.0", message_ts=ts, user_id="U1", slack_file_id=fid, img=norm())


def test_store_save_idempotent_and_private(db, settings):
    store = ImageStore(db, settings)
    a = store_one(store)
    assert a.path.is_file() and a.path.parent == settings.images_dir
    assert oct(a.path.stat().st_mode & 0o777) == "0o600"
    assert a.tokens == visual_tokens(100, 50)
    b = store_one(store)
    assert b.id == a.id and len(list(settings.images_dir.iterdir())) == 1
    assert [i.id for i in store.for_message("C123", "1.0")] == [a.id]
    assert base64.standard_b64decode(store.load_base64(a)) == a.path.read_bytes()


def test_store_expiry_for_turns_and_cleanup(db, settings):
    store = ImageStore(db, settings)
    a = store_one(store, "F1")
    b = store_one(store, "F2")
    with db.tx() as conn:
        store.link_turn(conn, 10, [a.id, b.id])
    assert [i.id for i in store.for_turns([10])[10]] == [a.id, b.id]
    db.execute("UPDATE images SET expires_at=? WHERE id=?",
               ((utcnow() - timedelta(minutes=1)).isoformat(timespec="seconds"), a.id))
    turns = store.for_turns([10])[10]
    assert turns[0] is None and turns[1].id == b.id  # 期限切れは None
    assert store.for_message("C123", "1.0") and len(store.for_message("C123", "1.0")) == 1
    assert store.cleanup() == 1
    assert not a.path.exists() and b.path.exists()
    assert store.for_turns([10])[10][0] is None  # 削除後も「期限切れ」と判別できる
    with pytest.raises(ImageError):
        store.load_base64(a)


def test_retention_zero_keeps_processing_window_only(db, settings):
    settings.image_retention_hours = 0
    store = ImageStore(db, settings)
    a = store_one(store)
    # 回答処理中は使える（定期削除で消えない）
    assert [i.id for i in store.for_message("C123", "1.0")] == [a.id]
    assert store.cleanup() == 0 and a.path.exists()
    # 異常終了で delete() が呼ばれなくても、猶予（15分）を過ぎれば自動削除される
    assert store.cleanup(utcnow() + timedelta(minutes=16)) == 1 and not a.path.exists()


def test_cleanup_orphans_and_ignores_symlink_targets(db, settings, tmp_path):
    store = ImageStore(db, settings)
    keep = store_one(store)
    old = settings.images_dir / "orphan.png"
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    young = settings.images_dir / "young.png"
    young.write_bytes(b"x")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")
    link = settings.images_dir / "link.png"
    link.symlink_to(outside)
    os.utime(link, (time.time() - 7200, time.time() - 7200), follow_symlinks=False)
    removed = store.cleanup()
    assert removed == 2 and not old.exists() and not link.is_symlink()
    assert young.exists() and keep.path.exists() and outside.read_text() == "keep me"


def test_delete_and_stats(db, settings):
    store = ImageStore(db, settings)
    a = store_one(store)
    db.execute("INSERT INTO image_requests(usage_event_id, image_count, est_image_tokens, created_at) "
               "VALUES (1, 2, 500, ?)", (utcnow().isoformat(timespec="seconds"),))
    st = store.stats((utcnow() - timedelta(days=1)).isoformat(timespec="seconds"))
    assert st == {"requests": 1, "images": 2, "est_tokens": 500, "stored": 1}
    assert store.delete([a.id]) == 1 and not a.path.exists()


# ---------------------------------------------------------------------------- Slack から取得
class FakeClient:
    def __init__(self, info):
        self.info = info

    async def files_info(self, file):
        return {"file": self.info}


async def test_fetch_slack_image_ok_and_rejections(settings, tmp_path, monkeypatch):
    calls = []

    async def fake_download(url, dest, token, max_bytes, **kw):
        calls.append((url, token, max_bytes))
        dest.write_bytes(b"data")
        return 4

    monkeypatch.setattr(im_mod, "download_slack_file", fake_download)
    settings.slack_bot_token = type(settings.slack_bot_token)("xoxb-test")
    ok = {"id": "F1", "name": "a.png", "mimetype": "image/png", "size": 100, "mode": "hosted",
          "url_private_download": "https://files.slack.com/files-pri/T/F1/download/a.png"}
    path = await fetch_slack_image(FakeClient(ok), "F1", settings, tmp_path)
    assert path.read_bytes() == b"data" and calls[0][1] == "xoxb-test"
    assert calls[0][2] == settings.image_max_file_mb * 1024 * 1024

    for bad, msg in [({**ok, "mimetype": "image/heic", "name": "a.heic"}, "対応していない"),
                     ({**ok, "mimetype": "application/pdf", "name": "a.pdf"}, "対応していない"),
                     ({**ok, "size": 11 * 1024 * 1024}, "上限"),
                     ({**ok, "mode": "external"}, "外部ファイル"),
                     ({**ok, "name": "<!channel>.png"}, None)]:
        if msg is None:
            await fetch_slack_image(FakeClient(bad), "F1", settings, tmp_path)
            continue
        with pytest.raises(ImageError, match=msg) as ei:
            await fetch_slack_image(FakeClient(bad), "F1", settings, tmp_path)
        assert "https://" not in str(ei.value)


async def test_fetch_slack_image_download_error_becomes_image_error(settings, tmp_path):
    info = {"id": "F1", "name": "<!channel>.png", "mimetype": "image/png", "size": 1,
            "url_private_download": "https://evil.example.com/x.png"}
    with pytest.raises(ImageError) as ei:
        await fetch_slack_image(FakeClient(info), "F1", settings, tmp_path)
    assert "evil" not in str(ei.value) and "<!channel>" not in str(ei.value)
