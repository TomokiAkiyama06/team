from classbot import commands
from classbot.jobs import KIND_INGEST, KIND_QA, JobQueue
from classbot.slack_format import md_to_mrkdwn, split_message


def test_parse_command_exact_table():
    c = commands.parse_command("<@UBOT> 資料一覧 2")
    assert c and c.name == commands.LIST and c.int_arg() == 2
    assert commands.parse_command("<@UBOT>　資料削除　#12").int_arg() == 12  # 全角空白
    assert commands.parse_command("<@UBOT> 資料一覧について教えて") is None
    assert commands.parse_command("<@UBOT> 第3回の課題は？") is None
    assert commands.parse_command("<@UBOT>") is None
    assert commands.parse_command("<@UBOT> HELP").name == commands.HELP


def test_job_queue_dedupe_claim_requeue(db):
    q = JobQueue(db)
    a = q.enqueue(KIND_INGEST, {"material_id": 1}, dedupe_key="ingest:1")
    assert a is not None
    assert q.enqueue(KIND_INGEST, {"material_id": 1}, dedupe_key="ingest:1") is None
    q.enqueue(KIND_QA, {"user": "U1"})
    assert q.pending_qa_for_user("U1") == 1
    job = q.claim(KIND_INGEST)
    assert job.payload == {"material_id": 1} and job.attempts == 1
    assert q.claim(KIND_INGEST) is None
    assert q.requeue_running() == 1
    job = q.claim(KIND_INGEST)
    q.fail(job.id, "boom", retry=True)
    job = q.claim(KIND_INGEST)
    assert job.attempts == 3
    q.fail(job.id, "boom", retry=True)  # 上限到達で failed
    assert q.claim(KIND_INGEST) is None
    # 完了済みなら同じ dedupe_key で再登録できる
    assert q.enqueue(KIND_INGEST, {"material_id": 1}, dedupe_key="ingest:1") is not None


def test_mrkdwn_and_split():
    assert md_to_mrkdwn("**重要** [資料](https://x.example)") == "*重要* <https://x.example|資料>"
    assert md_to_mrkdwn("```\n**keep**\n```") == "```\n**keep**\n```"
    parts = split_message("あ" * 5000 + "\n" + "い" * 10, 3900)
    assert all(len(p) <= 3900 for p in parts) and "".join(parts).replace("\n", "") == "あ" * 5000 + "い" * 10
