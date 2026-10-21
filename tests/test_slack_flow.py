"""Slack 受付 → ジョブ → ワーカーの結合テスト（Slack/Claude は偽物）。"""

from types import SimpleNamespace

import pytest

from classbot import material_worker as mw
from classbot.budget import BudgetManager, Usage
from classbot.indexer import Indexer
from classbot.jobs import KIND_DELETE, KIND_QA, KIND_SLACK_UPLOAD, JobQueue
from classbot.llm import LLMError, LLMResponse
from classbot.material_worker import MaterialWorker
from classbot.materials import MaterialStore
from classbot.pricing import PriceTable
from classbot.qa_worker import QAWorker
from classbot.search import Searcher
from classbot.slack_app import ADMIN_ONLY_MSG, SlackHandlers

BOT = "UBOT"


class FakeSlack:
    def __init__(self):
        self.posts, self.updates, self.ephemerals = [], [], []
        self.files = {}

    async def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": f"9{len(self.posts)}.0"}

    async def chat_update(self, **kw):
        self.updates.append(kw)
        return {"ok": True}

    async def chat_postEphemeral(self, **kw):
        self.ephemerals.append(kw)

    async def files_info(self, file):
        return {"file": self.files[file]}


class FakeLLM:
    def __init__(self, text="第3回の課題はレポートです[S1]。\n[[SOURCES: S1, S9]]", error=None):
        self.text, self.error, self.calls = text, error, []

    async def answer(self, system, messages):
        self.calls.append((system, messages))
        if self.error:
            raise self.error
        return LLMResponse(text=self.text, stop_reason="end_turn", model="claude-opus-5-5",
                           usage=Usage(1000, 200, 0, 0), request_id="req_1")


@pytest.fixture
def env(settings, db):
    queue = JobQueue(db)
    store = MaterialStore(db, settings)
    budget = BudgetManager(db, settings, PriceTable.load(settings.pricing_file))
    wakes = []
    runtime = {"bot_user_id": BOT, "wake": lambda: wakes.append(1)}
    h = SlackHandlers(settings, db, queue, store, budget, runtime)
    return SimpleNamespace(s=settings, db=db, queue=queue, store=store, budget=budget, h=h,
                           slack=FakeSlack(), indexer=Indexer(db, settings, store))


def mention(text, user="U1", ts="1.0", channel="C123", **kw):
    return {"type": "app_mention", "channel": channel, "user": user, "ts": ts,
            "text": f"<@{BOT}> {text}", **kw}


async def test_question_flow_and_filters(env):
    await env.h.on_app_mention(mention("第3回の課題は？", channel="COTHER"), env.slack)
    assert env.queue.claim(KIND_QA) is None and not env.slack.posts  # 他チャンネルは無視

    ev = mention("第3回の課題は？")
    await env.h.on_app_mention(ev, env.slack)
    await env.h.on_app_mention(ev, env.slack)  # 再送
    await env.h.on_message({**ev, "type": "message"}, env.slack)  # 二重配送
    job = env.queue.claim(KIND_QA)
    assert job.payload["text"] == "第3回の課題は？" and job.payload["thread_ts"] == "1.0"
    assert env.queue.claim(KIND_QA) is None

    # ボットのスレッドではメンションなしの追加質問を受け付ける（他人も可）
    await env.h.on_message({"channel": "C123", "user": "U2", "ts": "2.0", "thread_ts": "1.0",
                            "text": "締切は？"}, env.slack)
    assert env.queue.claim(KIND_QA).payload["user"] == "U2"
    # 無関係なスレッド・通常投稿には反応しない
    await env.h.on_message({"channel": "C123", "user": "U2", "ts": "3.0", "thread_ts": "0.5",
                            "text": "雑談"}, env.slack)
    await env.h.on_message({"channel": "C123", "user": "U2", "ts": "4.0", "text": "雑談"}, env.slack)
    assert env.queue.claim(KIND_QA) is None


async def test_admin_commands_require_admin(env):
    await env.h.on_app_mention(mention("資料削除 1"), env.slack)
    assert env.slack.posts[-1]["text"] == ADMIN_ONLY_MSG
    await env.h.on_app_mention(mention("ヘルプ", ts="1.1"), env.slack)
    assert "使い方" in env.slack.posts[-1]["text"]
    # ボタン操作もサーバー側で管理者を再検証する
    await env.h.on_action({"user": {"id": "U1"}, "channel": {"id": "C123"},
                           "container": {"message_ts": "5.0"},
                           "actions": [{"action_id": "delete_material", "value": "1"}]}, env.slack)
    assert env.slack.ephemerals and env.queue.claim(KIND_DELETE) is None


