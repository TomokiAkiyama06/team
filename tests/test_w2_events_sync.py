"""W2: Slack イベント処理の網羅性と、Slack 登録・materials/ 同期の競合に関する回帰テスト。"""

import json
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import classbot.indexer as indexer_mod
import classbot.materials as materials_mod
from classbot import material_worker as mw
from classbot.budget import BudgetManager
from classbot.indexer import Indexer
from classbot.jobs import KIND_INGEST, KIND_QA, KIND_SLACK_UPLOAD, JobQueue
from classbot.material_worker import MaterialWorker
from classbot.materials import MaterialStore
from classbot.pricing import PriceTable
from classbot.safety import sha256_file
from classbot.scanner import Scanner
from classbot.slack_app import SlackHandlers
from tests._fake_extract import FakeResult, make_fake

BOT = "U0BOT"
ADMIN = "UADMIN"
CH = "C123"
ROOT = Path(__file__).resolve().parents[1]


class FakeSlack:
    def __init__(self):
        self.posts, self.updates, self.ephemerals, self.files = [], [], [], {}

    async def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": f"9{len(self.posts)}.0"}

    async def chat_update(self, **kw):
        self.updates.append(kw)

    async def chat_postEphemeral(self, **kw):
        self.ephemerals.append(kw)

    async def files_info(self, file):
        return {"file": self.files[file]}


@pytest.fixture
def env(settings, db):
    queue = JobQueue(db)
    store = MaterialStore(db, settings)
    budget = BudgetManager(db, settings, PriceTable.load(settings.pricing_file))
    runtime = {"bot_user_id": BOT, "wake": lambda: None}
    h = SlackHandlers(settings, db, queue, store, budget, runtime)
    return SimpleNamespace(s=settings, db=db, queue=queue, store=store, h=h, slack=FakeSlack(),
                           indexer=Indexer(db, settings, store))


# --- 実際の Slack ペイロードに近い形 -------------------------------------------------
def slack_file(fid, name="資料.md"):
    return {"id": fid, "name": name, "title": name, "mimetype": "text/markdown", "filetype": "markdown",
            "size": 120, "mode": "hosted", "url_private_download": f"https://files.slack.com/files-pri/T1-{fid}/download/x"}


def app_mention(text, ts, user=ADMIN, thread_ts=None, files=None):
    ev = {"type": "app_mention", "user": user, "text": f"<@{BOT}> {text}", "ts": ts, "channel": CH,
          "event_ts": ts, "team": "T1", "client_msg_id": f"m-{ts}",
          "blocks": [{"type": "rich_text", "elements": []}]}
    if thread_ts:
        ev["thread_ts"] = thread_ts
    if files is not None:
        ev["files"] = files
    return ev


def message(text, ts, user=ADMIN, thread_ts=None, files=None, **extra):
    ev = {"type": "message", "channel": CH, "user": user, "text": text, "ts": ts, "event_ts": ts,
          "channel_type": "channel", "team": "T1"}
    if thread_ts:
        ev["thread_ts"] = thread_ts
    if files is not None:
        ev.update({"subtype": "file_share", "files": files, "upload": False, "display_as_bot": False})
    ev.update(extra)
    return ev


def upload_jobs(env):
    return [json.loads(r["payload"]) for r in env.db.query("SELECT payload FROM jobs WHERE kind=?",
                                                            (KIND_SLACK_UPLOAD,))]


# --- (4) マニフェスト ----------------------------------------------------------------
def test_manifest_has_scopes_and_events_for_threads_in_public_and_private_channels():
    text = (ROOT / "slack-app-manifest.yaml").read_text(encoding="utf-8")
    scopes = set(re.findall(r"^\s+- ([a-z_]+:[a-z_]+)", text, flags=re.M))
    events = set(re.findall(r"^\s+- (app_mention|message\.[a-z]+)\b", text, flags=re.M))
    assert {"app_mentions:read", "channels:history", "groups:history", "chat:write", "files:read"} <= scopes
    assert {"app_mention", "message.channels", "message.groups"} <= events
    for key in ("socket_mode_enabled: true", "is_enabled: true", "org_deploy_enabled: false",
                "token_rotation_enabled: false"):
        assert key in text
    # 不要な広い権限を要求しない
    assert not scopes & {"channels:read", "users:read", "im:history", "chat:write.public", "files:write"}


