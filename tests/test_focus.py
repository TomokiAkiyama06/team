"""質問が指している資料（スレッドで登録した資料・ファイル名・授業回）の全文を渡す仕組みのテスト。

「資料登録したスレッドで『今回渡した資料の授業内容教えて、あと提出物何がある？』と聞くと、
キーワードが資料に無いため検索で全件足切りされ『確認できませんでした』になる」問題の回帰テストを含む。"""

from types import SimpleNamespace

import pytest

from classbot import material_worker as mw
from classbot.budget import BudgetManager
from classbot.indexer import Indexer
from classbot.jobs import KIND_QA, KIND_SLACK_UPLOAD, JobQueue
from classbot.material_worker import MaterialWorker
from classbot.materials import MaterialStore
from classbot.pricing import PriceTable
from classbot.prompts import FOCUS_NOTE, render_search_results
from classbot.qa_worker import QAWorker
from classbot.search import FOCUS_LATEST, FOCUS_LECTURE, FOCUS_NAMED, FOCUS_THREAD, Searcher
from classbot.textnorm import query_tokens
from tests.test_search import add_material
from tests.test_slack_flow import FakeLLM, FakeSlack

USER_QUESTION = "今回渡した資料の授業内容教えて、あと提出物何がある？"

# 第4回スライド（ファイル名に授業回が無い。本文にも「資料」「授業内容」は出てこない）
SLIDES = [
    "第4回 プロジェクト演習テーマB VMでウェブサーバ・コンテンツ構築",
    "本日の目標 VM上にApacheをインストールし、Webページを公開する",
    "Apacheのインストール sudo apt install apache2",
    "ドキュメントルート /var/www/html/index.html を編集する",
    "課題 自己紹介ページを作成し、スクリーンショットをLMSに出すこと",
]


def _pdf_bytes(pages: list[str]) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_text((72, 72), text, fontsize=11, fontname="japan")
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def env(settings, db, monkeypatch):
    settings.image_event_grace_sec = 0
    queue = JobQueue(db)
    store = MaterialStore(db, settings)
    indexer = Indexer(db, settings, store)
    slack, llm = FakeSlack(), FakeLLM(text="第4回は Apache で Web サーバーを構築します[S2]。\n[[SOURCES: S2]]")
    budget = BudgetManager(db, settings, PriceTable.load(settings.pricing_file))
    searcher = Searcher(db, settings)
    qa = QAWorker(settings, db, queue, searcher, budget, llm, slack)
    worker = MaterialWorker(settings, db, queue, store, indexer, slack)
    pdfs: dict[str, bytes] = {}

    async def fake_download(self, url, dest):
        dest.write_bytes(pdfs[url])

    monkeypatch.setattr(mw.MaterialWorker, "_download", fake_download)

    async def upload(fid, name, pages, thread_ts):
        url = f"https://files.slack.com/{fid}"
        pdfs[url] = _pdf_bytes(pages)
        slack.files[fid] = {"name": name, "size": len(pdfs[url]), "mode": "hosted", "url_private_download": url}
        queue.enqueue(KIND_SLACK_UPLOAD, {"channel": "C123", "user": "UADMIN", "thread_ts": thread_ts,
                                          "file_ids": [fid], "target_material_id": None})
        await worker.handle(queue.claim(KIND_SLACK_UPLOAD))
        assert "登録しました" in slack.posts[-1]["text"], slack.posts[-1]
        return db.query_one("SELECT id FROM materials WHERE display_name=?", (name,))["id"]

    async def ask(text, ts, thread_ts=None):
        queue.enqueue(KIND_QA, {"channel": "C123", "user": "U1", "ts": ts, "thread_ts": thread_ts or ts,
                                "text": text})
        await qa.handle(queue.claim(KIND_QA))
        content = llm.calls[-1][1][-1]["content"]
        return content if isinstance(content, str) else content[-1]["text"]

    return SimpleNamespace(s=settings, db=db, store=store, searcher=searcher, slack=slack, llm=llm,
                           upload=upload, ask=ask)


# ---------------------------------------------------------------- スレッドで登録した資料

