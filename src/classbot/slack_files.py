"""Slack に添付されたファイルの認証付きダウンロード（資料登録・画像質問で共用）。

- 接続先は https の files.slack.com のみ。Bot トークンを Slack 以外へ送らない。
- リダイレクトは追わない（別ホストへトークン付きで誘導されないように）。
- 上限バイト数を超えたら途中で打ち切り、書きかけのファイルを削除する。
- トークン・URL（クエリ含む）はログにもエラーメッセージにも出さない。
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

log = logging.getLogger(__name__)

ALLOWED_DOWNLOAD_HOSTS = ("files.slack.com",)
_CHUNK = 1 << 16


class DownloadError(Exception):
    """利用者にそのまま表示できる日本語の理由を持つダウンロードエラー。"""


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):g}MB"


async def download_slack_file(url: str, dest: Path, token: str, max_bytes: int, *,
                              timeout_sec: float = 120) -> int:
    """Slack のファイルを dest にストリーム保存し、書き込んだバイト数を返す。"""
    parsed = urlparse(url or "")
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS:
        raise DownloadError("Slack のファイル URL を取得できませんでした。")
    headers = {"Authorization": f"Bearer {token}"}
    size = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec)) as session:
            async with session.get(url, headers=headers, allow_redirects=False) as resp:
                ctype = resp.headers.get("Content-Type", "")
                if resp.status != 200 or "text/html" in ctype:
                    log.warning("slack download rejected", extra={"status": resp.status})
                    raise DownloadError("ファイルをダウンロードできませんでした（files:read 権限を確認してください）。")
                with dest.open("wb") as f:
                    async for chunk in resp.content.iter_chunked(_CHUNK):
                        size += len(chunk)
                        if size > max_bytes:
                            raise DownloadError(f"ファイルサイズが上限（{_mb(max_bytes)}）を超えています。")
                        f.write(chunk)
    except DownloadError:
        dest.unlink(missing_ok=True)
        raise
    except (aiohttp.ClientError, TimeoutError) as e:
        dest.unlink(missing_ok=True)
        log.warning("slack download failed", extra={"error": type(e).__name__})
        raise DownloadError("ファイルのダウンロード中に通信エラーが発生しました。") from None
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return size
