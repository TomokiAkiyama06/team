"""コンテナのヘルスチェック。

本体プロセスが heartbeat_interval_sec ごとに data/heartbeat.json を更新する。
更新が途絶えた、または Slack 接続が長時間切れている場合に異常終了コードを返す。
"""

from __future__ import annotations

import json
import time

from classbot.config import Settings


def check(settings: Settings) -> tuple[bool, str]:
    try:
        hb = json.loads(settings.heartbeat_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return False, f"heartbeat を読めません: {e}"
    age = time.time() - float(hb.get("ts", 0))
    if age > settings.heartbeat_interval_sec * 4:
        return False, f"heartbeat が {age:.0f} 秒更新されていません"
    if not hb.get("slack_connected", False):
        down = time.time() - float(hb.get("slack_connected_changed", time.time()))
        if down > settings.socket_disconnect_exit_sec:
            return False, f"Slack 未接続が {down:.0f} 秒続いています"
    return True, "ok"
