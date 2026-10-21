"""障害・同時実行・再起動まわりの回帰テスト（Slack/Claude は偽物、SQLite は実ファイル）。"""

import asyncio
import json
import multiprocessing as mp
import sqlite3
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from classbot.app import Bot, check_database
from classbot.budget import BudgetManager, Rejection, Reservation, Usage
from classbot.config import Settings
from classbot.db import Database, DatabaseCorruptError
from classbot.healthcheck import check as health_check
from classbot.jobs import KIND_INGEST, KIND_QA, MAX_ATTEMPTS, JobQueue
from classbot.llm import ClaudeClient, LLMError, LLMResponse
from classbot.materials import MaterialStore
from classbot.pricing import PriceTable, cost_usd
from classbot.qa_worker import INTERNAL_ERROR, STALE_MESSAGE, QAWorker
from classbot.search import Searcher
from classbot.slack_app import SlackHandlers

REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


# ---------------------------------------------------------------- fakes
class FakeSlack:
    def __init__(self, fail_update=False, fail_post=False):
        self.posts, self.updates = [], []
        self.fail_update, self.fail_post = fail_update, fail_post

    async def chat_postMessage(self, **kw):
        if self.fail_post:
            raise RuntimeError("slack down")
        self.posts.append(kw)
        return {"ts": f"9{len(self.posts)}.0"}

    async def chat_update(self, **kw):
        if self.fail_update:
            raise RuntimeError("message_not_found")
        self.updates.append(kw)
        return {"ok": True}

    async def chat_postEphemeral(self, **kw):
        pass


class FakeLLM:
    def __init__(self, error=None):
        self.error, self.calls = error, 0

    async def answer(self, system, messages):
        self.calls += 1
        if self.error:
            raise self.error
        return LLMResponse(text="回答です。\n[[SOURCES: なし]]", stop_reason="end_turn",
                           model="claude-opus-5-5", usage=Usage(1000, 200), request_id="req_1",
                           iterations=[("claude-opus-5-5", Usage(1000, 200))])


class FakeMessages:
    def __init__(self, exc):
        self.exc = exc

    async def create(self, **kw):
        raise self.exc


def status_err(cls, code):
    return cls("err", response=httpx2.Response(code, request=REQ), body=None)


@pytest.fixture
def prices(settings):
    return PriceTable.load(settings.pricing_file)


@pytest.fixture
def bm(db, settings, prices):
    return BudgetManager(db, settings, prices)


def make_qa(settings, db, bm, llm, slack):
    q = QAWorker(settings, db, JobQueue(db), Searcher(db, settings), bm, llm, slack)
    q.retry_delay = 0.01
    return q


def qa_payload(ts="20.0", thread_ts=None, user="U1", text="第3回の課題は？"):
    return {"channel": "C123", "user": user, "ts": ts, "thread_ts": thread_ts or ts, "text": text}


def usage_rows(db):
    return db.query("SELECT * FROM usage_events ORDER BY id")


# ---------------------------------------------------------------- 予算（同時実行）
def _proc_reserve(kwargs, n, out):
    s = Settings(_env_file=None, **kwargs)
    db = Database(s.db_path)
    bm = BudgetManager(db, s, PriceTable.load(s.pricing_file))
    ok = 0
    for i in range(n):
        if isinstance(bm.reserve(f"U{i}", "C123", None, 2000), Reservation):
            ok += 1
    out.put(ok)
    db.close()


