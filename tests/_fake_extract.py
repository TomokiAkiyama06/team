"""テスト用: extract.extract_to_jsonl の代替（抽出ワーカー A に依存しない）。"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FakeResult:
    ok: bool
    status: str
    chunks_path: Path | None
    page_count: int | None
    chunk_count: int
    error: str | None


def ensure_extract_module() -> None:
    """classbot.extract が未実装でも indexer を import できるようにする。"""
    try:
        import classbot.extract  # noqa: F401
    except ImportError:
        mod = types.ModuleType("classbot.extract")
        mod.ExtractResult = FakeResult
        mod.extract_to_jsonl = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stub"))
        sys.modules["classbot.extract"] = mod


def make_fake(mode: str = "ok"):
    """ファイル内容の各行を 1 チャンクとして書き出す偽抽出器。"""
    calls: list[Path] = []

    def fake(path: Path, file_type: str, settings, out_path: Path):
        calls.append(path)
        if mode == "fail":
            return FakeResult(False, "failed", None, None, 0, "壊れたファイルです")
        if mode == "no_text":
            return FakeResult(False, "no_text", None, 3, 0, "テキストを抽出できませんでした")
        if mode == "crash":
            raise RuntimeError("boom")
        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        with out_path.open("w", encoding="utf-8") as f:
            for i, line in enumerate(lines):
                f.write(json.dumps({"seq": i, "text": line, "location": f"L{i + 1}",
                                    "page": None, "heading": None}, ensure_ascii=False) + "\n")
        return FakeResult(True, "ready", out_path, None, len(lines), None)

    fake.calls = calls
    return fake