# --- (4) イベント処理 ----------------------------------------------------------------
@pytest.mark.parametrize("order", ["mention_first", "message_first"])
async def test_mention_with_attachment_registers_once(env, order):
    files = [slack_file("F1")]
    m = app_mention("資料登録", "100.0", files=files)
    msg = message(f"<@{BOT}> 資料登録", "100.0", files=files)
    for ev in ([m, msg] if order == "mention_first" else [msg, m]):
        await (env.h.on_app_mention if ev["type"] == "app_mention" else env.h.on_message)(ev, env.slack)
    jobs = upload_jobs(env)
    assert len(jobs) == 1 and jobs[0]["file_ids"] == ["F1"]
    # 次の「資料登録」（添付なし）で同じファイルを再登録しない
    await env.h.on_app_mention(app_mention("資料登録", "101.0"), env.slack)
    assert len(upload_jobs(env)) == 1
    assert "添付してください" in env.slack.posts[-1]["text"]


async def test_mention_event_without_files_field_still_registers(env):
    """app_mention に files が含まれない場合でも、同じメッセージの file_share で登録できる。"""
    await env.h.on_app_mention(app_mention("資料登録", "110.0"), env.slack)
    await env.h.on_message(message(f"<@{BOT}> 資料登録", "110.0", files=[slack_file("F2")]), env.slack)
    jobs = upload_jobs(env)
    assert len(jobs) == 1 and jobs[0]["file_ids"] == ["F2"] and jobs[0]["thread_ts"] == "110.0"


async def test_old_pending_not_double_fulfilled_by_mention_with_file(env):
    await env.h.on_app_mention(app_mention("資料登録", "120.0"), env.slack)  # 登録待ち
    files = [slack_file("F3")]
    # 別メッセージで「メンション＋添付」。message イベントが先に届いても二重登録しない
    await env.h.on_message(message(f"<@{BOT}> 資料登録", "121.0", files=files), env.slack)
    await env.h.on_app_mention(app_mention("資料登録", "121.0", files=files), env.slack)
    assert len(upload_jobs(env)) == 1
    assert env.db.query_one("SELECT COUNT(*) n FROM pending_uploads")["n"] == 0


async def test_bot_messages_edits_and_retries_are_ignored(env):
    await env.h.on_app_mention(app_mention("第3回の課題は？", "200.0", user="U1"), env.slack)
    # ボット自身の回答（bot_id 付き）・編集・削除・参加通知は処理しない
    await env.h.on_message(message("回答です", "200.5", user=BOT, thread_ts="200.0", bot_id="B1",
                                   app_id="A1"), env.slack)
    await env.h.on_message({"type": "message", "subtype": "message_changed", "channel": CH, "hidden": True,
                            "ts": "200.6", "message": {"user": "U1", "text": "x", "thread_ts": "200.0"}}, env.slack)
    await env.h.on_message({"type": "message", "subtype": "message_deleted", "channel": CH, "hidden": True,
                            "ts": "200.7", "deleted_ts": "200.0"}, env.slack)
    await env.h.on_message(message("参加しました", "200.8", user="U9", subtype="channel_join"), env.slack)
    # 再送（同じ ts）
    await env.h.on_app_mention(app_mention("第3回の課題は？", "200.0", user="U1"), env.slack)
    assert env.db.query_one("SELECT COUNT(*) n FROM jobs WHERE kind=?", (KIND_QA,))["n"] == 1


