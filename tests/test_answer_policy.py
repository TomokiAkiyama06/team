"""回答方針（資料にない技術的な質問への対応）のテスト。

授業資料に存在しない Ubuntu のエラーや Python の例外を、本物の Indexer / Searcher / プロンプト
組み立て / 出典検証 / QA ワーカーに通す。Claude は偽物（実 API は呼ばない）なので、ここで検証するのは
「モデルに渡す指示・データ」と「回答の後処理」であり、実モデルの回答品質は実接続時に確認する
（docs/PRODUCTION_CHECKLIST.md 6.2）。"""

import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from classbot import images as images_mod
from classbot.budget import BudgetManager, Usage
from classbot.indexer import Indexer
from classbot.jobs import KIND_QA, JobQueue
from classbot.llm import LLMResponse
from classbot.materials import MaterialStore
from classbot.pricing import PriceTable
from classbot.prompts import (COURSE_SPECIFIC_NOTE, DEFAULT_SYSTEM_PROMPT, SUFFICIENCY_NOTES,
                              build_system_prompt, is_course_specific)
from classbot.qa_worker import QAWorker
from classbot.safety import mask_secrets
from classbot.search import Searcher

# 授業資料（Ubuntu の apt ロックや Python の例外には触れていない）
MATERIALS = {
    "第03回_課題.md": "# 第3回 課題\n第3回の課題はレポート提出です。締切は10月31日 23:59、Moodle から提出します。\n",
    "第05回_SSH演習.md": "# SSH 公開鍵認証\nssh-keygen -t ed25519 で鍵を作成し、ssh-copy-id で公開鍵をサーバーに登録します。\n"
                       "接続は ssh -i ~/.ssh/id_ed25519 student@192.168.56.10 で行います。\n",
}

APT_LOCK = ("sudo apt update したら次のエラーが出ました。どうすればいいですか？\n"
            "E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 2345 (unattended-upgr)\n"
            "N: Be aware that removing the lock file is not a solution and may break your system.\n"
            "E: Unable to acquire the dpkg frontend lock (/var/lib/dpkg/lock-frontend), is another process using it?")

PY_TRACEBACK = ("python3 app.py を実行するとエラーになります\n"
                "Traceback (most recent call last):\n"
                '  File "/home/student/app.py", line 3, in <module>\n'
                "    import requests\n"
                "ModuleNotFoundError: No module named 'requests'")

GENERAL_ANSWER = ("*考えられる原因*: 自動更新（unattended-upgrades）が apt を使用中です。\n"
                  "*確認方法*:\n```\nps aux | grep -i apt\n```\n"
                  "*解決手順*:\n1. 数分待ってから再実行します。\n"
                  "2. ロックファイルは削除しないでください（パッケージ管理が壊れる可能性があります）。\n"
                  "[[SOURCES: なし]]")


class FakeSlack:
    def __init__(self):
        self.posts, self.updates, self.files, self.blobs = [], [], {}, {}

    async def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": f"9{len(self.posts)}.0"}

    async def chat_update(self, **kw):
        self.updates.append(kw)
        return {"ok": True}

    async def files_info(self, file):
        return {"file": self.files[file]}


class FakeLLM:
    def __init__(self, text=GENERAL_ANSWER):
        self.text, self.calls = text, []

    async def answer(self, system, messages):
        self.calls.append((system, messages))
        return LLMResponse(text=self.text, stop_reason="end_turn", model="claude-sonnet-5-5",
                           usage=Usage(2000, 400, 0, 0), request_id="req_policy")


@pytest.fixture
def env(settings, db, monkeypatch):
    settings.claude_model = "claude-sonnet-5-5"
    settings.image_event_grace_sec = 0
    store = MaterialStore(db, settings)
    indexer = Indexer(db, settings, store)
    for name, text in MATERIALS.items():
        p = settings.materials_dir / name
        p.write_text(text, encoding="utf-8")
        mid = store.upsert_server(name, p.stat().st_size, p.stat().st_mtime, "md")
        assert indexer.index_material(mid).status == "ready"
    queue = JobQueue(db)
    budget = BudgetManager(db, settings, PriceTable.load(settings.pricing_file))
    slack, llm = FakeSlack(), FakeLLM()
    qa = QAWorker(settings, db, queue, Searcher(db, settings), budget, llm, slack)
    return SimpleNamespace(s=settings, db=db, queue=queue, slack=slack, llm=llm, qa=qa,
                           searcher=Searcher(db, settings), monkeypatch=monkeypatch)


