"""学生の質問画像の検証・正規化・一時保存。

公式仕様（vision）に合わせた方針:
- 対応形式は PNG / JPEG / WebP / GIF。アニメーションは非対応のため先頭フレームのみ使う。
- 拡張子や Slack の mimetype ではなく、ファイルの中身で形式を判定する。
- 展開前にヘッダーの寸法を検査し、巨大画像（画像爆弾）を拒否する。
- EXIF の向きを反映したうえで、EXIF・GPS・ICC などのメタデータはすべて除去して再エンコードする。
- 長辺と総画素数の上限まで縮小し、送信サイズを 5MB 以下に抑える（API 上限は 10MB、
  Bedrock / Google Cloud は 5MB）。
- 画像のバイト列・base64・URL・ファイル名はログに出さない（件数・寸法・サイズ・file_id のみ）。
"""

from __future__ import annotations

import base64
import io
import logging
import math
import os
import uuid
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from classbot.db import Database, now_iso, utcnow
from classbot.safety import escape_slack, safe_filename
from classbot.slack_files import DownloadError, download_slack_file

log = logging.getLogger(__name__)

SUPPORTED_IMAGE_MIMES = {"image/png": "png", "image/jpeg": "jpeg", "image/webp": "webp", "image/gif": "gif"}
_SUPPORTED_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}
_UNSUPPORTED_IMAGE_EXTS = {"heic", "heif", "svg", "bmp", "tif", "tiff", "ico", "avif", "jxl", "psd",
                           "raw", "cr2", "nef", "dng", "eps"}
_PIL_FORMATS = {"PNG": "png", "JPEG": "jpeg", "WEBP": "webp", "GIF": "gif"}

MAX_API_DIMENSION = 8000              # 公式: 1 枚あたり最大 8000x8000 px
MAX_OUTPUT_BYTES = 5 * 1024 * 1024    # 送信する 1 枚の上限（全プラットフォームで安全な値）
ORPHAN_MIN_AGE_SEC = 3600
# 保存期間 0 の設定でも、回答処理中（保存〜読み出し）に定期削除されないための猶予。
# 回答後は QA ワーカーが ImageStore.delete() で即時削除し、異常終了時はこの時刻に自動削除される。
PROCESSING_WINDOW = timedelta(minutes=15)
_MB = 1024 * 1024


class ImageError(Exception):
    """利用者にそのまま表示できる日本語の理由を持つ画像エラー（画像の内容は含めない）。"""


def classify_slack_file(f: dict) -> str:
    """Slack のファイル情報を "image" / "unsupported_image" / "other" に分類する。"""
    mimetype = (f.get("mimetype") or "").lower().split(";")[0].strip()
    filetype = (f.get("filetype") or "").lower()
    name = f.get("name") or f.get("title") or ""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if mimetype in SUPPORTED_IMAGE_MIMES:
        return "image"
    if mimetype.startswith("image/"):
        return "unsupported_image"
    if mimetype in ("", "application/octet-stream", "binary") and (
            filetype in _SUPPORTED_EXTS or ext in _SUPPORTED_EXTS):
        return "image"
    if filetype in _UNSUPPORTED_IMAGE_EXTS or ext in _UNSUPPORTED_IMAGE_EXTS:
        return "unsupported_image"
    return "other"


def visual_tokens(width: int, height: int) -> int:
    """公式の計算式: 28x28 px のパッチ 1 つが 1 ビジュアルトークン。"""
    return math.ceil(width / 28) * math.ceil(height / 28)


def display_name(f: dict) -> str:
    """利用者向けメッセージに使う短い名前（無害化・Slack エスケープ済み）。"""
    name = safe_filename(f.get("name") or f.get("title") or "画像")
    if len(name) > 40:
        name = name[:37] + "…"
    return escape_slack(name)


# ---------------------------------------------------------------------------- 正規化
@dataclass
class NormalizedImage:
    data: bytes
    media_type: str      # image/png | image/jpeg
    width: int
    height: int
    animated: bool
    source_format: str   # png | jpeg | webp | gif（中身から判定した元形式）


