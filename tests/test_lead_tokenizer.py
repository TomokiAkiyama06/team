from classbot.app import Bot
from classbot.jobs import KIND_REINDEX_ALL, JobQueue
from classbot.textnorm import TOKENIZER_VERSION


async def test_tokenizer_change_schedules_full_reindex(settings, db):
    bot = Bot(settings)
    bot.db.init_schema()
    # 資料が無い初回起動では再インデックス不要、バージョンだけ記録
    assert bot.check_tokenizer_version() is False
    assert bot.db.query_one("SELECT value FROM meta WHERE key='tokenizer_version'")["value"] == TOKENIZER_VERSION
    assert bot.check_tokenizer_version() is False

    bot.store.upsert_server("a.md", 10, 1.0, "md")
    bot.db.execute("UPDATE meta SET value='1' WHERE key='tokenizer_version'")  # 旧規則で作られた索引
    assert bot.check_tokenizer_version() is True
    job = JobQueue(bot.db).claim(KIND_REINDEX_ALL)
    assert job is not None and job.payload["force"] is True
    assert bot.check_tokenizer_version() is False
