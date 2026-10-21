import asyncio
import os
import time

import pytest

from tests._fake_extract import ensure_extract_module

ensure_extract_module()

from classbot.materials import MaterialStore  # noqa: E402
from classbot.scanner import Scanner, is_ignored_name  # noqa: E402


@pytest.fixture
def env(db, settings):
    store = MaterialStore(db, settings)
    queued: list[int] = []
    return store, Scanner(db, settings, store, queued.append), queued


def _mark_indexed(db, mid, digest):
    db.execute("UPDATE materials SET status='ready', indexed_hash=?, current_hash=?, "
               "active_version=1 WHERE id=?", (digest, digest, mid))


def test_detects_add_update_delete(env, db, settings):
    store, scanner, queued = env
    p = settings.materials_dir / "第01回_授業概要.md"
    p.write_text("v1", encoding="utf-8")
    (settings.materials_dir / "sub").mkdir()
    (settings.materials_dir / "sub" / "課題.txt").write_text("課題", encoding="utf-8")
    rep = scanner.scan()
    assert len(rep.added) == 2 and len(queued) == 2
    mid = store.get_server("第01回_授業概要.md")["id"]
    assert store.get_server("sub/課題.txt") is not None

    from classbot.safety import sha256_file
    _mark_indexed(db, mid, sha256_file(p))
    queued.clear()

    # 変更なし → 再処理しない
    assert scanner.scan().unchanged == 2 and queued == []

    # mtime だけ変更（内容同じ）→ ハッシュ比較で再処理しない
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime - 100))
    rep = scanner.scan()
    assert rep.changed == [] and queued == []

    # 内容変更 → 再処理
    p.write_text("v2 updated", encoding="utf-8")
    os.utime(p, (time.time() - 10, time.time() - 10))
    rep = scanner.scan()
    assert rep.changed == [mid] and queued == [mid]

    # 削除
    p.unlink()
    rep = scanner.scan()
    assert rep.removed == ["第01回_授業概要.md"]
    assert store.get(mid) is None


def test_skips_temp_hidden_and_unsupported(env, settings):
    _, scanner, queued = env
    for name in ("~$draft.docx", ".hidden.md", "a.pdf.part", "x.crdownload", "img.png",
                 "notes.swp"):
        (settings.materials_dir / name).write_text("x", encoding="utf-8")
    rep = scanner.scan()
    assert rep.added == [] and queued == []
    assert "img.png" in rep.skipped
    assert is_ignored_name("~$a.pptx") and not is_ignored_name("a.pptx")


def test_symlink_outside_materials_is_skipped(env, settings, tmp_path):
    _, scanner, queued = env
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    (settings.materials_dir / "link.txt").symlink_to(secret)
    rep = scanner.scan()
    assert rep.added == [] and "link.txt" in rep.skipped


def test_unstable_file_is_deferred(env, settings):
    _, scanner, queued = env
    settings.file_stable_sec = 60
    (settings.materials_dir / "copying.md").write_text("x", encoding="utf-8")
    rep = scanner.scan()
    assert rep.needs_rescan and queued == []


def test_startup_requeues_interrupted(env, db, settings):
    store, scanner, queued = env
    (settings.materials_dir / "a.md").write_text("x", encoding="utf-8")
    scanner.scan()
    mid = queued[0]
    db.execute("UPDATE materials SET status='indexing' WHERE id=?", (mid,))
    queued.clear()
    assert scanner.scan().requeued == [] and queued == []  # 通常スキャンでは触らない
    rep = scanner.scan(startup=True)
    assert rep.requeued == [mid] and queued == [mid]


def test_failed_same_content_not_retried(env, db, settings):
    store, scanner, queued = env
    p = settings.materials_dir / "bad.md"
    p.write_text("x", encoding="utf-8")
    scanner.scan()
    mid = queued.pop()
    from classbot.safety import sha256_file
    db.execute("UPDATE materials SET status='failed', current_hash=?, mtime=0 WHERE id=?",
               (sha256_file(p), mid))
    rep = scanner.scan()
    assert queued == [] and rep.changed == []


def test_watchdog_triggers_debounced_callback(env, settings):
    _, scanner, _ = env

    async def run():
        loop = asyncio.get_running_loop()
        fired = asyncio.Event()
        count = {"n": 0}

        def on_change():
            count["n"] += 1
            fired.set()

        assert scanner.start_watchdog(loop, on_change, debounce_sec=0.3)
        try:
            for i in range(3):
                (settings.materials_dir / f"w{i}.md").write_text("x", encoding="utf-8")
            await asyncio.wait_for(fired.wait(), timeout=5)
            await asyncio.sleep(0.5)
        finally:
            scanner.stop()
        return count["n"]

    assert asyncio.run(run()) == 1