def _target_size(w: int, h: int, long_edge: int, max_pixels: int) -> tuple[int, int]:
    scale = min(1.0, long_edge / max(w, h), math.sqrt(max_pixels / (w * h)))
    if scale >= 1.0:
        return w, h
    return max(1, math.floor(w * scale)), max(1, math.floor(h * scale))


def _has_alpha(im: Image.Image) -> bool:
    if im.mode in ("RGBA", "LA", "PA"):
        alpha = im.getchannel("A")
        return alpha.getextrema()[0] < 255
    return False


def _encode(im: Image.Image, fmt: str, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    im.info = {}  # ICC・テキストチャンク・EXIF を書き出さない
    if fmt == "JPEG":
        im.save(buf, "JPEG", quality=quality, optimize=True)
    else:
        im.save(buf, "PNG", optimize=False, compress_level=6)
    return buf.getvalue()


def normalize_image(src: Path, settings) -> NormalizedImage:
    """画像を検証し、送信用に縮小・メタデータ除去・再エンコードする。"""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(src) as im:
                fmt = _PIL_FORMATS.get(im.format or "")
                if fmt is None:
                    raise ImageError("対応していない画像形式です（PNG / JPEG / WebP / GIF に対応）。")
                w, h = im.size
                if w <= 0 or h <= 0:
                    raise ImageError("画像の寸法が不正です。")
                if w > MAX_API_DIMENSION or h > MAX_API_DIMENSION or w * h > settings.image_max_source_pixels:
                    raise ImageError(f"画像の解像度が大きすぎます（{w}x{h}）。"
                                     f"縦横 {MAX_API_DIMENSION}px 以下に縮小してから送ってください。")
                animated = bool(getattr(im, "is_animated", False)) and getattr(im, "n_frames", 1) > 1
                if animated:
                    im.seek(0)
                im.load()
                frame = ImageOps.exif_transpose(im) if fmt == "jpeg" or "exif" in im.info else im.copy()
    except ImageError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageError("画像の解像度が大きすぎるため処理できません。") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError):
        raise ImageError("画像として読み込めませんでした（ファイルが壊れているか、対応していない形式です）。") from None

    # モード変換（透過は保持、それ以外は RGB / L）
    if frame.mode == "P":
        frame = frame.convert("RGBA" if "transparency" in frame.info else "RGB")
    elif frame.mode in ("LA", "PA"):
        frame = frame.convert("RGBA")
    elif frame.mode in ("I;16", "I", "F"):
        frame = frame.convert("L")
    elif frame.mode not in ("RGB", "RGBA", "L"):
        frame = frame.convert("RGB")
    alpha = _has_alpha(frame)
    if frame.mode == "RGBA" and not alpha:
        frame = frame.convert("RGB")

    tw, th = _target_size(frame.width, frame.height, settings.image_max_long_edge, settings.image_max_pixels)
    if (tw, th) != frame.size:
        frame = frame.resize((tw, th), Image.Resampling.LANCZOS)

    use_jpeg = fmt == "jpeg" and not alpha
    data = _encode(frame, "JPEG" if use_jpeg else "PNG")
    if len(data) > MAX_OUTPUT_BYTES and not alpha and not use_jpeg:
        use_jpeg = True  # 写真のような PNG は JPEG にすると大幅に小さくなる
        data = _encode(frame, "JPEG")
    quality = 90
    while len(data) > MAX_OUTPUT_BYTES:
        if use_jpeg and quality > 60:
            quality -= 10
        else:
            nw, nh = max(1, int(frame.width * 0.8)), max(1, int(frame.height * 0.8))
            if (nw, nh) == frame.size or nw < 64 or nh < 64:
                raise ImageError("画像を送信できるサイズまで縮小できませんでした。")
            frame = frame.resize((nw, nh), Image.Resampling.LANCZOS)
        data = _encode(frame, "JPEG" if use_jpeg else "PNG", quality)

    return NormalizedImage(data=data, media_type="image/jpeg" if use_jpeg else "image/png",
                           width=frame.width, height=frame.height, animated=animated, source_format=fmt)


# ---------------------------------------------------------------------------- 保存
@dataclass
class StoredImage:
    id: int
    path: Path
    media_type: str
    width: int
    height: int
    size_bytes: int
    tokens: int
    animated: bool