async def test_material_registered_in_thread_is_answered_in_full(env):
    """報告された事象の再現: 登録したスレッドでの質問に、資料の全ページを根拠として渡す。"""
    mid = await env.upload("F1", "proxb_41.pdf", SLIDES, thread_ts="1.0")
    assert env.db.query_one("SELECT 1 FROM thread_materials WHERE channel='C123' AND thread_ts='1.0' "
                            "AND material_id=?", (mid,))
    # 修正前と同じく、キーワード検索だけでは何も見つからない質問
    assert env.searcher.search(USER_QUESTION).sufficiency in ("none", "weak")

    sent = await env.ask(USER_QUESTION, ts="2.0", thread_ts="1.0")
    assert 'status="sufficient"' in sent
    assert "<focus_materials>" in sent and FOCUS_NOTE in sent
    assert "proxb_41.pdf（このスレッドで登録・指定された資料。全文5箇所を S1〜S5 として含めています）" in sent
    for page in ("プロジェクト演習テーマB", "apache2", "/var/www/html/index.html", "自己紹介ページ"):
        assert page in sent, page
    assert "<course_policy_note>" in sent    # 「提出物」は授業固有の情報として扱う
    final = env.slack.updates[-1]["text"]
    assert "出典：proxb_41.pdf p.2 [S2]" in final


async def test_thread_link_does_not_leak_to_other_threads(env):
    await env.upload("F1", "proxb_41.pdf", SLIDES, thread_ts="1.0")
    await env.upload("F2", "第03回_講義.pdf", ["Linux の基本操作"], thread_ts="2.0")
    sent = await env.ask("第3回の内容は？", ts="3.0", thread_ts="2.0")
    assert "第03回_講義.pdf（質問の授業回" in sent and "proxb_41.pdf（" not in sent


async def test_material_update_links_thread(env, settings):
    mid = await env.upload("F1", "proxb_41.pdf", SLIDES, thread_ts="1.0")
    env.slack.files["F2"] = dict(env.slack.files["F1"])
    q = JobQueue(env.db)
    q.enqueue(KIND_SLACK_UPLOAD, {"channel": "C123", "user": "UADMIN", "thread_ts": "5.0",
                                  "file_ids": ["F2"], "target_material_id": mid})
    worker = MaterialWorker(settings, env.db, q, env.store, Indexer(env.db, settings, env.store), env.slack)
    await worker.handle(q.claim(KIND_SLACK_UPLOAD))
    focus = env.searcher.resolve_focus("内容は？", "C123", "5.0")
    assert [(f.material_id, f.reason) for f in focus] == [(mid, FOCUS_THREAD)]


async def test_deleting_material_removes_thread_link(env):
    mid = await env.upload("F1", "proxb_41.pdf", SLIDES, thread_ts="1.0")
    env.store.delete(mid)
    assert env.db.query_one("SELECT 1 FROM thread_materials WHERE material_id=?", (mid,)) is None
    assert env.searcher.resolve_focus(USER_QUESTION, "C123", "1.0") == []


async def test_named_material_stays_in_focus_for_followups(env):
    mid = await env.upload("F1", "proxb_41.pdf", SLIDES, thread_ts="1.0")
    sent = await env.ask("proxb_41.pdf の内容を要約して", ts="10.0")
    assert "質問でファイル名が指定された資料。全文5箇所" in sent
    # 同じスレッドの追加質問（ファイル名なし）でも同じ資料を全文で参照する
    sent = await env.ask("提出物は？", ts="11.0", thread_ts="10.0")
    assert "このスレッドで登録・指定された資料。全文5箇所" in sent
    assert env.searcher.resolve_focus("提出物は？", "C123", "10.0")[0].material_id == mid


# ---------------------------------------------------------------- 資料の特定

def test_named_file_reference(db, settings):
    add_material(db, "proxb_41.pdf", [("p.1", None, SLIDES[0])])
    add_material(db, "other.pdf", [("p.1", None, "無関係")])
    focus = Searcher(db, settings).resolve_focus("proxb_41 の内容を教えて")
    assert [(f.display_name, f.reason) for f in focus] == [("proxb_41.pdf", FOCUS_NAMED)]
    # 短すぎる名前は質問文に偶然含まれても資料指定とみなさない
    add_material(db, "ab.md", [("L1", None, "x")])
    assert all(f.display_name != "ab.md" for f in Searcher(db, settings).resolve_focus("ab について"))


def test_lecture_number_from_filename_or_first_page(db, settings):
    add_material(db, "proxb_41.pdf", [(f"p.{i}", None, t) for i, t in enumerate(SLIDES, 1)])
    add_material(db, "第03回_講義.pdf", [("p.1", None, "Linux の基本操作")])
    # シラバスのように複数の回が並ぶ資料は、特定の回の資料とはみなさない
    add_material(db, "シラバス.pdf", [("p.1", None, "第1回 ガイダンス 第2回 Linux 第3回 SSH 第4回 Web")])
    s = Searcher(db, settings)
    assert [(f.display_name, f.reason) for f in s.resolve_focus("第4回の課題は？")] == \
        [("proxb_41.pdf", FOCUS_LECTURE)]
    assert [f.display_name for f in s.resolve_focus("第三回の内容")] == ["第03回_講義.pdf"]
    assert s.resolve_focus("Apache の設定方法") == []