async def ask(env, text, ts="1.0", thread_ts=None, **extra):
    env.queue.enqueue(KIND_QA, {"channel": "C123", "user": "U1", "ts": ts, "thread_ts": thread_ts or ts,
                                "text": text, **extra})
    await env.qa.handle(env.queue.claim(KIND_QA))
    system, messages = env.llm.calls[-1]
    last = messages[-1]["content"]
    last_text = last if isinstance(last, str) else last[-1]["text"]
    return system, messages, last_text, env.slack.updates[-1]["text"]


# ---------------------------------------------------------------- システムプロンプト

def test_system_prompt_policies(settings):
    system = build_system_prompt(settings.system_prompt_file)
    required = [
        "資料に載っていない」ことを理由に回答をあきらめてはいけません",   # 7
        "Ubuntu / Linux、SSH、Python、WordPress、ネットワーク、Web サーバー",  # 3
        "考えられる原因", "確認方法", "解決手順",                           # 4
        "必要な情報",                                                       # 6
        "スクリーンショット",                                               # 5
        "出典IDの付いていない部分は一般的な知識",                          # 8
        "第一優先は登録された授業資料、第二優先はあなた（AI）自身の知識",   # 9 資料 → AI の知識の優先順位
        "*補足（AIの一般知識）*",                                            #   AI の知識で補った部分を区別
        "断り書きや「次にできること」の案内だけで回答を終えてはいけません",
        "それぞれに答えます",                                                #   複数の質問の一部で止めない
        "根拠がない場合は推測で答えず",                                     # 10
        "バックアップ",                                                     # 11
        "伏せる",                                                           # 12
        "バージョンによって異なる場合があります",                          # 13
        "[[SOURCES:",                                                       # 14 出典検証のプロトコルは維持
    ]
    for phrase in required:
        assert phrase in system, phrase
    # 旧方針（資料にない質問には毎回「確認できませんでした」と冒頭で明示）を残していない
    assert "と最初に明示し" not in system


def test_default_prompt_and_notes_do_not_force_refusal():
    assert "あなた自身の知識" in DEFAULT_SYSTEM_PROMPT
    assert "第一優先は登録された授業資料" in DEFAULT_SYSTEM_PROMPT
    for key in ("sufficient", "weak", "none"):
        assert "AIの一般知識" in SUFFICIENCY_NOTES[key]
        assert "「登録された資料からは確認できませんでした」と明示" not in SUFFICIENCY_NOTES[key]
    assert "断り書きや確認の依頼だけで回答を終えないでください" in SUFFICIENCY_NOTES["none"]
    # 授業固有の質問でも、資料に無い値を作らないだけで、他の部分や一般的な補足は答える
    assert "作り出さず" in COURSE_SPECIFIC_NOTE
    assert "省略せずに答えてください" in COURSE_SPECIFIC_NOTE


# ---------------------------------------------------------------- 資料にない技術的な質問

async def test_ubuntu_apt_lock_error_not_in_materials(env):
    result = env.searcher.search(APT_LOCK)
    assert result.sufficiency in ("none", "weak")
    assert not any("課題" in h.display_name for h in result.hits if h.coverage > 0.5)

    system, messages, last_text, final = await ask(env, APT_LOCK)
    assert "Could not get lock /var/lib/dpkg/lock-frontend" in last_text   # エラー全文を渡す
    assert f'status="{result.sufficiency}"' in last_text
    assert SUFFICIENCY_NOTES[result.sufficiency] in last_text
    assert "<course_policy_note>" not in last_text                          # 授業固有の質問ではない
    assert "<today>" in last_text                                           # 情報の新しさの判断材料
    # 回答（一般知識・出典なし）はそのまま届き、出典は付かない
    assert "解決手順" in final and "ps aux | grep -i apt" in final
    assert "出典：" not in final and "[[SOURCES" not in final
    assert "確認できませんでした" not in final


async def test_python_exception_not_in_materials(env):
    env.llm.text = ("`requests` がインストールされていません。\n```\npython3 -m pip install requests\n```\n"
                    "仮想環境を使っている場合は有効化してから実行してください。\n[[SOURCES: なし]]")
    system, messages, last_text, final = await ask(env, PY_TRACEBACK)
    assert "ModuleNotFoundError: No module named 'requests'" in last_text
    assert "＜module＞" in last_text and "<module>" not in last_text  # データ中のタグは無害化
    assert "pip install requests" in final and "出典：" not in final


async def test_technical_question_still_prefers_course_material(env):
    """資料に関連する技術的な質問では、RAG の結果を優先して出典を表示する。"""
    env.llm.text = ("授業資料の手順では ed25519 の鍵を使います[S1]。\n"
                    "Permission denied (publickey) は公開鍵が登録されていない場合によく起きます。\n[[SOURCES: S1]]")
    q = "SSH の公開鍵認証で ssh-copy-id のあと Permission denied (publickey) が出ます"
    system, messages, last_text, final = await ask(env, q)
    assert "第05回_SSH演習.md" in last_text
    assert "出典：第05回_SSH演習.md" in final


