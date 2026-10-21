"""利用回数・月額予算の管理。

- リクエスト受付時: ユーザー別の日次上限を確認（check_daily）
- API 呼び出し直前: BEGIN IMMEDIATE の中で「当月の消費額 + 実行中の予約額 + 今回の予約額」が
  予算以下であることを確認し、予約を作る（reserve）。同時リクエストでも予算を超えない。
- API 呼び出し後: 実際のトークン数から推定費用を計上し予約を解除（settle / fail）
- 起動時: 前回プロセスが残した予約は、実費不明のため予約額で計上する（recover_stale）

金額はすべてこのボット経由の利用に対する推定値。同じ API キーを他のシステムで
使った場合の費用は把握できない。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from classbot.config import Settings
from classbot.db import Database, now_iso, utcnow
from classbot.pricing import PriceTable, cost_usd

log = logging.getLogger(__name__)

COUNTED_STATUSES = ("pending", "ok", "error")


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class Reservation:
    usage_event_id: int
    reservation_id: int
    amount_usd: float


@dataclass
class Rejection:
    reason: str            # "daily" | "budget"
    message: str           # 利用者向けの日本語メッセージ
    used: int = 0
    limit: int = 0


@dataclass
class UserUsage:
    user_id: str
    today_requests: int
    month_requests: int
    month_cost_usd: float


@dataclass
class UsageSummary:
    today_requests: int
    today_cost_usd: float
    month_requests: int
    month_cost_usd: float
    month_input_tokens: int
    month_output_tokens: int
    month_cache_creation_tokens: int
    month_cache_read_tokens: int
    month_estimate_only_count: int
    reserved_usd: float
    budget_usd: float
    remaining_usd: float
    rejected_today: int
    daily: list[tuple[str, int, float]] = field(default_factory=list)   # (YYYY-MM-DD, 件数, 費用)
    users: list[UserUsage] = field(default_factory=list)


class BudgetManager:
    def __init__(self, db: Database, settings: Settings, prices: PriceTable,
                 fallback_possible: bool = False):
        """fallback_possible: サーバー側フォールバックが動き得る（拒否された試行と
        フォールバック先の試行の両方が課金され得る）場合 True。予約額を保守的に増やす。"""
        self.db = db
        self.settings = settings
        self.prices = prices
        self.fallback_possible = fallback_possible
        self.tz = ZoneInfo(settings.timezone)
        # 設定モデルの料金が無ければここで例外（起動時に検出）
        self.model_price = prices.price_for(settings.claude_model)

    # --- 期間境界（ローカル日付 → UTC ISO 文字列） ---
    def _local_now(self) -> datetime:
        return utcnow().astimezone(self.tz)

    def day_start_iso(self, now: datetime | None = None) -> str:
        local = (now or utcnow()).astimezone(self.tz)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")

    def month_start_iso(self, now: datetime | None = None) -> str:
        local = (now or utcnow()).astimezone(self.tz)
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")

    # --- 見積もり ---
    def _attempt_cost(self, p, est_input_tokens: int) -> float:
        cost = cost_usd(p, est_input_tokens, self.settings.claude_max_tokens)
        if self.settings.claude_prompt_cache:
            # キャッシュ書き込みは通常入力より高いので、その差額も保守的に見込む
            cost += est_input_tokens * max(0.0, p.cache_write - p.input) / 1_000_000
        return cost

    def estimate_cost(self, est_input_tokens: int) -> float:
        """1 リクエストの最大見込み額。フォールバックが動き得る場合は、設定モデルの試行に加えて
        料金表で最も高いモデルでの 2 回目の試行分も予約する。"""
        cost = self._attempt_cost(self.model_price, est_input_tokens)
        if self.fallback_possible:
            cost += self._attempt_cost(self.prices.conservative(), est_input_tokens)
        return cost

    # --- 日次上限 ---
    def _daily_count(self, conn, user_id: str) -> int:
        row = conn.execute(
            f"SELECT COUNT(*) FROM usage_events WHERE user_id = ? AND created_at >= ? "
            f"AND status IN ({','.join('?' * len(COUNTED_STATUSES))})",
            (user_id, self.day_start_iso(), *COUNTED_STATUSES),
        ).fetchone()
        return int(row[0])

    def check_daily(self, user_id: str, is_admin: bool) -> tuple[bool, int, int]:
        """(許可するか, 本日の利用回数, 上限)。受付時の早期判定用（確定判定は reserve）。"""
        limit = self.settings.daily_request_limit_per_user
        used = self._daily_count(self.db.connect(), user_id)
        if is_admin and self.settings.admins_exempt_from_daily_limit:
            return True, used, limit
        return used < limit, used, limit

    # --- 予約 ---
    def _month_committed(self, conn) -> float:
        spent = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_events WHERE created_at >= ?",
            (self.month_start_iso(),),
        ).fetchone()[0]
        reserved = conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) FROM budget_reservations").fetchone()[0]
        return float(spent) + float(reserved)

    def reserve(self, user_id: str, channel: str | None, thread_ts: str | None,
                est_input_tokens: int, is_admin: bool = False) -> Reservation | Rejection:
        amount = self.estimate_cost(est_input_tokens)
        limit = self.settings.daily_request_limit_per_user
        with self.db.tx() as conn:
            exempt = is_admin and self.settings.admins_exempt_from_daily_limit
            used = self._daily_count(conn, user_id)
            rejection: Rejection | None = None
            if not exempt and used >= limit:
                rejection = Rejection(
                    "daily",
                    f"本日の利用上限（{limit}回）に達しました。明日またご利用ください。",
                    used, limit)
            elif self._month_committed(conn) + amount > self.settings.monthly_budget_usd:
                rejection = Rejection(
                    "budget",
                    "今月の利用予算に達したため、現在 AI への質問を受け付けられません。"
                    "授業担当者にお問い合わせください。")
            now = now_iso()
            if rejection:
                conn.execute(
                    "INSERT INTO usage_events(created_at, user_id, channel, thread_ts, model, status)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (now, user_id, channel, thread_ts, self.settings.claude_model,
                     f"rejected_{rejection.reason}"))
                log.warning("request_rejected", extra={"reason": rejection.reason, "user": user_id})
                return rejection
            ev = conn.execute(
                "INSERT INTO usage_events(created_at, user_id, channel, thread_ts, model, status)"
                " VALUES (?, ?, ?, ?, ?, 'pending')",
                (now, user_id, channel, thread_ts, self.settings.claude_model)).lastrowid
            rid = conn.execute(
                "INSERT INTO budget_reservations(usage_event_id, amount_usd, created_at)"
                " VALUES (?, ?, ?)", (ev, amount, now)).lastrowid
        return Reservation(int(ev), int(rid), amount)

    def settle(self, reservation: Reservation, model: str | None, usage: Usage,
               request_id: str | None = None,
               iterations: list[tuple[str | None, Usage]] | None = None) -> float:
        """実際のトークン数で費用を計上し予約を解除する。計上額を返す。

        iterations（フォールバック時の試行ごとのモデルと使用量）があれば、各試行を
        その試行を実行したモデルの単価で合算する。usage は合計値として行に保存する。"""
        parts = iterations or [(model, usage)]
        cost = sum(
            cost_usd(self.prices.price_or_conservative(m), u.input_tokens, u.output_tokens,
                     u.cache_creation_input_tokens, u.cache_read_input_tokens)
            for m, u in parts)
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE usage_events SET status='ok', model=?, input_tokens=?, output_tokens=?,"
                " cache_creation_input_tokens=?, cache_read_input_tokens=?, cost_usd=?, request_id=?"
                " WHERE id=?",
                (model, usage.input_tokens, usage.output_tokens, usage.cache_creation_input_tokens,
                 usage.cache_read_input_tokens, cost, request_id, reservation.usage_event_id))
            conn.execute("DELETE FROM budget_reservations WHERE id=?", (reservation.reservation_id,))
        return cost

    def fail(self, reservation: Reservation, error: str, billed_unknown: bool = False) -> None:
        """API 呼び出し失敗。課金有無が不明（タイムアウト等）なら予約額を保守的に計上する。"""
        cost = reservation.amount_usd if billed_unknown else 0.0
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE usage_events SET status='error', cost_usd=?, cost_is_estimate_only=?, error=?"
                " WHERE id=?",
                (cost, 1 if billed_unknown else 0, error[:500], reservation.usage_event_id))
            conn.execute("DELETE FROM budget_reservations WHERE id=?", (reservation.reservation_id,))

    def recover_stale(self) -> int:
        """起動時に残っている予約（前回プロセスの異常終了）を予約額で計上して解除する。"""
        with self.db.tx() as conn:
            rows = conn.execute(
                "SELECT id, usage_event_id, amount_usd FROM budget_reservations").fetchall()
            for r in rows:
                conn.execute(
                    "UPDATE usage_events SET status='error', cost_usd=?, cost_is_estimate_only=1,"
                    " error='interrupted (process restart)' WHERE id=?",
                    (r["amount_usd"], r["usage_event_id"]))
            conn.execute("DELETE FROM budget_reservations")
        if rows:
            log.warning("stale_reservations_recovered", extra={"count": len(rows)})
        return len(rows)

    # --- 集計 ---
    def summary(self, top_users: int = 20) -> UsageSummary:
        conn = self.db.connect()
        day0, month0 = self.day_start_iso(), self.month_start_iso()
        st = ",".join("?" * len(COUNTED_STATUSES))
        t = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(cost_usd),0) FROM usage_events"
            f" WHERE created_at >= ? AND status IN ({st})", (day0, *COUNTED_STATUSES)).fetchone()
        m = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(cost_usd),0), COALESCE(SUM(input_tokens),0),"
            f" COALESCE(SUM(output_tokens),0), COALESCE(SUM(cache_creation_input_tokens),0),"
            f" COALESCE(SUM(cache_read_input_tokens),0), COALESCE(SUM(cost_is_estimate_only),0)"
            f" FROM usage_events WHERE created_at >= ? AND status IN ({st})",
            (month0, *COUNTED_STATUSES)).fetchone()
        rejected = conn.execute(
            "SELECT COUNT(*) FROM usage_events WHERE created_at >= ? AND status LIKE 'rejected_%'",
            (day0,)).fetchone()[0]
        reserved = float(conn.execute(
            "SELECT COALESCE(SUM(amount_usd),0) FROM budget_reservations").fetchone()[0])

        daily: dict[str, list] = {}
        for r in conn.execute(
                f"SELECT created_at, cost_usd FROM usage_events WHERE created_at >= ?"
                f" AND status IN ({st})", (month0, *COUNTED_STATUSES)):
            d = datetime.fromisoformat(r[0]).astimezone(self.tz).strftime("%Y-%m-%d")
            agg = daily.setdefault(d, [0, 0.0])
            agg[0] += 1
            agg[1] += float(r[1])

        users = [
            UserUsage(r[0], int(r[1]), int(r[2]), float(r[3]))
            for r in conn.execute(
                f"SELECT user_id, SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END), COUNT(*),"
                f" COALESCE(SUM(cost_usd),0) FROM usage_events WHERE created_at >= ?"
                f" AND status IN ({st}) GROUP BY user_id ORDER BY COUNT(*) DESC LIMIT ?",
                (day0, month0, *COUNTED_STATUSES, top_users))
        ]
        month_cost = float(m[1])
        budget = self.settings.monthly_budget_usd
        return UsageSummary(
            today_requests=int(t[0]), today_cost_usd=float(t[1]),
            month_requests=int(m[0]), month_cost_usd=month_cost,
            month_input_tokens=int(m[2]), month_output_tokens=int(m[3]),
            month_cache_creation_tokens=int(m[4]), month_cache_read_tokens=int(m[5]),
            month_estimate_only_count=int(m[6]), reserved_usd=reserved, budget_usd=budget,
            remaining_usd=max(0.0, budget - month_cost - reserved), rejected_today=int(rejected),
            daily=sorted((d, v[0], v[1]) for d, v in daily.items()), users=users,
        )

