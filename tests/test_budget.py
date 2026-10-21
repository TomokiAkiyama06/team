import threading
from datetime import UTC, datetime

import pytest

from classbot.budget import BudgetManager, Rejection, Reservation, Usage
from classbot.db import Database
from classbot.pricing import PriceTable, PricingError


@pytest.fixture
def bm(db, settings):
    return BudgetManager(db, settings, PriceTable.load(settings.pricing_file))


def test_unknown_model_raises(db, settings):
    settings.claude_model = "claude-unknown"
    with pytest.raises(PricingError):
        BudgetManager(db, settings, PriceTable.load(settings.pricing_file))


def test_daily_limit(bm, settings):
    settings.daily_request_limit_per_user = 2
    for _ in range(2):
        r = bm.reserve("U1", "C123", "1.0", 1000)
        assert isinstance(r, Reservation)
        bm.settle(r, "claude-opus-5-5", Usage(100, 100))
    r = bm.reserve("U1", "C123", "1.0", 1000)
    assert isinstance(r, Rejection) and r.reason == "daily"
    assert bm.check_daily("U1", False) == (False, 2, 2)
    # 管理者は既定で日次上限の対象外
    assert bm.check_daily("U1", True)[0] is True
    assert isinstance(bm.reserve("U2", "C123", None, 1000), Reservation)


def test_concurrent_reservations_respect_budget(settings):
    settings.monthly_budget_usd = 1.0
    settings.claude_max_tokens = 8000  # 予約 ≈ 8000*20/1M = 0.16 + 入力分
    settings.daily_request_limit_per_user = 1000
    db = Database(settings.db_path)
    db.init_schema()
    prices = PriceTable.load(settings.pricing_file)
    results = []
    lock = threading.Lock()

    def worker(i):
        local = BudgetManager(db, settings, prices)
        r = local.reserve(f"U{i}", "C123", None, 2000)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok = [r for r in results if isinstance(r, Reservation)]
    assert ok and len(ok) < 20
    assert sum(r.amount_usd for r in ok) <= 1.0
    assert all(r.reason == "budget" for r in results if isinstance(r, Rejection))


def test_settle_and_fail(bm, db):
    r = bm.reserve("U1", "C123", None, 1000)
    cost = bm.settle(r, "claude-opus-5-5", Usage(1_000_000, 0, 0, 0), "req_1")
    assert cost == pytest.approx(4.0)
    row = db.query_one("SELECT * FROM usage_events WHERE id=?", (r.usage_event_id,))
    assert row["status"] == "ok" and row["request_id"] == "req_1"
    assert db.query_one("SELECT COUNT(*) c FROM budget_reservations")["c"] == 0

    r2 = bm.reserve("U1", "C123", None, 1000)
    bm.fail(r2, "timeout", billed_unknown=True)
    row = db.query_one("SELECT * FROM usage_events WHERE id=?", (r2.usage_event_id,))
    assert row["status"] == "error" and row["cost_is_estimate_only"] == 1
    assert row["cost_usd"] == pytest.approx(r2.amount_usd)

    r3 = bm.reserve("U1", "C123", None, 1000)
    bm.fail(r3, "bad request")
    assert db.query_one("SELECT cost_usd FROM usage_events WHERE id=?", (r3.usage_event_id,))[0] == 0


def test_unknown_served_model_priced_conservatively(bm):
    r = bm.reserve("U1", "C123", None, 10)
    assert bm.settle(r, "claude-mystery", Usage(0, 1_000_000)) == pytest.approx(50.0)


def test_recover_stale(bm, db):
    r = bm.reserve("U1", "C123", None, 1000)
    assert bm.recover_stale() == 1
    row = db.query_one("SELECT * FROM usage_events WHERE id=?", (r.usage_event_id,))
    assert row["status"] == "error" and row["cost_usd"] == pytest.approx(r.amount_usd)
    s = bm.summary()
    assert s.month_estimate_only_count == 1 and s.reserved_usd == 0


def test_timezone_boundaries(bm):
    # 2026-10-20 14:59:59 UTC = 23:59:59 JST → 当日の開始は 2026-10-19T15:00 UTC
    now = datetime(2026, 10, 20, 14, 59, 59, tzinfo=UTC)
    assert bm.day_start_iso(now) == "2026-10-19T15:00:00+00:00"
    now2 = datetime(2026, 10, 20, 15, 0, 0, tzinfo=UTC)  # JST 10/21 0:00
    assert bm.day_start_iso(now2) == "2026-10-20T15:00:00+00:00"
    assert bm.month_start_iso(datetime(2026, 10, 31, 16, 0, tzinfo=UTC)) == "2026-10-31T15:00:00+00:00"


def test_summary(bm):
    r = bm.reserve("U1", "C123", None, 1000)
    bm.settle(r, "claude-opus-5-5", Usage(1000, 500, 200, 300))
    bm.reserve("U2", "C123", None, 1000)
    s = bm.summary()
    assert s.today_requests == 2 and s.month_requests == 2
    assert s.month_cache_read_tokens == 300 and s.reserved_usd > 0
    assert {u.user_id for u in s.users} == {"U1", "U2"}
    assert s.remaining_usd < s.budget_usd and len(s.daily) == 1
