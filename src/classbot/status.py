"""利用状況・ボット状態の表示文（Slack と CLI で共用）。"""

from __future__ import annotations

import time

from classbot.budget import UsageSummary
from classbot.config import Settings
from classbot.db import Database
from classbot.jobs import JobQueue
from classbot.materials import MaterialStore
from classbot.slack_format import human_size

USAGE_DISCLAIMER = (
    "※ 費用は API 応答のトークン数と config/pricing.toml の単価から算出した *推定値* です。"
    "実際の請求額は Anthropic Console で確認してください。"
    "上限はこのボット経由の利用分のみが対象で、同じ API キーを他のシステムで使った費用は含みません。"
)


def format_usage(s: UsageSummary, settings: Settings, top: int = 10,
                 images: dict | None = None) -> str:
    lines = [
        "*利用状況*",
        f"• 今日: {s.today_requests}件 / 推定 ${s.today_cost_usd:.3f}（上限超過で拒否 {s.rejected_today}件）",
        f"• 今月: {s.month_requests}件 / 推定 ${s.month_cost_usd:.3f}"
        f"（予算 ${s.budget_usd:.2f}・残り ${s.remaining_usd:.2f}・処理中の予約 ${s.reserved_usd:.3f}）",
        f"• 今月のトークン: 入力 {s.month_input_tokens:,} / 出力 {s.month_output_tokens:,}"
        f" / キャッシュ書込 {s.month_cache_creation_tokens:,} / キャッシュ読込 {s.month_cache_read_tokens:,}",
    ]
    if images is not None:
        lines.append(f"• 今月の画像付き質問: {images.get('requests', 0)}件 / 画像 {images.get('images', 0)}枚"
                     f"（推定画像トークン {images.get('est_tokens', 0):,}。費用は上の入力トークンに含まれます）"
                     f" / 現在保存中の画像 {images.get('stored', 0)}枚（保存期間 {settings.image_retention_hours:g}時間）")
    if s.month_estimate_only_count:
        lines.append(f"• 実使用量を取得できず予約額で計上した件数: {s.month_estimate_only_count}")
    if s.daily:
        lines.append("*日別（直近）*")
        lines += [f"• {d}: {n}件 / ${c:.3f}" for d, n, c in s.daily[-7:]]
    if s.users:
        lines.append(f"*ユーザー別（今月・上位{top}）*")
        lines += [f"• <@{u.user_id}>: 今日 {u.today_requests}件 / 今月 {u.month_requests}件 / ${u.month_cost_usd:.3f}"
                  for u in s.users[:top]]
    lines.append(f"上限: 1人1日 {settings.daily_request_limit_per_user}回 / 同時実行 {settings.max_concurrent_requests}")
    lines.append(USAGE_DISCLAIMER)
    return "\n".join(lines)


def format_status(settings: Settings, db: Database, store: MaterialStore, queue: JobQueue,
                  runtime: dict) -> str:
    counts = store.status_counts()
    jobs = queue.counts()
    uptime = int(time.time() - runtime.get("started_at", time.time()))
    db_size = settings.db_path.stat().st_size if settings.db_path.exists() else 0
    mat = " / ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "なし"
    job = " / ".join(f"{k} {v}" for k, v in sorted(jobs.items())) or "なし"
    return "\n".join([
        "*ボット状態*",
        f"• 稼働時間: {uptime // 3600}時間{uptime % 3600 // 60}分",
        f"• Slack 接続: {'接続中' if runtime.get('slack_connected') else '未接続'}",
        f"• モデル: `{settings.claude_model}`（effort: {settings.claude_effort}）",
        f"• 資料: {mat}",
        f"• ジョブ: {job}",
        f"• 最終スキャン: {runtime.get('last_scan_at', '-')}（{runtime.get('last_scan_summary', '')}）",
        f"• ファイル監視: {'watchdog + 定期照合' if runtime.get('watchdog') else '定期照合のみ'}"
        f"（{settings.scan_interval_sec}秒間隔）",
        f"• DB サイズ: {human_size(db_size)}",
    ])
