"""利用者の制限（SLACK_RESTRICT_USERS）のテスト。チャンネルを公開にしても、登録した人だけが使える。"""

import pytest

from classbot.access import NOT_ALLOWED_MSG, AccessList, user_ids_in
from classbot.jobs import KIND_QA
from classbot.qa_worker import QAWorker
from classbot.search import Searcher
from tests.test_slack_flow import BOT, FakeLLM, env, mention  # noqa: F401  (env は fixture)


@pytest.fixture
def restricted(env):  # noqa: F811
    env.s.slack_restrict_users = True
    env.s.slack_allowed_user_ids = ["UENV"]
    return env


def qa_users(env):
    users = []
    while (job := env.queue.claim(KIND_QA)) is not None:
        users.append(job.payload["user"])
    return users


async def test_restriction_off_by_default_everyone_can_ask(env):  # noqa: F811
    await env.h.on_app_mention(mention("第3回の課題は？", user="UANY"), env.slack)
    assert qa_users(env) == ["UANY"] and not env.slack.ephemerals


async def test_unregistered_user_gets_private_notice_and_no_job(restricted):
    ev = mention("第3回の課題は？", user="USTRANGER", ts="1.0")
    await restricted.h.on_app_mention(ev, restricted.slack)
    await restricted.h.on_app_mention(ev, restricted.slack)   # 再送でも案内は1回
    assert qa_users(restricted) == []
    assert not restricted.slack.posts                          # チャンネルには何も投稿しない
    assert restricted.slack.ephemerals == [{"channel": "C123", "user": "USTRANGER", "text": NOT_ALLOWED_MSG}]
    # コマンド（ヘルプ・管理コマンド）も同じ
    await restricted.h.on_app_mention(mention("ヘルプ", user="USTRANGER", ts="2.0"), restricted.slack)
    assert restricted.slack.ephemerals[-1]["text"] == NOT_ALLOWED_MSG and not restricted.slack.posts


async def test_admin_env_and_registered_users_can_ask(restricted):
    for n, user in enumerate(("UADMIN", "UENV"), 1):
        await restricted.h.on_app_mention(mention("質問です", user=user, ts=f"{n}.0"), restricted.slack)
    assert qa_users(restricted) == ["UADMIN", "UENV"]
    await restricted.h.on_app_mention(mention("質問です", user="U1", ts="3.0"), restricted.slack)
    assert qa_users(restricted) == []
    AccessList(restricted.db, restricted.s).add(["U1"], "UADMIN")
    await restricted.h.on_app_mention(mention("質問です", user="U1", ts="4.0"), restricted.slack)
    assert qa_users(restricted) == ["U1"]


async def test_unregistered_followup_in_bot_thread_is_ignored(restricted):
    await restricted.h.on_app_mention(mention("質問です", user="UENV", ts="1.0"), restricted.slack)
    assert qa_users(restricted) == ["UENV"]
    await restricted.h.on_message({"channel": "C123", "user": "USTRANGER", "ts": "2.0", "thread_ts": "1.0",
                                   "text": "便乗して質問"}, restricted.slack)
    await restricted.h.on_message({"channel": "C123", "user": "UENV", "ts": "3.0", "thread_ts": "1.0",
                                   "text": "追加の質問"}, restricted.slack)
    assert qa_users(restricted) == ["UENV"]
    assert not restricted.slack.ephemerals    # スレッド内の会話には案内も出さない


async def test_admin_manages_users_from_slack(restricted):
    async def admin(text, ts, thread_ts=None):
        extra = {"thread_ts": thread_ts} if thread_ts else {}
        await restricted.h.on_app_mention(mention(text, user="UADMIN", ts=ts, **extra), restricted.slack)
        return restricted.slack.ephemerals[-1]

    reply = await admin("利用者追加 <@U111|yamada> <@U222> U333ABCD", "10.0")
    assert reply["user"] == "UADMIN" and "thread_ts" not in reply
    assert "✅ 利用者を追加しました: <@U111> <@U222> <@U333ABCD>" in reply["text"]
    assert f"<@{BOT}>" not in reply["text"]                       # ボット自身は登録しない
    assert "登録済み: <@U111>" in (await admin("利用者追加 <@U111>", "11.0"))["text"]

    listing = (await admin("利用者一覧", "12.0", thread_ts="10.0"))
    assert listing["thread_ts"] == "10.0"                          # スレッド内ならスレッドに返す
    assert "*利用者の制限*: 有効" in listing["text"] and "Slack で登録（3人）" in listing["text"]
    assert "<@U222>" in listing["text"] and "<@UENV>" in listing["text"]

    reply = await admin("利用者削除 <@U222> <@UENV> <@U999>", "13.0")
    assert "解除しました: <@U222>" in reply["text"]
    assert "<@UENV> は .env" in reply["text"] and "登録されていません: <@U999>" in reply["text"]
    assert not restricted.slack.posts                              # 利用者の管理はチャンネルに出さない

    await restricted.h.on_app_mention(mention("質問", user="U222", ts="14.0"), restricted.slack)
    assert qa_users(restricted) == []

    reply = await admin("利用者追加", "15.0")
    assert "メンションしてください" in reply["text"]


async def test_user_commands_are_admin_only(restricted):
    AccessList(restricted.db, restricted.s).add(["U1"], "UADMIN")
    await restricted.h.on_app_mention(mention("利用者追加 <@U2>", user="U1", ts="1.0"), restricted.slack)
    assert restricted.slack.posts[-1]["text"] == "この操作は管理者のみ実行できます。"
    assert not AccessList(restricted.db, restricted.s).is_allowed("U2")


async def test_restriction_off_commands_warn(env):  # noqa: F811
    await env.h.on_app_mention(mention("利用者追加 <@U111>", user="UADMIN"), env.slack)
    assert "制限が無効" in env.slack.ephemerals[-1]["text"]


async def test_worker_rechecks_before_calling_api(restricted):
    """受付後に制限を有効にした質問（再起動で再実行された場合など）でも API を呼ばない。"""
    restricted.queue.enqueue(KIND_QA, {"channel": "C123", "user": "USTRANGER", "ts": "1.0",
                                       "thread_ts": "1.0", "text": "質問"})
    llm = FakeLLM()
    qa = QAWorker(restricted.s, restricted.db, restricted.queue, Searcher(restricted.db, restricted.s),
                  restricted.budget, llm, restricted.slack)
    await qa.handle(restricted.queue.claim(KIND_QA))
    assert llm.calls == [] and NOT_ALLOWED_MSG in restricted.slack.posts[-1]["text"]
    assert restricted.budget.summary().month_requests == 0


def test_user_ids_in():
    assert user_ids_in(f"<@{BOT}> 利用者追加 <@U1AB|name> <@W2CD> U3EFGHIJ x", exclude=BOT) == \
        ["U1AB", "W2CD", "U3EFGHIJ"]
    assert user_ids_in("利用者追加 USER さん") == []   # 普通の英単語はIDとみなさない


def test_status_shows_restriction(restricted):
    from classbot.status import format_status

    AccessList(restricted.db, restricted.s).add(["U1", "U2"], "UADMIN")
    text = format_status(restricted.s, restricted.db, restricted.store, restricted.queue, {})
    assert "利用者の制限: 有効（Slack で登録 2人 ＋ .env で許可 1人 ＋ 管理者）" in text