async def test_mention_inside_bot_thread_handled_once_and_broadcast_followup(env):
    await env.h.on_app_mention(app_mention("質問1", "300.0", user="U1"), env.slack)
    # スレッド内でメンション付き追加質問: app_mention と message の両方が届く
    await env.h.on_message(message(f"<@{BOT}> 質問2", "301.0", user="U2", thread_ts="300.0"), env.slack)
    await env.h.on_app_mention(app_mention("質問2", "301.0", user="U2", thread_ts="300.0"), env.slack)
    # 「チャンネルにも投稿する」付きの返信
    await env.h.on_message(message("質問3", "302.0", user="U3", thread_ts="300.0",
                                   subtype="thread_broadcast"), env.slack)
    texts = [json.loads(r["payload"])["text"] for r in
             env.db.query("SELECT payload FROM jobs WHERE kind=? ORDER BY id", (KIND_QA,))]
    assert texts == ["質問1", "質問2", "質問3"]


async def test_admin_attachment_in_bot_thread_without_pending_is_a_question(env):
    await env.h.on_app_mention(app_mention("質問", "400.0", user="U1"), env.slack)
    await env.h.on_message(message("このエラーは？", "401.0", user=ADMIN, thread_ts="400.0",
                                   files=[slack_file("F4", "screenshot.png")]), env.slack)
    assert env.db.query_one("SELECT COUNT(*) n FROM jobs WHERE kind=?", (KIND_QA,))["n"] == 2
    assert not upload_jobs(env)


async def test_upload_job_is_idempotent_per_slack_file(env, monkeypatch):
    env.slack.files["F5"] = slack_file("F5", "シラバス.md")

    async def fake_download(self, url, dest):
        dest.write_text("# シラバス\n成績評価はレポート60%。\n", encoding="utf-8")

    monkeypatch.setattr(mw.MaterialWorker, "_download", fake_download)
    worker = MaterialWorker(env.s, env.db, env.queue, env.store, env.indexer, env.slack)
    payload = {"channel": CH, "user": ADMIN, "thread_ts": "1.0", "file_ids": ["F5"], "target_material_id": None}
    for _ in range(2):  # 二重に登録されたジョブ
        env.queue.enqueue(KIND_SLACK_UPLOAD, payload)
        await worker.handle(env.queue.claim(KIND_SLACK_UPLOAD))
    assert env.db.query_one("SELECT COUNT(*) n FROM materials")["n"] == 1


# --- (6) Slack 登録と materials/ 同期 ----------------------------------------------------
@pytest.fixture
def fake_extract(monkeypatch):
    fake = make_fake()
    monkeypatch.setattr(indexer_mod, "extract_to_jsonl", fake)
    return fake


def _texts(db, mid):
    return sorted(r["text"] for r in db.query(
        "SELECT c.text FROM chunks c JOIN materials m ON m.id=c.material_id AND m.active_version=c.version "
        "WHERE m.id=?", (mid,)))


def test_file_modified_during_indexing_is_not_lost(env, monkeypatch):
    """抽出中に元ファイルが更新され、スキャナーの再登録がジョブ重複排除で捨てられても、
    最終的に最新内容が検索対象になり indexed_hash が内容と一致する。"""
    p = env.s.materials_dir / "課題.md"
    p.write_text("旧: 締切は5月1日", encoding="utf-8")
    queue = env.queue
    scanner = Scanner(env.db, env.s, env.store,
                      lambda mid: queue.enqueue(KIND_INGEST, {"material_id": mid}, dedupe_key=f"ingest:{mid}"))
    scanner.scan()
    job = queue.claim(KIND_INGEST)  # 実行中
    base = make_fake()
    state = {"n": 0}

    def fake(path, file_type, settings, out_path):
        state["n"] += 1
        if state["n"] == 1:
            p.write_text("新: 締切は5月8日", encoding="utf-8")
            scanner.scan()  # 実行中ジョブがあるため再登録は重複排除される
        return base(path, file_type, settings, out_path)

    monkeypatch.setattr(indexer_mod, "extract_to_jsonl", fake)
    env.indexer.index_material(job.payload["material_id"])
    queue.complete(job.id)
    row = env.store.get(job.payload["material_id"])
    assert _texts(env.db, row["id"]) == ["新: 締切は5月8日"]
    assert row["indexed_hash"] == sha256_file(p)
    # 次のスキャンでも整合している
    assert not scanner.scan().changed


