"""W2: W4 セキュリティ指摘（実行時の管理者再検証・Slack 書式の無害化・ワークスペース制限）の回帰テスト。"""

from types import SimpleNamespace

import pytest

from classbot.budget import BudgetManager
from classbot.indexer import Indexer
from classbot.jobs import KIND_DELETE, KIND_INGEST, KIND_QA, KIND_REINDEX_ALL, JobQueue
from classbot.material_worker import MaterialWorker
from classbot.materials import MaterialStore
from classbot.pricing import PriceTable
from classbot.slack_app import SlackHandlers
from classbot.slack_format import delete_button, list_blocks, material_line
from tests.test_w2_events_sync import ADMIN, BOT, CH, FakeSlack, app_mention


@pytest.fixture
def env(settings, db):
    queue = JobQueue(db)
    store = MaterialStore(db, settings)
    budget = BudgetManager(db, settings, PriceTable.load(settings.pricing_file))
    h = SlackHandlers(settings, db, queue, store, budget, {"bot_user_id": BOT, "wake": lambda: None})
    slack = FakeSlack()
    worker = MaterialWorker(settings, db, queue, store, Indexer(db, settings, store), slack)
    return SimpleNamespace(s=settings, db=db, queue=queue, store=store, h=h, slack=slack, worker=worker)


def _server(env, name):
    p = env.s.materials_dir / name
    p.write_text("内容", encoding="utf-8")
    return env.store.upsert_server(name, p.stat().st_size, p.stat().st_mtime, "md"), p


@pytest.mark.parametrize("kind", [KIND_DELETE, KIND_INGEST, KIND_REINDEX_ALL])
async def test_material_jobs_reverify_admin_at_execution(env, kind):
    mid, p = _server(env, "a.md")
    env.s.slack_admin_user_ids = ["UNEW"]  # 受付後に管理者から外された
    env.queue.enqueue(kind, {"material_id": mid, "user": ADMIN, "force": True,
                             "notify": {"channel": CH, "thread_ts": "1.0"}})
    await env.worker.handle(env.queue.claim(kind))
    assert env.slack.posts[-1]["text"] == "この操作は管理者のみ実行できます。"
    assert env.store.get(mid) is not None and p.exists()
    assert env.store.get(mid)["status"] == "pending"  # インデックスも実行されない


async def test_delete_job_without_user_is_rejected(env):
    mid, _ = _server(env, "a.md")
    env.queue.enqueue(KIND_DELETE, {"material_id": mid, "notify": {"channel": CH, "thread_ts": "1"}})
    await env.worker.handle(env.queue.claim(KIND_DELETE))
    assert env.store.get(mid) is not None


async def test_reindex_command_payload_carries_user(env):
    mid, _ = _server(env, "a.md")
    await env.h.on_app_mention(app_mention(f"再インデックス {mid}", "5.0"), env.slack)
    await env.h.on_app_mention(app_mention("再インデックス", "6.0"), env.slack)
    jobs = {j.kind: j.payload for j in (env.queue.claim(KIND_INGEST), env.queue.claim(KIND_REINDEX_ALL))}
    assert jobs[KIND_INGEST]["user"] == ADMIN and jobs[KIND_REINDEX_ALL]["user"] == ADMIN


async def test_untrusted_names_are_escaped_in_slack_output(env):
    mid, _ = _server(env, "<!channel>‮悪意.md")
    row = env.store.get(mid)
    assert "‮" not in row["display_name"]  # 不可視・方向制御文字を除去
    line = material_line(row, "Asia/Tokyo")
    assert "<!channel>" not in line and "&lt;!channel&gt;" in line
    assert "<!channel>" not in delete_button(row)["confirm"]["text"]["text"]
    assert "<!channel>" not in str(list_blocks([row], 1, 1, 15, "Asia/Tokyo"))
    env.queue.enqueue(KIND_DELETE, {"material_id": mid, "user": ADMIN,
                                    "notify": {"channel": CH, "thread_ts": "1.0"}})
    await env.worker.handle(env.queue.claim(KIND_DELETE))
    assert "<!channel>" not in env.slack.posts[-1]["text"]


@pytest.mark.parametrize("team_id,expect", [("", 2), ("T1", 1)])
async def test_optional_team_restriction(env, team_id, expect):
    env.s.slack_team_id = team_id
    await env.h.on_app_mention(app_mention("質問", "10.0", user="U1"), env.slack)  # team=T1
    ext = app_mention("質問", "11.0", user="UEXT")
    ext.update({"team": "TOTHER", "user_team": "TOTHER"})
    await env.h.on_app_mention(ext, env.slack)
    assert env.db.query_one("SELECT COUNT(*) n FROM jobs WHERE kind=?", (KIND_QA,))["n"] == expect
    # ボタン操作も同様
    mid, _ = _server(env, "b.md")
    await env.h.on_action({"user": {"id": ADMIN, "team_id": "TOTHER"}, "team": {"id": "TOTHER"},
                           "channel": {"id": CH}, "container": {"message_ts": "5.0"},
                           "actions": [{"action_id": "delete_material", "value": str(mid)}]}, env.slack)
    queued = env.queue.claim(KIND_DELETE) is not None
    assert queued == (team_id == "")
