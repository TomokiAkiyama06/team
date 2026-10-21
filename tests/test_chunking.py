from classbot.chunking import Segment, chunk_segments


def test_chunks_do_not_cross_segments():
    segs = [Segment("一ページ目の本文です。", "p.1", 1), Segment("二ページ目の本文です。", "p.2", 2)]
    chunks = list(chunk_segments(segs, 800, 100))
    assert [c.location for c in chunks] == ["p.1", "p.2"]
    assert [c.seq for c in chunks] == [0, 1]
    assert chunks[1].page == 2


def test_long_segment_split_at_sentence_boundaries():
    text = "".join(f"これは{i}番目の文です。" for i in range(200))
    chunks = list(chunk_segments([Segment(text, "p.3", 3)], 200, 30))
    assert len(chunks) > 5
    assert all(len(c.text) <= 200 for c in chunks)
    assert all(c.location == "p.3" for c in chunks)
    # 大半のチャンクは文末で終わる
    assert sum(c.text.endswith("。") for c in chunks) >= len(chunks) - 1


def test_very_long_sentence_is_hard_split():
    text = "あ" * 1000
    chunks = list(chunk_segments([Segment(text, "L1-1")], 300, 50))
    assert all(len(c.text) <= 300 for c in chunks)


def test_duplicate_chunks_removed():
    footer = "© 2026 授業名 無断転載禁止"
    segs = [Segment(footer, f"スライド{i}", i) for i in range(1, 6)]
    segs.append(Segment("固有の本文", "スライド6", 6))
    chunks = list(chunk_segments(segs, 800, 100))
    assert len(chunks) == 2
    assert chunks[0].location == "スライド1"


def test_empty_segments_skipped():
    segs = [Segment("   \n\n ", "p.1", 1), Segment("本文", "p.2", 2)]
    chunks = list(chunk_segments(segs, 800, 100))
    assert len(chunks) == 1 and chunks[0].location == "p.2"


def test_whitespace_normalized():
    chunks = list(chunk_segments([Segment("a   b\n\n\n\nc", "L1-5")], 800, 0))
    assert chunks[0].text == "a b\n\nc"


def test_english_paragraph_splits_at_sentence_boundaries():
    """改行のない英文の長い段落でも、単語の途中ではなく文末で分割する。"""
    from classbot.chunking import Segment, chunk_segments

    text = " ".join(f"Sentence {i} explains recursion with value 3.14 clearly." for i in range(40))
    chunks = list(chunk_segments([Segment(text, "p.1", 1)], size=300, overlap=0))
    assert len(chunks) > 1
    for c in chunks:
        assert c.text.endswith("clearly."), c.text[-30:]
        assert len(c.text) <= 300
    # 小数点では区切らない
    assert all("3.14" in c.text for c in chunks)