def test_snapshot_hash_matches_indexed_content(env, monkeypatch):
    p = env.s.materials_dir / "a.md"
    p.write_text("v1", encoding="utf-8")
    mid = env.store.upsert_server("a.md", p.stat().st_size, p.stat().st_mtime, "md")
    seen = {}
    base = make_fake()

    def fake(path, file_type, settings, out_path):
        seen.setdefault("path", path)
        return base(path, file_type, settings, out_path)

    monkeypatch.setattr(indexer_mod, "extract_to_jsonl", fake)
    env.indexer.index_material(mid)
    assert seen["path"] != p and seen["path"].parent == env.s.tmp_dir  # スナップショットを抽出
    assert not list(env.s.tmp_dir.glob("snap-*"))  # 後片付け済み


def test_concurrent_indexers_keep_complete_latest_version(env, monkeypatch):
    """CLI --inline とボットが同じ資料を同時に処理しても、有効版のチャンクが欠けない。
    A のチャンク書き込み直後（切替前）に B が最後まで処理して切り替える順序を再現する。"""
    p = env.s.materials_dir / "big.md"
    p.write_text("\n".join(f"行{i}" for i in range(450)), encoding="utf-8")
    mid = env.store.upsert_server("big.md", p.stat().st_size, p.stat().st_mtime, "md")
    monkeypatch.setattr(indexer_mod, "extract_to_jsonl", make_fake())
    other = Indexer(env.db, env.s, env.store)
    original = Indexer._insert_chunks
    state = {"first": True}

    def interleaved(self, *a, **k):
        n = original(self, *a, **k)
        if state["first"]:
            state["first"] = False
            other.index_material(mid, force=True)  # B が割り込んで先に切り替える
        return n

    monkeypatch.setattr(Indexer, "_insert_chunks", interleaved)
    env.indexer.index_material(mid, force=True)
    row = env.store.get(mid)
    assert len(_texts(env.db, mid)) == 450 == row["chunk_count"]
    env.indexer.cleanup_orphans()
    assert env.db.query_one("SELECT COUNT(*) n FROM chunks")["n"] == 450
    assert env.db.query_one("SELECT COUNT(*) n FROM chunks_fts")["n"] == 450


def test_threaded_indexers_do_not_error(env, monkeypatch):
    p = env.s.materials_dir / "t.md"
    p.write_text("\n".join(f"行{i}" for i in range(300)), encoding="utf-8")
    mid = env.store.upsert_server("t.md", p.stat().st_size, p.stat().st_mtime, "md")
    monkeypatch.setattr(indexer_mod, "extract_to_jsonl", make_fake())
    from classbot.db import Database

    errors = []

    def run():
        try:
            db = Database(env.s.db_path)
            Indexer(db, env.s, MaterialStore(db, env.s)).index_material(mid, force=True)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(_texts(env.db, mid)) == 300


def test_delete_during_indexing_leaves_no_chunks(env, monkeypatch):
    p = env.s.materials_dir / "x.md"
    p.write_text("内容", encoding="utf-8")
    mid = env.store.upsert_server("x.md", p.stat().st_size, p.stat().st_mtime, "md")
    base = make_fake()

    def fake(path, file_type, settings, out_path):
        r = base(path, file_type, settings, out_path)
        env.store.delete(mid)  # 管理者が Slack から削除
        return r

    monkeypatch.setattr(indexer_mod, "extract_to_jsonl", fake)
    out = env.indexer.index_material(mid)
    assert out.status == "missing"
    assert env.db.query_one("SELECT COUNT(*) n FROM chunks")["n"] == 0
    assert not p.exists() and list(env.s.trash_dir.iterdir())
    # 再スキャンで復活しない
    assert not Scanner(env.db, env.s, env.store, lambda m: None).scan().added