async def test_followup_in_thread_keeps_context(env):
    await ask(env, APT_LOCK, ts="10.0")
    env.llm.text = "`ps` の結果に unattended-upgr があれば、完了を待ってください。\n[[SOURCES: なし]]"
    system, messages, last_text, final = await ask(env, "ps の結果を見ましたが、まだ動いています", ts="11.0",
                                                   thread_ts="10.0")
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert "Could not get lock" in messages[0]["content"]


# ---------------------------------------------------------------- 授業固有の情報

@pytest.mark.parametrize("q,expected", [
    ("第5回の課題の締切はいつですか？", True),
    ("レポートの提出方法を教えて", True),
    ("出席は成績に入りますか", True),
    ("sudo apt update でエラーになります", False),
    ("WordPress の管理画面に入れません", False),
])
def test_course_specific_detection(q, expected):
    assert is_course_specific(q) is expected


async def test_course_specific_question_without_material_is_flagged(env):
    env.llm.text = "登録された資料からは確認できませんでした。授業担当者に確認してください。\n[[SOURCES: なし]]"
    system, messages, last_text, final = await ask(env, "第7回の課題の締切と採点基準を教えて")
    assert COURSE_SPECIFIC_NOTE in last_text
    assert "根拠がない場合は推測で答えず" in system


# ---------------------------------------------------------------- 秘密情報のマスキング

@pytest.mark.parametrize("text,secret", [
    ("DB_PASSWORD=hunter2pass でつながりません", "hunter2pass"),
    ("define( 'DB_PASSWORD', 'wpPass123!' ); の後に Error establishing a database connection", "wpPass123!"),
    ("export ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnop", "abcdefghijklmnop"),
    ("mysql -u root -pS3cret! wordpress で ERROR 1045", "S3cret!"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA\n-----END OPENSSH PRIVATE KEY-----",
     "b3BlbnNzaC1rZXktdjEAAAA"),
    ("postgres://alice:pa55word@db.example.com/app に接続できない", "pa55word"),
    ("Authorization: Bearer abcdef123456 を付けています", "abcdef123456"),
])
def test_mask_secrets(text, secret):
    masked, n = mask_secrets(text)
    assert n >= 1 and secret not in masked


@pytest.mark.parametrize("code", [
    "token = request.headers.get('x')",
    "password = input('pw: ')",
    "api_key: str = os.environ['KEY']",
    "password_hash = bcrypt.hashpw(pw, salt)",
    APT_LOCK,
    PY_TRACEBACK,
])
def test_mask_secrets_keeps_code_and_errors(code):
    assert mask_secrets(code) == (code, 0)


async def test_secrets_masked_before_api_and_history(env):
    q = ("wp-config.php を直したのに Error establishing a database connection です\n"
         "define( 'DB_PASSWORD', 'wpPass123!' );\nANTHROPIC_API_KEY=sk-ant-api03-zzzzzzzzzzzz")
    system, messages, last_text, final = await ask(env, q)
    sent = json.dumps(messages, ensure_ascii=False)
    assert "wpPass123!" not in sent and "zzzzzzzzzzzz" not in sent
    assert "<masked_secrets_note>" in last_text
    stored = env.db.query_one("SELECT text FROM conversation_turns WHERE role='user'")["text"]
    assert "wpPass123!" not in stored
    assert "伏せ字にしてから AI に送信しました" in final


# ---------------------------------------------------------------- エラーのスクリーンショット

async def test_error_screenshot_is_sent_with_question(env):
    buf = io.BytesIO()
    Image.new("RGB", (800, 300), (0, 0, 0)).save(buf, "PNG")
    env.slack.files["S1"] = {"id": "S1", "name": "error.png", "mimetype": "image/png", "size": 1000,
                             "mode": "hosted", "url_private_download": "https://files.slack.com/files-pri/T1-S1/x.png"}

    async def fake_download(url, dest, token, max_bytes, **kw):
        dest.write_bytes(buf.getvalue())
        return len(buf.getvalue())

    env.monkeypatch.setattr(images_mod, "download_slack_file", fake_download)
    system, messages, last_text, final = await ask(env, "このエラーの直し方を教えて", image_file_ids=["S1"])
    blocks = messages[-1]["content"]
    assert [b["type"] for b in blocks] == ["image", "text"]
    assert base64.b64decode(blocks[0]["source"]["data"])[:4] == b"\x89PNG"
    assert "スクリーンショットが添付された場合は、画像内のエラーメッセージ" in system
