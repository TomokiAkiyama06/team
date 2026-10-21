import pytest

from tests._fake_extract import ensure_extract_module, make_fake

ensure_extract_module()

import classbot.indexer as indexer_mod  # noqa: E402
from classbot.indexer import Indexer  # noqa: E402
from classbot.materials import MaterialStore  # noqa: E402
from classbot.textnorm import fts_match_expr, query_tokens  # noqa: E402


@pytest.fixture
def env(db, settings, monkeypatch):
    fake = make_fake()
    monkeypatch.setattr(indexer_mod, "extract_to_jsonl", fake)
    store = MaterialStore(db, settings)
    return store, Indexer(db, settings, store), fake


def _server(store, settings, name, content):
    p = settings.materials_dir / name
    p.write_text(content, encoding="utf-8")
    st = p.stat()
    return store.upsert_server(name, st.st_size, st.st_mtime, "md"), p


def _search(db, q):
    return db.query(
        "SELECT c.text FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
        "JOIN materials m ON m.id = c.material_id AND m.active_version = c.version "
        "WHERE chunks_fts MATCH ?", (fts_match_expr(query_tokens(q)),))


def test_index_and_skip_unchanged(env, db, settings):
    store, idx, fake = env
    mid, _ = _server(store, settings, "課題提出方法.md", "第3回の課題はLMSで提出\n締切は金曜")
    out = idx.index_material(mid)
    assert out.status == "ready" and out.chunk_count == 2 and not out.skipped
    row = store.get(mid)
    assert row["status"] == "ready" and row["active_version"] is not None
    assert row["indexed_hash"] == row["current_hash"]
    assert [r["text"] for r in _search(db, "締切")] == ["締切は金曜"]
    # 内容が同じなら再処理しない
    out2 = idx.index_material(mid)
    assert out2.skipped and len(fake.calls) == 1
    # force なら再処理
    assert not idx.index_material(mid, force=True).skipped
    assert db.query_one("SELECT COUNT(*) n FROM chunks")["n"] == 2


def test_update_replaces_old_chunks(env, db, settings):
    store, idx, _ = env
    mid, p = _server(store, settings, "a.md", "古い内容です")
    idx.index_material(mid)
    p.write_text("新しい内容です\n追加行", encoding="utf-8")
    out = idx.index_material(mid)
    assert out.status == "ready" and out.chunk_count == 2
    texts = {r["text"] for r in db.query("SELECT text FROM chunks")}
    assert texts == {"新しい内容です", "追加行"}
    assert db.query_one("SELECT COUNT(*) n FROM chunks_fts")["n"] == 2


@pytest.mark.parametrize("mode", ["fail", "crash"])
def test_failed_update_keeps_last_good_version(env, db, settings, monkeypatch, mode):
    store, idx, _ = env
    mid, p = _server(store, settings, "a.md", "正常な内容")
    idx.index_material(mid)
    good_version = store.get(mid)["active_version"]
    p.write_text("壊れた更新", encoding="utf-8")
    monkeypatch.setattr(indexer_mod, "extract_to_jsonl", make_fake(mode))
    out = idx.index_material(mid)
    assert out.status == "failed"
    row = store.get(mid)
    assert row["status"] == "ready" and row["active_version"] == good_version
    assert row["last_error"]
    assert [r["text"] for r in _search(db, "正常な内容")] == ["正常な内容"]


def test_no_text_without_previous_version(env, db, settings, monkeypatch):
    store, idx, _ = env
    mid, _ = _server(store, settings, "scan.md", "x")
    monkeypatch.setattr(indexer_mod, "extract_to_jsonl", make_fake("no_text"))
    out = idx.index_material(mid)
    assert out.status == "no_text"
    row = store.get(mid)
    assert row["status"] == "no_text" and row["active_version"] is None
    assert "テキストを抽出できませんでした" in row["last_error"]