def test_symlink_swapped_after_scan_is_refused(env, fake_extract, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("秘密", encoding="utf-8")
    p = env.s.materials_dir / "a.md"
    p.write_text("通常", encoding="utf-8")
    mid = env.store.upsert_server("a.md", p.stat().st_size, p.stat().st_mtime, "md")
    p.unlink()
    p.symlink_to(secret)
    out = env.indexer.index_material(mid)
    assert out.status == "failed" and not fake_extract.calls
    assert env.db.query_one("SELECT COUNT(*) n FROM chunks")["n"] == 0


def test_data_dir_inside_materials_is_not_scanned(db, settings):
    settings.data_dir = settings.materials_dir / "data"
    settings.ensure_dirs()
    (settings.uploads_dir / "abc__x.md").write_text("Slack 登録資料", encoding="utf-8")
    (settings.materials_dir / "正規.md").write_text("サーバー資料", encoding="utf-8")
    store = MaterialStore(db, settings)
    rep = Scanner(db, settings, store, lambda m: None).scan()
    assert [store.get(m)["rel_path"] for m in rep.added] == ["正規.md"]


def test_rename_is_delete_plus_add_and_same_names_are_independent(env, fake_extract):
    scanner = Scanner(env.db, env.s, env.store, lambda m: None)
    p = env.s.materials_dir / "シラバス.md"
    p.write_text("サーバー版", encoding="utf-8")
    scanner.scan()
    old = env.store.get_server("シラバス.md")["id"]
    env.indexer.index_material(old)
    # Slack からも同名の資料を登録
    (env.s.uploads_dir / "u1__シラバス.md").write_text("Slack版", encoding="utf-8")
    sid = env.store.create_slack("シラバス.md", "u1__シラバス.md", "md", 9, ADMIN)
    env.indexer.index_material(sid)
    p.rename(env.s.materials_dir / "シラバス_v2.md")
    rep = scanner.scan()
    assert rep.removed == ["シラバス.md"] and len(rep.added) == 1
    assert env.store.get(old) is None
    assert env.store.get(sid)["status"] == "ready" and _texts(env.db, sid) == ["Slack版"]
    # サーバー側の削除は Slack 登録資料（別保存領域）に影響しない
    assert (env.s.uploads_dir / "u1__シラバス.md").exists()


def test_trash_name_collision_does_not_overwrite(env, monkeypatch):
    class FixedDT:
        @staticmethod
        def now():
            from datetime import datetime
            return datetime(2026, 1, 1, 0, 0, 0, 0)

    monkeypatch.setattr(materials_mod, "datetime", FixedDT)
    (env.s.materials_dir / "a").mkdir()
    (env.s.materials_dir / "b").mkdir()
    f1, f2 = env.s.materials_dir / "a" / "同名.md", env.s.materials_dir / "b" / "同名.md"
    f1.write_text("1", encoding="utf-8")
    f2.write_text("2", encoding="utf-8")
    d1, d2 = env.store.move_to_trash(f1), env.store.move_to_trash(f2)
    assert d1 != d2 and {d1.read_text(), d2.read_text()} == {"1", "2"}


def test_replace_slack_file_extraction_failure_keeps_old_file_and_index(env, monkeypatch):
    (env.s.uploads_dir / "u1__a.md").write_text("旧版", encoding="utf-8")
    sid = env.store.create_slack("a.md", "u1__a.md", "md", 6, ADMIN)
    monkeypatch.setattr(indexer_mod, "extract_to_jsonl", make_fake())
    env.indexer.index_material(sid)
    tmp = env.s.tmp_dir / "new"
    tmp.write_text("新版", encoding="utf-8")
    monkeypatch.setattr(indexer_mod, "extract_to_jsonl", make_fake("crash"))
    out = env.indexer.replace_slack_file(sid, tmp, "a.md")
    row = env.store.get(sid)
    assert out.status == "failed" and row["status"] == "ready" and row["stored_name"] == "u1__a.md"
    assert _texts(env.db, sid) == ["旧版"]
    assert sorted(p.name for p in env.s.uploads_dir.iterdir()) == ["u1__a.md"]