class ImageStore:
    def __init__(self, db: Database, settings):
        self.db = db
        self.s = settings

    @property
    def dir(self) -> Path:
        return self.s.images_dir

    def _row_to_image(self, row) -> StoredImage:
        return StoredImage(id=row["id"], path=self.dir / row["path"], media_type=row["media_type"],
                           width=row["width"], height=row["height"], size_bytes=row["size_bytes"],
                           tokens=row["tokens"], animated=bool(row["animated"]))

    def _available(self, row, now: str) -> bool:
        return row["expires_at"] > now and (self.dir / row["path"]).is_file()

    def save(self, *, channel: str, thread_ts: str, message_ts: str, user_id: str,
             slack_file_id: str, img: NormalizedImage) -> StoredImage:
        """正規化済み画像を保存する。同じ投稿・同じファイルは 1 件だけ（ジョブ再実行時も冪等）。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        ext = "jpg" if img.media_type == "image/jpeg" else "png"
        name = f"{uuid.uuid4().hex}.{ext}"
        path = self.dir / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(img.data)
        created = utcnow()
        hours = float(self.s.image_retention_hours)
        expires = created + (timedelta(hours=hours) if hours > 0 else PROCESSING_WINDOW)
        try:
            with self.db.tx() as conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO images(channel, thread_ts, message_ts, user_id, slack_file_id, path, "
                    "media_type, width, height, size_bytes, tokens, animated, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (channel, thread_ts, message_ts, user_id, slack_file_id, name, img.media_type,
                     img.width, img.height, len(img.data), visual_tokens(img.width, img.height),
                     int(img.animated), created.isoformat(timespec="seconds"),
                     expires.isoformat(timespec="seconds")))
                if cur.rowcount != 1:
                    existing = conn.execute(
                        "SELECT * FROM images WHERE channel=? AND message_ts=? AND slack_file_id=?",
                        (channel, message_ts, slack_file_id)).fetchone()
                else:
                    existing = None
                    row = conn.execute("SELECT * FROM images WHERE id=?", (cur.lastrowid,)).fetchone()
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        if existing is not None:
            path.unlink(missing_ok=True)
            return self._row_to_image(existing)
        log.info("image stored", extra={"image_id": row["id"], "slack_file_id": slack_file_id,
                                        "width": img.width, "height": img.height, "bytes": len(img.data)})
        return self._row_to_image(row)

    def for_message(self, channel: str, message_ts: str) -> list[StoredImage]:
        """投稿に添付された画像のうち、保存期間内でファイルが残っているもの。"""
        now = now_iso()
        rows = self.db.query("SELECT * FROM images WHERE channel=? AND message_ts=? ORDER BY id",
                             (channel, message_ts))
        return [self._row_to_image(r) for r in rows if self._available(r, now)]

    def link_turn(self, conn, turn_id: int, image_ids: list[int]) -> None:
        """会話履歴の質問と画像を対応付ける（呼び出し側のトランザクション内で実行）。"""
        for iid in image_ids:
            conn.execute("INSERT OR IGNORE INTO turn_images(turn_id, image_id) VALUES (?, ?)", (turn_id, iid))

    def for_turns(self, turn_ids: list[int]) -> dict[int, list[StoredImage | None]]:
        """履歴の質問ごとの画像。保存期間切れ・削除済みは None（「参照できません」と伝えるため）。"""
        if not turn_ids:
            return {}
        now = now_iso()
        marks = ",".join("?" * len(turn_ids))
        rows = self.db.query(
            f"SELECT t.turn_id, t.image_id, i.* FROM turn_images t LEFT JOIN images i ON i.id = t.image_id "
            f"WHERE t.turn_id IN ({marks}) ORDER BY t.turn_id, t.image_id", tuple(turn_ids))
        out: dict[int, list[StoredImage | None]] = {}
        for r in rows:
            img = self._row_to_image(r) if r["id"] is not None and self._available(r, now) else None
            out.setdefault(r["turn_id"], []).append(img)
        return out

    def load_base64(self, img: StoredImage) -> str:
        try:
            return base64.standard_b64encode(img.path.read_bytes()).decode("ascii")
        except OSError:
            raise ImageError("画像の保存期間が過ぎたため参照できません。") from None

    def _remove_file(self, rel: str) -> bool:
        """images_dir 直下のファイルだけを削除する（シンボリックリンクは辿らない）。"""
        if "/" in rel or "\\" in rel or rel in ("", ".", ".."):
            return False
        p = self.dir / rel
        try:
            if p.is_symlink() or p.is_file():
                p.unlink()
                return True
        except FileNotFoundError:
            pass
        return False

    def delete(self, image_ids: list[int]) -> int:
        """指定画像を即時削除する（保存期間 0 の設定で回答後に消す場合など）。"""
        if not image_ids:
            return 0
        marks = ",".join("?" * len(image_ids))
        with self.db.tx() as conn:
            rows = conn.execute(f"SELECT id, path FROM images WHERE id IN ({marks})", tuple(image_ids)).fetchall()
            conn.execute(f"DELETE FROM images WHERE id IN ({marks})", tuple(image_ids))
        return sum(self._remove_file(r["path"]) for r in rows)

    def cleanup(self, now: datetime | None = None) -> int:
        """保存期間を過ぎた画像と、DB に記録の無い古いファイルを削除する。削除したファイル数を返す。
        turn_images は残す（履歴で「保存期間切れ」と判別するため。画像データは含まない）。"""
        now_s = (now or utcnow()).isoformat(timespec="seconds")
        with self.db.tx() as conn:
            rows = conn.execute("SELECT id, path FROM images WHERE expires_at <= ?", (now_s,)).fetchall()
            if rows:
                marks = ",".join("?" * len(rows))
                conn.execute(f"DELETE FROM images WHERE id IN ({marks})", tuple(r["id"] for r in rows))
        removed = sum(self._remove_file(r["path"]) for r in rows)

        if self.dir.is_dir():
            known = {r["path"] for r in self.db.query("SELECT path FROM images")}
            cutoff = (now or utcnow()).timestamp() - ORPHAN_MIN_AGE_SEC
            with os.scandir(self.dir) as it:
                for entry in it:
                    if entry.name in known or entry.is_dir(follow_symlinks=False):
                        continue
                    try:
                        mtime = entry.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        continue
                    if mtime < cutoff and self._remove_file(entry.name):
                        removed += 1
        if removed:
            log.info("images cleaned up", extra={"files": removed})
        return removed

    def stats(self, since_iso: str) -> dict:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(image_count), 0) AS imgs, "
            "COALESCE(SUM(est_image_tokens), 0) AS toks FROM image_requests WHERE created_at >= ?",
            (since_iso,))
        stored = self.db.query_one("SELECT COUNT(*) AS n FROM images")
        return {"requests": row["n"], "images": row["imgs"], "est_tokens": row["toks"],
                "stored": stored["n"]}


# ---------------------------------------------------------------------------- Slack から取得
async def fetch_slack_image(client, file_id: str, settings, tmp_dir: Path) -> Path:
    """Slack の画像を認証付きで一時ファイルに取得する（呼び出し側が削除する）。"""
    info = (await client.files_info(file=file_id))["file"]
    name = display_name(info)
    if info.get("mode") in ("external", "snippet", "post") or info.get("is_external"):
        raise ImageError(f"「{name}」は外部ファイルのため読み取れません。画像を直接添付してください。")
    kind = classify_slack_file(info)
    if kind != "image":
        raise ImageError(f"「{name}」は対応していない形式です（PNG / JPEG / WebP / GIF に対応）。")
    limit = int(settings.image_max_file_mb) * _MB
    if int(info.get("size") or 0) > limit:
        raise ImageError(f"「{name}」はサイズが上限（{settings.image_max_file_mb}MB）を超えています。")
    url = info.get("url_private_download") or info.get("url_private") or ""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dest = tmp_dir / f"img-{uuid.uuid4().hex}"
    try:
        size = await download_slack_file(url, dest, settings.slack_bot_token.get_secret_value(), limit)
    except DownloadError as e:
        raise ImageError(f"「{name}」: {e}") from None
    log.info("image downloaded", extra={"slack_file_id": file_id, "bytes": size})
    return dest