def test_cleanup_orphans(env, db, settings):
    store, idx, _ = env
    mid, _ = _server(store, settings, "a.md", "内容")
    idx.index_material(mid)
    # 中断した処理の残骸を模擬
    db.execute("INSERT INTO chunks (material_id, version, seq, location, text) "
               "VALUES (?, 99999, 0, 'L1', 'orphan')", (mid,))
    assert idx.cleanup_orphans() == 1
    assert db.query_one("SELECT COUNT(*) n FROM chunks")["n"] == 1


def test_missing_file(env, settings):
    store, idx, _ = env
    mid = store.upsert_server("gone.md", 1, 1.0, "md")
    assert idx.index_material(mid).status == "missing"


def test_replace_slack_file(env, db, settings):
    store, idx, _ = env
    old = settings.uploads_dir / "old__a.md"
    old.write_text("旧版", encoding="utf-8")
    mid = store.create_slack("a.md", "old__a.md", "md", 6, "UADMIN")
    assert idx.index_material(mid).status == "ready"

    tmp = settings.tmp_dir / "upload.part"
    tmp.write_text("新版の資料", encoding="utf-8")
    out = idx.replace_slack_file(mid, tmp, "a_v2.md")
    assert out.status == "ready"
    row = store.get(mid)
    assert row["display_name"] == "a_v2.md" and row["stored_name"] != "old__a.md"
    assert (settings.uploads_dir / row["stored_name"]).exists()
    assert not old.exists() and any(settings.trash_dir.iterdir())
    assert [r["text"] for r in db.query("SELECT text FROM chunks")] == ["新版の資料"]


def test_replace_slack_file_failure_keeps_old(env, db, settings, monkeypatch):
    store, idx, _ = env
    old = settings.uploads_dir / "old__a.md"
    old.write_text("旧版", encoding="utf-8")
    mid = store.create_slack("a.md", "old__a.md", "md", 6, "UADMIN")
    idx.index_material(mid)
    monkeypatch.setattr(indexer_mod, "extract_to_jsonl", make_fake("fail"))
    tmp = settings.tmp_dir / "upload.part"
    tmp.write_text("壊れた", encoding="utf-8")
    out = idx.replace_slack_file(mid, tmp, "a.md")
    assert out.status == "failed"
    row = store.get(mid)
    assert row["stored_name"] == "old__a.md" and row["status"] == "ready"
    assert old.exists()
    assert sorted(p.name for p in settings.uploads_dir.iterdir()) == ["old__a.md"]


def test_integration_real_extractor_md_txt(db, settings):
    """実際の抽出器（子プロセス）で .md / .txt を登録し検索できること。"""
    pytest.importorskip("classbot.chunking")
    from classbot import extract

    if not hasattr(extract, "extract_to_jsonl") or extract.__name__ != "classbot.extract":
        pytest.skip("extract not implemented")
    store = MaterialStore(db, settings)
    idx = Indexer(db, settings, store)
    md = settings.materials_dir / "課題提出方法.md"
    md.write_text("# 提出方法\n\nレポートはLMSから提出してください。\n\n## 締切\n\n締切は7月31日です。\n",
                  encoding="utf-8")
    txt = settings.materials_dir / "第03回_演習.txt"
    txt.write_text("演習問題1: 配列の合計を求めよ。\n演習問題2: 文字列を反転せよ。\n",
                   encoding="utf-8")
    ids = []
    for p, t in ((md, "md"), (txt, "txt")):
        st = p.stat()
        ids.append(store.upsert_server(p.name, st.st_size, st.st_mtime, t))
    for mid in ids:
        out = idx.index_material(mid)
        assert out.status == "ready", out.message
    hits = db.query(
        "SELECT c.text, c.location, m.display_name FROM chunks_fts f "
        "JOIN chunks c ON c.id = f.rowid "
        "JOIN materials m ON m.id = c.material_id AND m.active_version = c.version "
        "WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts, 5.0, 1.0)",
        (fts_match_expr(query_tokens("締切はいつ？")),))
    assert hits and "7月31日" in hits[0]["text"]
    assert hits[0]["display_name"] == "課題提出方法.md"
    assert not list(settings.tmp_dir.glob("chunks-*.jsonl"))