async def test_upload_with_separate_events(env):
    # 1) メンションが先、添付が後
    await env.h.on_app_mention(mention("資料登録", user="UADMIN", ts="10.0"), env.slack)
    assert "添付" in env.slack.posts[-1]["text"]
    await env.h.on_message({"channel": "C123", "user": "UADMIN", "ts": "11.0", "subtype": "file_share",
                            "files": [{"id": "F1"}], "text": ""}, env.slack)
    job = env.queue.claim(KIND_SLACK_UPLOAD)
    assert job.payload["file_ids"] == ["F1"] and job.payload["thread_ts"] == "10.0"
    # 2) 添付が先、メンションが後
    await env.h.on_message({"channel": "C123", "user": "UADMIN", "ts": "12.0", "subtype": "file_share",
                            "files": [{"id": "F2"}], "text": ""}, env.slack)
    assert env.queue.claim(KIND_SLACK_UPLOAD) is None
    await env.h.on_app_mention(mention("資料登録", user="UADMIN", ts="13.0"), env.slack)
    assert env.queue.claim(KIND_SLACK_UPLOAD).payload["file_ids"] == ["F2"]
    # 学生の添付は無視
    await env.h.on_message({"channel": "C123", "user": "U1", "ts": "14.0", "subtype": "file_share",
                            "files": [{"id": "F3"}], "text": ""}, env.slack)
    assert env.db.query_one("SELECT 1 FROM recent_admin_files WHERE file_id='F3'") is None


async def test_material_worker_upload_and_qa(env, monkeypatch):
    env.slack.files["F1"] = {"name": "../第03回_演習問題.md", "size": 200, "mode": "hosted",
                             "url_private_download": "https://files.slack.com/x"}

    async def fake_download(self, url, dest):
        dest.write_text("# 第3回 課題\n第3回の課題はレポート提出です。締切は資料に記載なし。\n", encoding="utf-8")

    monkeypatch.setattr(mw.MaterialWorker, "_download", fake_download)
    worker = MaterialWorker(env.s, env.db, env.queue, env.store, env.indexer, env.slack)
    env.queue.enqueue(KIND_SLACK_UPLOAD, {"channel": "C123", "user": "UADMIN", "thread_ts": "1.0",
                                          "file_ids": ["F1"], "target_material_id": None})
    await worker.handle(env.queue.claim(KIND_SLACK_UPLOAD))
    assert "登録しました" in env.slack.posts[-1]["text"], env.slack.posts
    row = env.db.query_one("SELECT * FROM materials")
    assert row["display_name"] == "第03回_演習問題.md" and row["source"] == "slack"
    assert (env.s.uploads_dir / row["stored_name"]).is_file()  # materials/ とは別領域

    llm = FakeLLM()
    qa = QAWorker(env.s, env.db, env.queue, Searcher(env.db, env.s), env.budget, llm, env.slack)
    env.h._mark_thread("C123", "20.0")
    env.queue.enqueue(KIND_QA, {"channel": "C123", "user": "U1", "ts": "20.0", "thread_ts": "20.0",
                                "text": "第3回の課題について教えて"})
    await qa.handle(env.queue.claim(KIND_QA))
    final = env.slack.updates[-1]["text"]
    assert "出典：第03回_演習問題.md" in final and "S9" not in final
    sys_prompt, messages = llm.calls[0]
    assert "<search_results" in messages[-1]["content"] and "S1" in messages[-1]["content"]
    assert env.budget.summary().month_requests == 1

    # 追加質問は履歴を引き継ぐ
    env.queue.enqueue(KIND_QA, {"channel": "C123", "user": "U2", "ts": "21.0", "thread_ts": "20.0",
                                "text": "締切は？"})
    await qa.handle(env.queue.claim(KIND_QA))
    roles = [m["role"] for m in llm.calls[1][1]]
    assert roles == ["user", "assistant", "user"]


async def test_qa_llm_error_releases_budget(env):
    llm = FakeLLM(error=LLMError("rate_limit", "混雑しています", detail="429"))
    qa = QAWorker(env.s, env.db, env.queue, Searcher(env.db, env.s), env.budget, llm, env.slack)
    env.queue.enqueue(KIND_QA, {"channel": "C123", "user": "U1", "ts": "30.0", "thread_ts": "30.0",
                                "text": "質問"})
    await qa.handle(env.queue.claim(KIND_QA))
    assert env.slack.updates[-1]["text"] == "混雑しています"
    s = env.budget.summary()
    assert s.reserved_usd == 0 and s.month_cost_usd == 0
