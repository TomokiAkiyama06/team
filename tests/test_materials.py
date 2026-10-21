from tests._fake_extract import ensure_extract_module

ensure_extract_module()

from classbot.materials import MaterialStore  # noqa: E402


def test_upsert_server_is_idempotent(db, settings):
    store = MaterialStore(db, settings)
    a = store.upsert_server("第01回_授業概要.md", 10, 1.0, "md")
    b = store.upsert_server("第01回_授業概要.md", 20, 2.0, "md")
    assert a == b
    row = store.get(a)
    assert row["size_bytes"] == 20 and row["display_name"] == "第01回_授業概要.md"


def test_server_and_slack_same_name_are_separate(db, settings):
    store = MaterialStore(db, settings)
    a = store.upsert_server("シラバス.md", 1, 1.0, "md")
    b = store.create_slack("シラバス.md", "abc__シラバス.md", "md", 1, "UADMIN")
    assert a != b
    assert store.file_path(store.get(a)) == settings.materials_dir / "シラバス.md"
    assert store.file_path(store.get(b)) == settings.uploads_dir / "abc__シラバス.md"
    rows, total = store.list(0, 10)
    assert total == 2 and len(rows) == 2


def test_delete_moves_file_to_trash(db, settings):
    store = MaterialStore(db, settings)
    p = settings.materials_dir / "a.md"
    p.write_text("x", encoding="utf-8")
    mid = store.upsert_server("a.md", 1, 1.0, "md")
    deleted = store.delete(mid)
    assert deleted["id"] == mid
    assert not p.exists()
    assert any(f.name.endswith("__a.md") for f in settings.trash_dir.iterdir())
    assert store.get(mid) is None
    assert store.delete(mid) is None


def test_list_pagination(db, settings):
    store = MaterialStore(db, settings)
    for i in range(5):
        store.upsert_server(f"f{i}.md", 1, 1.0, "md")
    rows, total = store.list(2, 2)
    assert total == 5 and [r["display_name"] for r in rows] == ["f2.md", "f3.md"]
    assert store.status_counts() == {"pending": 5}