def test_budget_never_exceeded_across_processes(settings, db, prices):
    settings.monthly_budget_usd = 1.0
    kwargs = dict(materials_dir=str(settings.materials_dir), data_dir=str(settings.data_dir),
                  pricing_file=str(settings.pricing_file), claude_model=settings.claude_model,
                  monthly_budget_usd=1.0, daily_request_limit_per_user=1000)
    ctx = mp.get_context("spawn")
    out = ctx.Queue()
    procs = [ctx.Process(target=_proc_reserve, args=(kwargs, 8, out)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    granted = sum(out.get(timeout=5) for _ in procs)
    reserved = db.query_one("SELECT COALESCE(SUM(amount_usd),0) AS s FROM budget_reservations")["s"]
    per = BudgetManager(db, settings, prices).estimate_cost(2000)
    assert reserved <= 1.0 + 1e-9
    assert granted == int(1.0 // per)  # 予算内に収まる最大件数だけ許可される


def test_daily_limit_under_thread_concurrency(settings, prices):
    settings.daily_request_limit_per_user = 3
    results = []

    def worker():
        d = Database(settings.db_path)
        r = BudgetManager(d, settings, prices).reserve("U1", "C123", None, 100)
        results.append(isinstance(r, Reservation))
        d.close()

    Database(settings.db_path).init_schema()
    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 3


def test_month_boundary_asia_tokyo(bm):
    # 2026-10-31 15:30 UTC = 2026-11-01 00:30 JST → 11 月分
    now = datetime(2026, 10, 31, 15, 30, tzinfo=UTC)
    assert bm.month_start_iso(now) == "2026-10-31T15:00:00+00:00"
    assert bm.month_start_iso(datetime(2026, 10, 31, 14, 59, tzinfo=UTC)) == "2026-09-30T15:00:00+00:00"


def test_settle_actual_above_reservation_is_recorded(bm, db, settings, prices):
    settings.monthly_budget_usd = 1.0
    r = bm.reserve("U1", "C123", None, 10)
    # 予約見積もりより大きな実使用量（推定入力が過小だった場合）
    cost = bm.settle(r, "claude-opus-5-5", Usage(200_000, 8000))
    assert cost > r.amount_usd
    assert db.query_one("SELECT cost_usd FROM usage_events WHERE id=?", (r.usage_event_id,))[0] == pytest.approx(cost)
    # 実際の消費額が次の予約判定に反映される
    s = bm.summary()
    assert s.month_cost_usd == pytest.approx(cost) and s.reserved_usd == 0


def test_settle_sums_fallback_iterations_by_model(bm, prices):
    r = bm.reserve("U1", "C123", None, 1000)
    its = [("claude-opus-5-5", Usage(1000, 50)), ("claude-fable-5-1", Usage(1200, 300))]
    total = Usage(2200, 350)
    cost = bm.settle(r, "claude-fable-5-1", total, "req", its)
    expected = (cost_usd(prices.price_for("claude-opus-5-5"), 1000, 50)
                + cost_usd(prices.price_for("claude-fable-5-1"), 1200, 300))
    assert cost == pytest.approx(expected)
    row = bm.db.query_one("SELECT * FROM usage_events WHERE id=?", (r.usage_event_id,))
    assert (row["input_tokens"], row["output_tokens"], row["model"]) == (2200, 350, "claude-fable-5-1")


def test_settle_unknown_iteration_model_priced_conservatively(bm, prices):
    r = bm.reserve("U1", "C123", None, 1000)
    cost = bm.settle(r, "x", Usage(1000, 100), None, [("claude-mystery", Usage(1000, 100))])
    assert cost == pytest.approx(cost_usd(prices.conservative(), 1000, 100))


def test_fallback_possible_reserves_second_attempt(db, settings, prices):
    plain = BudgetManager(db, settings, prices).estimate_cost(5000)
    fb = BudgetManager(db, settings, prices, fallback_possible=True).estimate_cost(5000)
    second = BudgetManager(db, settings, prices)._attempt_cost(prices.conservative(), 5000)
    assert fb == pytest.approx(plain + second) and fb > 2 * plain - 1e-12


def test_bot_passes_fallback_flag_to_budget(settings):
    settings.claude_refusal_fallback = True
    bot = Bot(settings)
    assert bot.budget.fallback_possible is bot.llm.use_fallback
    settings.claude_refusal_fallback = False
    assert Bot(settings).budget.fallback_possible is False


# ---------------------------------------------------------------- API 障害
@pytest.mark.parametrize("err,billed", [
    (LLMError("timeout", "時間切れ", billed_unknown=True, detail="APITimeoutError"), True),
    (LLMError("rate_limit", "混雑", detail="429"), False),
    (LLMError("auth", "設定エラー", detail="401"), False),
])
async def test_llm_errors_settle_budget(settings, db, bm, err, billed):
    slack = FakeSlack()
    qa = make_qa(settings, db, bm, FakeLLM(err), slack)
    qa.queue.enqueue(KIND_QA, qa_payload())
    await qa.handle(qa.queue.claim(KIND_QA))
    assert slack.updates[-1]["text"] == err.user_message
    row = usage_rows(db)[-1]
    assert row["status"] == "error" and bool(row["cost_is_estimate_only"]) is billed
    assert (row["cost_usd"] > 0) is billed
    assert bm.summary().reserved_usd == 0


@pytest.mark.parametrize("exc,billed", [
    (anthropic.APITimeoutError(request=REQ), True),
    (anthropic.APIConnectionError(request=REQ), False),
    (status_err(anthropic.RateLimitError, 429), False),
    (status_err(anthropic.InternalServerError, 500), False),
    (status_err(anthropic.AuthenticationError, 401), False),
])
async def test_real_sdk_exceptions_through_worker(settings, db, bm, exc, billed):
    fake = SimpleNamespace(messages=FakeMessages(exc), beta=SimpleNamespace(messages=FakeMessages(exc)))
    slack = FakeSlack()
    qa = make_qa(settings, db, bm, ClaudeClient(settings, fake), slack)
    qa.queue.enqueue(KIND_QA, qa_payload())
    stop = asyncio.Event()
    wake = asyncio.Event()
    task = asyncio.create_task(qa.run(wake, stop))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if db.query_one("SELECT 1 FROM jobs WHERE status='done'"):
            break
    stop.set()
    wake.set()
    await asyncio.wait_for(task, 10)
    # 利用者に理由が伝わり、ジョブは完了扱い（自動再試行で二重回答しない）
    assert slack.updates and slack.updates[-1]["text"] not in ("", INTERNAL_ERROR)
    assert db.query_one("SELECT status FROM jobs")[0] == "done"
    row = usage_rows(db)[-1]
    assert row["status"] == "error" and bool(row["cost_is_estimate_only"]) is billed
    assert bm.summary().reserved_usd == 0


async def test_chat_update_failure_falls_back_to_post(settings, db, bm):
    slack = FakeSlack(fail_update=True)
    qa = make_qa(settings, db, bm, FakeLLM(), slack)
    qa.queue.enqueue(KIND_QA, qa_payload())
    await qa.handle(qa.queue.claim(KIND_QA))
    assert "回答です" in slack.posts[-1]["text"]  # 回答は失われない
    assert usage_rows(db)[-1]["status"] == "ok"
    assert db.query_one("SELECT delivered FROM qa_progress")[0] == 1


async def test_slack_outage_retries_delivery_without_new_api_call(settings, db, bm):
    slack = FakeSlack()
    llm = FakeLLM()
    qa = make_qa(settings, db, bm, llm, slack)
    qa.queue.enqueue(KIND_QA, qa_payload())
    stop = asyncio.Event()
    # プレースホルダー投稿後に Slack が落ちる
    orig = slack.chat_postMessage

    async def post_then_fail(**kw):
        r = await orig(**kw)
        slack.fail_update = slack.fail_post = True
        return r

    slack.chat_postMessage = post_then_fail
    wake = asyncio.Event()
    task = asyncio.create_task(qa.run(wake, stop))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if db.query_one("SELECT answer_text FROM qa_progress WHERE answer_text IS NOT NULL"):
            break
    slack.chat_postMessage = orig
    slack.fail_update = slack.fail_post = False  # Slack 復旧
    for _ in range(200):
        await asyncio.sleep(0.01)
        if db.query_one("SELECT 1 FROM jobs WHERE status='done'"):
            break
    stop.set()
    wake.set()
    await asyncio.wait_for(task, 10)
    assert llm.calls == 1 and len(usage_rows(db)) == 1
    assert "回答です" in slack.updates[-1]["text"]


# ---------------------------------------------------------------- 再起動
async def test_restart_after_answer_saved_does_not_call_api_again(settings, db, bm):
    q = JobQueue(db)
    q.enqueue(KIND_QA, qa_payload())
    q.claim(KIND_QA)  # 前回プロセスで running のまま停止
    db.execute("INSERT INTO qa_progress(job_key, placeholder_ts, answer_text, updated_at) "
               "VALUES ('C123:20.0', '91.0', '保存済みの回答', '2026-10-20T00:00:00+00:00')")
    assert q.requeue_running() == 1
    slack, llm = FakeSlack(), FakeLLM()
    qa = make_qa(settings, db, bm, llm, slack)
    await qa.handle(q.claim(KIND_QA))
    assert llm.calls == 0 and slack.updates[-1] == {"channel": "C123", "ts": "91.0", "text": "保存済みの回答"}
    # 送信済みなら次の再実行では何もしない
    q.enqueue(KIND_QA, qa_payload())
    await qa.handle(q.claim(KIND_QA))
    assert len(slack.updates) == 1 and not slack.posts


async def test_restart_after_placeholder_reuses_it(settings, db, bm):
    db.execute("INSERT INTO qa_progress(job_key, placeholder_ts, updated_at) "
               "VALUES ('C123:20.0', '77.0', '2026-10-20T00:00:00+00:00')")
    slack = FakeSlack()
    qa = make_qa(settings, db, bm, FakeLLM(), slack)
    qa.queue.enqueue(KIND_QA, qa_payload())
    await qa.handle(qa.queue.claim(KIND_QA))
    assert not slack.posts and slack.updates[-1]["ts"] == "77.0"


async def test_stale_job_not_answered(settings, db, bm):
    slack, llm = FakeSlack(), FakeLLM()
    qa = make_qa(settings, db, bm, llm, slack)
    qa.queue.enqueue(KIND_QA, qa_payload())
    db.execute("UPDATE jobs SET created_at='2020-01-01T00:00:00+00:00'")
    await qa.handle(qa.queue.claim(KIND_QA))
    assert llm.calls == 0 and slack.posts[-1]["text"] == STALE_MESSAGE
    assert usage_rows(db) == []


def test_requeue_running_fails_crash_looping_job(db):
    q = JobQueue(db)
    q.enqueue(KIND_INGEST, {"material_id": 1})
    for _ in range(MAX_ATTEMPTS):
        q.claim(KIND_INGEST)
        q.requeue_running()
    assert db.query_one("SELECT status, last_error FROM jobs")[0] == "failed"


async def test_bot_recover_and_task_supervision(settings, db, bm):
    q = JobQueue(db)
    q.enqueue(KIND_QA, qa_payload())
    q.claim(KIND_QA)
    r = bm.reserve("U1", "C123", None, 100)
    db.execute("INSERT INTO materials(source, rel_path, display_name, file_type, created_at, updated_at, "
               "active_version) VALUES ('server','a.txt','a.txt','txt','x','x',1)")
    db.execute("INSERT INTO chunks(material_id, version, seq, location, text) VALUES (1, 2, 0, 'L1', 'orphan')")
    bot = Bot(settings)
    bot.recover()
    assert db.query_one("SELECT status FROM jobs")[0] == "pending"
    assert db.query_one("SELECT COUNT(*) FROM chunks")[0] == 0
    row = db.query_one("SELECT * FROM usage_events WHERE id=?", (r.usage_event_id,))
    assert row["cost_is_estimate_only"] == 1 and row["cost_usd"] == pytest.approx(r.amount_usd)

    async def boom():
        raise RuntimeError("worker died")

    t = asyncio.create_task(boom())
    t.add_done_callback(bot._on_task_done)
    await asyncio.sleep(0.01)
    assert bot.stop.is_set() and bot.exit_code == 1


# ---------------------------------------------------------------- SQLite
async def test_worker_survives_db_lock(settings, bm):
    db = Database(settings.db_path, busy_timeout_ms=100)
    db.init_schema()
    slack = FakeSlack()
    qa = make_qa(settings, db, bm, FakeLLM(), slack)
    qa.queue.enqueue(KIND_QA, qa_payload())
    locker = sqlite3.connect(settings.db_path, isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")  # 別プロセス（CLI 等）が長時間書き込みロックを保持
    stop = asyncio.Event()
    wake = asyncio.Event()
    task = asyncio.create_task(qa.run(wake, stop))
    await asyncio.sleep(1.5)
    assert not task.done()  # ロック中もワーカーは停止しない
    locker.execute("COMMIT")
    for _ in range(200):
        await asyncio.sleep(0.02)
        if slack.updates:
            break
    stop.set()
    wake.set()
    await asyncio.wait_for(task, 10)
    assert "回答です" in slack.updates[-1]["text"]
    locker.close()


def test_reads_not_blocked_by_writer_in_wal(settings, db):
    locker = sqlite3.connect(settings.db_path, isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")
    locker.execute("INSERT INTO meta(key, value) VALUES ('x', '1')")
    t0 = time.time()
    assert db.query_one("SELECT COUNT(*) FROM materials")[0] == 0
    assert time.time() - t0 < 1
    locker.execute("COMMIT")
    locker.close()


def test_tx_does_not_mask_error_after_implicit_rollback(db):
    with pytest.raises(ValueError):
        with db.tx() as conn:
            conn.execute("ROLLBACK")  # SQLite が自動ロールバックした状況を再現
            raise ValueError("original")
    with db.tx() as conn:  # 接続は引き続き使える
        conn.execute("INSERT INTO meta(key, value) VALUES ('ok', '1')")


def test_corrupted_database_detected(settings, caplog):
    settings.db_path.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(DatabaseCorruptError):
        Database(settings.db_path).quick_check()
    assert check_database(settings) is False
    assert "data/backups" in caplog.text


def test_missing_database_is_ok(settings):
    assert check_database(settings) is True


# ---------------------------------------------------------------- ヘルスチェック
def test_healthcheck(settings):
    def write(**kw):
        settings.heartbeat_file.write_text(json.dumps(kw))

    assert health_check(settings)[0] is False  # heartbeat なし
    now = time.time()
    write(ts=now, slack_connected=True, slack_connected_changed=now)
    assert health_check(settings) == (True, "ok")
    write(ts=now - settings.heartbeat_interval_sec * 5, slack_connected=True)
    assert health_check(settings)[0] is False
    write(ts=now, slack_connected=False, slack_connected_changed=now - 60)
    assert health_check(settings)[0] is True  # 短時間の切断は再接続を待つ
    write(ts=now, slack_connected=False,
          slack_connected_changed=now - settings.socket_disconnect_exit_sec - 1)
    assert health_check(settings)[0] is False


# ---------------------------------------------------------------- Slack 再送
async def test_event_resend_deduped_across_restart(settings, db, bm):
    def handlers(d):
        rt = {"bot_user_id": "UBOT", "wake": lambda: None}
        return SlackHandlers(settings, d, JobQueue(d), MaterialStore(d, settings),
                             BudgetManager(d, settings, PriceTable.load(settings.pricing_file)), rt)

    ev = {"channel": "C123", "user": "U1", "ts": "5.0", "text": "<@UBOT> 質問です"}
    await handlers(db).on_app_mention(ev, FakeSlack())
    db.close()
    db2 = Database(settings.db_path)  # プロセス再起動相当
    await handlers(db2).on_app_mention(ev, FakeSlack())  # Slack の再送
    assert db2.query_one("SELECT COUNT(*) FROM jobs WHERE kind='qa'")[0] == 1
    JobQueue(db2).purge_old()  # 直近のキーは残る
    assert db2.query_one("SELECT COUNT(*) FROM processed_events")[0] == 1
    db2.close()


async def test_model_output_mentions_are_neutralized(settings, db, bm):
    llm = FakeLLM()

    async def answer(system, messages):
        return LLMResponse(text="<!channel> 締切は<@U999>へ <https://evil.example|公式>\n[[SOURCES: なし]]",
                           stop_reason="end_turn", model="claude-opus-5-5", usage=Usage(10, 10),
                           request_id=None, iterations=[("claude-opus-5-5", Usage(10, 10))])

    llm.answer = answer
    slack = FakeSlack()
    qa = make_qa(settings, db, bm, llm, slack)
    qa.queue.enqueue(KIND_QA, qa_payload())
    await qa.handle(qa.queue.claim(KIND_QA))
    out = slack.updates[-1]["text"]
    assert "&lt;!channel&gt;" in out and "<!channel>" not in out
    assert "<@U999>" not in out and "<https://evil.example|" not in out
