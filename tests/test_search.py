from classbot.db import now_iso
from classbot.search import Searcher
from classbot.textnorm import index_tokens


def add_material(db, name, chunks, version=1):
    """chunks: list of (location, heading, text)"""
    with db.tx() as conn:
        mid = conn.execute(
            "INSERT INTO materials(source, rel_path, display_name, file_type, status, active_version,"
            " created_at, updated_at) VALUES ('server', ?, ?, 'pdf', 'ready', ?, ?, ?)",
            (name, name, version, now_iso(), now_iso())).lastrowid
        for seq, (loc, heading, text) in enumerate(chunks):
            cid = conn.execute(
                "INSERT INTO chunks(material_id, version, seq, location, heading, text)"
                " VALUES (?, ?, ?, ?, ?, ?)", (mid, version, seq, loc, heading, text)).lastrowid
            conn.execute(
                "INSERT INTO chunks_fts(rowid, title_tokens, body_tokens) VALUES (?, ?, ?)",
                (cid, index_tokens(f"{name} {heading or ''}"), index_tokens(text)))
    return mid


def test_lecture_number_ranks_matching_material(db, settings):
    add_material(db, "第01回_授業概要.pdf", [("p.1", None, "この授業の概要と評価方法について説明します。課題は毎回出します。")])
    add_material(db, "第03回_演習問題.pdf", [("p.2", None, "第3回の課題: 二分探索を実装し、計算量を考察せよ。")])
    add_material(db, "第05回_講義資料.pdf", [("p.4", None, "課題の提出はLMSから行います。ソートアルゴリズムの課題です。")])
    res = Searcher(db, settings).search("第3回の課題について教えて")
    assert res.hits
    assert res.hits[0].display_name == "第03回_演習問題.pdf"
    assert res.hits[0].source_id == "S1"
    assert res.hits[0].location == "p.2"


def test_two_char_japanese_word_found(db, settings):
    add_material(db, "課題提出方法.md", [("見出し「提出」", "提出", "締切は授業の翌週月曜日 23:59 です。")])
    res = Searcher(db, settings).search("締切はいつ？")
    assert res.hits and res.sufficiency == "sufficient"


def test_per_material_cap_and_near_dup(db, settings):
    settings.search_max_per_material = 2
    text = "再帰関数の基本とスタックの動きについて説明します。"
    add_material(db, "a.pdf", [(f"p.{i}", None, text + f" 例{i}は階乗。") for i in range(1, 6)])
    add_material(db, "b.pdf", [("p.1", None, "再帰関数を使ったフィボナッチ数列の計算。")])
    res = Searcher(db, settings).search("再帰関数")
    per = {}
    for h in res.hits:
        per[h.material_id] = per.get(h.material_id, 0) + 1
    assert max(per.values()) <= 2
    # a.pdf の各チャンクはほぼ同一内容なので重複除去で 1 件に減る
    a_hits = [h for h in res.hits if h.display_name == "a.pdf"]
    assert len(a_hits) == 1


def test_inactive_version_not_searched(db, settings):
    mid = add_material(db, "old.pdf", [("p.1", None, "量子コンピュータの古い説明")], version=1)
    db.execute("UPDATE materials SET active_version = 2 WHERE id = ?", (mid,))
    assert Searcher(db, settings).search("量子コンピュータ").hits == []


def test_sufficiency_none_and_weak(db, settings):
    add_material(db, "x.pdf", [("p.1", None, "データベースの正規化について。第一正規形と第二正規形。")])
    s = Searcher(db, settings)
    assert s.search("宇宙旅行の費用").sufficiency == "none"
    weak = s.search("データ構造とネットワークプロトコルとオペレーティングシステムの違い")
    assert weak.sufficiency in ("weak", "none")


def test_context_char_limit(db, settings):
    settings.search_context_max_chars = 100
    add_material(db, "big.pdf", [(f"p.{i}", None, f"機械学習の章{i}。" + "あ" * 60) for i in range(5)])
    res = Searcher(db, settings).search("機械学習")
    assert sum(len(h.text) for h in res.hits) <= 100


import pytest  # noqa: E402


@pytest.mark.parametrize("q", ['C++ の "ポインタ" * OR NOT', "NEAR(a b)", "^", '"', "''", "？？？",
                               "x" * 3000, "-- DROP TABLE chunks;"])
def test_fts_special_characters_do_not_raise(db, settings, q):
    """利用者の質問に FTS5 の演算子・記号が含まれても例外にならない。"""
    add_material(db, "a.pdf", [("p.1", None, "C++ のポインタと参照。NEAR AND OR NOT の説明。")])
    Searcher(db, settings).search(q)  # 例外が出ないこと
    assert db.query_one("SELECT COUNT(*) n FROM chunks")["n"] == 1


def test_fullwidth_and_halfwidth_kana_match(db, settings):
    add_material(db, "b.pdf", [("p.3", None, "ポインタ演算の注意点。")])
    assert Searcher(db, settings).search("ﾎﾟｲﾝﾀ").hits[0].location == "p.3"


def test_katakana_long_vowel_word_is_not_split(db, settings):
    from classbot.textnorm import query_tokens

    # 「データ」が「デ」「タ」の 1 文字トークンに崩れず bi-gram になる
    assert query_tokens("データ") == ["デー", "ータ"]
    add_material(db, "c.pdf", [("p.1", None, "コンピューターとプリンターの設計。")])
    add_material(db, "d.pdf", [("p.1", None, "データベースの正規化。")])
    hits = Searcher(db, settings).search("データ").hits
    assert hits[0].display_name == "d.pdf" and hits[0].coverage == 1.0
