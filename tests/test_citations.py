from classbot.citations import parse_answer
from classbot.search import SearchHit


def hit(sid, mid, name, loc):
    return SearchHit(sid, 1, mid, name, loc, "text", 1.0, 1.0)


HITS = [hit("S1", 1, "第3回講義資料.pdf", "p.12"), hit("S2", 2, "課題提出方法.md", "見出し「提出」"),
        hit("S3", 1, "第3回講義資料.pdf", "p.12")]


def test_valid_sources_footer():
    p = parse_answer("課題は二分探索です[S1]。\n[[SOURCES: S1]]", HITS)
    assert p.footer == "出典：第3回講義資料.pdf p.12 [S1]"
    assert "[[SOURCES" not in p.body
    assert p.body == "課題は二分探索です[S1]。"


def test_invalid_ids_removed():
    p = parse_answer("答えです[S9]。提出はLMS[S2]。\n[[SOURCES: S2, S7]]", HITS)
    assert "S9" not in p.body and "[S2]" in p.body
    assert set(p.invalid_ids) == {"S9", "S7"}
    assert p.footer == "出典：課題提出方法.md 見出し「提出」 [S2]"


def test_none_declared_no_footer():
    p = parse_answer("登録された資料からは確認できませんでした。\n[[SOURCES: なし]]", HITS)
    assert p.footer == "" and p.used_hits == []
    assert p.render() == "登録された資料からは確認できませんでした。"


def test_dedupe_same_material_location_and_multi():
    p = parse_answer("A[S1, S3] B【S2】\n[[SOURCES: S1,S2,S3]]", HITS)
    assert p.footer.splitlines() == [
        "出典：第3回講義資料.pdf p.12 [S1][S3]",
        "出典：課題提出方法.md 見出し「提出」 [S2]",
    ]


def test_missing_sources_line_uses_inline():
    p = parse_answer("内容です[S2]", HITS)
    assert [h.source_id for h in p.used_hits] == ["S2"]


def test_no_hits_any_id_invalid():
    p = parse_answer("嘘の出典[S1]\n[[SOURCES: S1]]", [])
    assert p.footer == "" and p.invalid_ids == ["S1"] and "[S1]" not in p.body


def test_fullwidth_brackets_and_ids_normalized():
    # 日本語文脈でモデルが全角括弧・全角数字を使っても検証される
    p = parse_answer("提出はLMS［Ｓ２］。架空［S9］\n［［SOURCES：Ｓ２］］", HITS)
    assert "[S2]" in p.body and "S9" not in p.body and "Ｓ" not in p.body
    assert [h.source_id for h in p.used_hits] == ["S2"] and p.invalid_ids == ["S9"]
    assert "SOURCES" not in p.render()


def test_loose_sources_line_removed():
    # 二重括弧を省いた出典行も本文に残さない
    p = parse_answer("内容です[S1]\nSOURCES: S1, S7", HITS)
    assert p.body == "内容です[S1]" and p.invalid_ids == ["S7"]


def test_sources_tag_mid_line_and_body_word_sources_kept():
    p = parse_answer("内容です。[[SOURCES: S2]]", HITS)
    assert p.body == "内容です。" and [h.source_id for h in p.used_hits] == ["S2"]
    # 本文中の単語 "sources" は消さない
    q = parse_answer("open sources について[S1]\n[[SOURCES: S1]]", HITS)
    assert q.body.startswith("open sources について")


def test_footer_built_only_from_db_metadata():
    # 本文にモデルが書いたファイル名ではなく、検索ヒットのメタデータから出典を作る
    p = parse_answer("「架空資料.pdf p.99」によると[S1]\n[[SOURCES: S1]]", HITS)
    assert p.footer == "出典：第3回講義資料.pdf p.12 [S1]"


def test_footer_one_line_per_material():
    hits = [hit("S1", 7, "proxb_41.pdf", "p.1"), hit("S2", 7, "proxb_41.pdf", "p.3"),
            hit("S3", 7, "proxb_41.pdf", "p.3"), hit("S4", 8, "課題提出方法.md", "")]
    p = parse_answer("A[S1] B[S2][S3] C[S4]\n[[SOURCES: S1, S2, S3, S4]]", hits)
    assert p.footer.splitlines() == [
        "出典：proxb_41.pdf p.1 [S1]、p.3 [S2][S3]",
        "出典：課題提出方法.md [S4]",
    ]
