"""偽の抽出器を使わない、materials/ 同期の全経路テスト。

Scanner（検知）→ ジョブ相当のコールバック → Indexer（実際の抽出子プロセス）→ Searcher
の順に通し、追加・更新・壊れた更新・削除で検索結果が正しく変わることを確かめる。
"""

from __future__ import annotations

import os
import time

import pytest

from classbot.indexer import Indexer
from classbot.materials import MaterialStore
from classbot.scanner import Scanner
from classbot.search import Searcher


@pytest.fixture
def env(db, settings):
    store = MaterialStore(db, settings)
    indexer = Indexer(db, settings, store)
    queued: list[int] = []
    scanner = Scanner(db, settings, store, queued.append)

    def sync() -> None:
        scanner.scan()
        while queued:
            indexer.index_material(queued.pop(0))

    return store, Searcher(db, settings), sync


def _texts(searcher, q):
    return [h.text for h in searcher.search(q).hits]


def _touch_old(path):
    t = time.time() - 30
    os.utime(path, (t, t))


def _pdf(path, text):
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text, fontsize=11, fontname="japan")
    doc.save(path)
    doc.close()
    _touch_old(path)


def test_add_update_delete_cycle_real_extractor(env, settings):
    store, searcher, sync = env
    path = settings.materials_dir / "第04回_演習.pdf"
    _pdf(path, "第4回の演習テーマは線形回帰です")
    sync()
    row = store.get_server("第04回_演習.pdf")
    assert row["status"] == "ready" and row["chunk_count"] == 1
    assert any("線形回帰" in t for t in _texts(searcher, "線形回帰"))

    # 内容を差し替え → 旧内容は検索されず、新内容が検索される
    _pdf(path, "第4回の演習テーマはロジスティック回帰に変更しました")
    sync()
    assert not any("線形回帰" in t for t in _texts(searcher, "線形回帰"))
    assert any("ロジスティック" in t for t in _texts(searcher, "ロジスティック回帰"))
    assert store.get_server("第04回_演習.pdf")["id"] == row["id"]  # 同じIDのまま更新

    # 壊れたファイルで上書き → 最後に成功した版で検索を継続
    path.write_bytes(b"%PDF-1.7\n" + b"\x00garbage" * 100)
    _touch_old(path)
    sync()
    bad = store.get_server("第04回_演習.pdf")
    assert bad["status"] == "ready" and bad["last_error"]
    assert any("ロジスティック" in t for t in _texts(searcher, "ロジスティック回帰"))

    # 削除 → 検索対象から外れる
    path.unlink()
    sync()
    assert store.get_server("第04回_演習.pdf") is None
    assert searcher.search("ロジスティック回帰").hits == []


def test_same_name_server_and_slack_are_separate(env, db, settings):
    """サーバー配置とSlack登録で同名ファイルがあっても別資料として共存し、
    サーバー側の同期がSlack登録資料を消さない。"""
    store, searcher, sync = env
    (settings.materials_dir / "シラバス.md").write_text("# 評価\nサーバー版の評価方法は試験です", encoding="utf-8")
    _touch_old(settings.materials_dir / "シラバス.md")
    sync()
    stored = "deadbeef__シラバス.md"
    (settings.uploads_dir / stored).write_text("# 評価\nSlack版の評価方法はレポートです", encoding="utf-8")
    mid = store.create_slack("シラバス.md", stored, "md", 10, "UADMIN")
    Indexer(db, settings, store).index_material(mid)

    names = [(h.display_name, h.material_id) for h in searcher.search("評価方法").hits]
    assert len({m for _, m in names}) == 2
    # サーバー側のファイルを消しても Slack 登録資料は残る
    (settings.materials_dir / "シラバス.md").unlink()
    sync()
    assert store.get(mid) is not None
    texts = _texts(searcher, "評価方法")
    assert texts and all("Slack版" in t for t in texts)