def test_latest_material_for_konkai_questions(db, settings):
    add_material(db, "第03回_講義.pdf", [("p.1", None, "Linux の基本操作")])
    latest = add_material(db, "proxb_41.pdf", [(f"p.{i}", None, t) for i, t in enumerate(SLIDES, 1)])
    s = Searcher(db, settings)
    for q in (USER_QUESTION, "最新の資料をまとめて", "今週の課題は？", "今回の授業でやったことは？"):
        assert [(f.material_id, f.reason) for f in s.resolve_focus(q)] == [(latest, FOCUS_LATEST)], q
    # 「今回」が資料を指していない質問・より具体的な手掛かりがある質問には使わない
    assert s.resolve_focus("今回、sudo apt update でエラーが出ました") == []
    assert [f.reason for f in s.resolve_focus("今回の第3回の資料は？")] == [FOCUS_LECTURE]
    db.execute("INSERT INTO thread_materials(channel, thread_ts, material_id, created_at) "
               "VALUES ('C123', '1.0', 1, 'x')")
    assert [(f.material_id, f.reason) for f in s.resolve_focus(USER_QUESTION, "C123", "1.0")] == \
        [(1, FOCUS_THREAD)]
    # モデルには推測であることと、参照した資料名を伝えるよう指示する
    rendered = render_search_results(s.retrieve(USER_QUESTION))
    assert "最も新しく登録された資料と判断しました" in rendered and "proxb_41.pdf" in rendered


def test_focus_over_budget_keeps_first_and_closest_chunks(db, settings):
    pages = [("p.1", None, "第4回 表紙 " + "あ" * 80)]
    pages += [(f"p.{i}", None, f"手順{i} " + "い" * 80) for i in range(2, 9)]
    pages.append(("p.9", None, "課題 提出物はスクリーンショット " + "う" * 80))
    add_material(db, "big.pdf", pages)
    settings.focus_material_max_chars = 300
    res = Searcher(db, settings).retrieve("big.pdf の提出物は？")
    f = res.focus[0]
    assert not f.complete and f.total_chunks == 9
    focus_hits = [h for h in res.hits if h.source_id in f.source_ids]
    locations = [h.location for h in focus_hits]
    assert locations[0] == "p.1" and "p.9" in locations       # 表紙と質問に近いページ
    assert locations == sorted(locations, key=lambda x: int(x[2:]))   # 資料の順序を保つ
    assert sum(len(h.text) for h in focus_hits) <= 300
    assert f"一部{len(f.source_ids)}箇所（全9箇所中）" in render_search_results(res)


def test_search_hits_follow_focus_without_duplicates(db, settings):
    add_material(db, "proxb_41.pdf", [(f"p.{i}", None, t) for i, t in enumerate(SLIDES, 1)])
    add_material(db, "課題提出方法.md", [("見出し「提出」", "提出", "課題はLMSから提出します。締切は授業の翌週。")])
    res = Searcher(db, settings).retrieve("第4回の課題の提出方法は？")
    assert [h.source_id for h in res.hits] == [f"S{i}" for i in range(1, len(res.hits) + 1)]
    assert len({h.chunk_id for h in res.hits}) == len(res.hits)
    assert res.focus[0].source_ids == ["S1", "S2", "S3", "S4", "S5"]
    assert any(h.display_name == "課題提出方法.md" for h in res.hits[5:])


# ---------------------------------------------------------------- キーワード検索の改善

def test_generic_question_words_excluded_from_tokens():
    toks = query_tokens(USER_QUESTION)
    assert "提出" in toks
    for generic in ("資料", "授業", "内容", "今回", "教え"):
        assert generic not in toks
    # 一般語しか無い質問では残す（検索語が空にならない）
    assert query_tokens("資料") == ["資料"]


def test_question_with_generic_words_finds_relevant_chunk(db, settings):
    add_material(db, "proxb_41.pdf", [("p.5", None, "提出物: 自己紹介ページのスクリーンショット")])
    res = Searcher(db, settings).search(USER_QUESTION)
    assert [h.location for h in res.hits] == ["p.5"]
