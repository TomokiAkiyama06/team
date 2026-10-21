"""W2: W3 指摘（ワーカーループの耐障害性・イベントループを止めない DB アクセス・受付の原子性）の回帰テスト。"""

import asyncio
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from classbot.budget import BudgetManager
from classbot.indexer import Indexer
from classbot.jobs import KIND_QA, JobQueue
from classbot.material_worker import MaterialWorker
from classbot.materials import MaterialStore
from classbot.pricing import PriceTable
from classbot.slack_app import SlackHandlers
from tests.test_w2_events_sync import BOT, FakeSlack, app_mention


@pytest.fixture
def env(settings, db):
    queue = JobQueue(db)
    store = MaterialStore(db, settings)
    budget = BudgetManager(db, settings, PriceTable.load(settings.pricing_file))
    h = SlackHandlers(settings, db, queue, store, budget, {"bot_user_id": BOT, "wake": lambda: None})
    return SimpleNamespace(s=settings, db=db, queue=queue, store=store, h=h, slack=FakeSlack())


async def test_material_worker_survives_claim_errors(env):
    worker = MaterialWorker(env.s, env.db, env.queue, env.store, Indexer(env.db, env.s, env.store), env.slack)
    worker.retry_delay = 0.01
    calls = {"n": 0}
    stop, wake = asyncio.Event(), asyncio.Event()

    def flaky_claim(kinds):
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        stop.set()
        return None  # 次の待機は wait_for のタイムアウト（5秒）前に stop で抜ける

    env.queue.claim = flaky_claim
    await asyncio.wait_for(worker.run(wake, stop), timeout=10)
    assert calls["n"] >= 3


async def test_handlers_do_not_block_event_loop_while_db_locked(env):
    locker = sqlite3.connect(env.s.db_path, isolation_level=None, check_same_thread=False)
    locker.execute("BEGIN IMMEDIATE")
    released = threading.Event()

    def release():
        released.wait(0.6)
        locker.execute("COMMIT")
        locker.close()

    threading.Thread(target=release).start()
    ticks = 0

    async def ticker():
        nonlocal ticks
        while not released.is_set():
            await asyncio.sleep(0.02)
            ticks += 1

    t = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    handling = asyncio.create_task(env.h.on_app_mention(app_mention("質問", "1.0", user="U1"), env.slack))
    await asyncio.sleep(0.4)
    released.set()
    await handling
    await t
    assert ticks >= 10  # 書き込みロック待ちの間もイベントループは動き続ける
    assert env.queue.claim(KIND_QA) is not None


async def test_accept_is_atomic_with_dedupe(env, monkeypatch):
    original = env.queue.enqueue

    def crash(*a, **k):
        raise RuntimeError("process died")

    monkeypatch.setattr(env.queue, "enqueue", crash)
    with pytest.raises(RuntimeError):
        await env.h.on_app_mention(app_mention("質問", "2.0", user="U1"), env.slack)
    # 処理済み記録もスレッド登録もロールバックされ、再送で受け付けられる
    assert env.db.query_one("SELECT 1 FROM processed_events WHERE event_key='C123:2.0'") is None
    assert env.db.query_one("SELECT 1 FROM threads WHERE thread_ts='2.0'") is None
    monkeypatch.setattr(env.queue, "enqueue", original)
    await env.h.on_app_mention(app_mention("質問", "2.0", user="U1"), env.slack)
    assert env.queue.claim(KIND_QA).payload["text"] == "質問"
