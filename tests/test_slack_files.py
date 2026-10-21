"""Slack ファイルの認証付きダウンロード（ネットワークに出ない偽セッションで検証）。"""

import pytest

from classbot import slack_files
from classbot.slack_files import DownloadError, download_slack_file


class FakeContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, n):
        for c in self.chunks:
            yield c


class FakeResp:
    def __init__(self, status=200, ctype="image/png", chunks=(b"abc",)):
        self.status = status
        self.headers = {"Content-Type": ctype}
        self.content = FakeContent(list(chunks))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    requests = []
    response = FakeResp()

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, headers=None, allow_redirects=True):
        FakeSession.requests.append({"url": url, "headers": headers, "allow_redirects": allow_redirects})
        return FakeSession.response


@pytest.fixture
def fake_session(monkeypatch):
    FakeSession.requests = []
    FakeSession.response = FakeResp()
    monkeypatch.setattr(slack_files.aiohttp, "ClientSession", FakeSession)
    return FakeSession


URL = "https://files.slack.com/files-pri/T1-F1/download/a.png"


async def test_download_ok_sends_token_only_to_slack(fake_session, tmp_path):
    n = await download_slack_file(URL, tmp_path / "out", "xoxb-secret", 1000)
    assert n == 3 and (tmp_path / "out").read_bytes() == b"abc"
    req = fake_session.requests[0]
    assert req["headers"] == {"Authorization": "Bearer xoxb-secret"} and req["allow_redirects"] is False


@pytest.mark.parametrize("url", [
    "http://files.slack.com/x",                 # https 以外
    "https://evil.example.com/x",              # 許可外ホスト
    "https://files.slack.com.evil.example/x",  # 似たホスト名
    "https://evil.example/@files.slack.com/x",
    "",
])
async def test_download_refuses_non_slack_hosts(fake_session, tmp_path, url):
    with pytest.raises(DownloadError):
        await download_slack_file(url, tmp_path / "out", "xoxb-secret", 1000)
    assert fake_session.requests == []  # トークンを送っていない


@pytest.mark.parametrize("resp", [FakeResp(status=302), FakeResp(status=403),
                                  FakeResp(ctype="text/html; charset=utf-8")])
async def test_download_rejects_redirect_error_and_html(fake_session, tmp_path, resp):
    fake_session.response = resp
    with pytest.raises(DownloadError, match="files:read"):
        await download_slack_file(URL, tmp_path / "out", "xoxb-secret", 1000)
    assert not (tmp_path / "out").exists()


async def test_download_byte_cap_removes_partial_file(fake_session, tmp_path):
    fake_session.response = FakeResp(chunks=[b"x" * 600, b"x" * 600])
    with pytest.raises(DownloadError, match="上限") as ei:
        await download_slack_file(URL, tmp_path / "out", "xoxb-secret", 1000)
    assert not (tmp_path / "out").exists()
    assert "xoxb" not in str(ei.value) and "files.slack.com" not in str(ei.value)


async def test_material_worker_download_delegates(settings, monkeypatch, tmp_path):
    from classbot import material_worker as mw

    seen = {}

    async def fake(url, dest, token, max_bytes, **kw):
        seen.update(url=url, max_bytes=max_bytes)
        return 0

    monkeypatch.setattr(mw, "download_slack_file", fake)
    worker = mw.MaterialWorker(settings, None, None, None, None, None)
    await worker._download(URL, tmp_path / "o")
    assert seen == {"url": URL, "max_bytes": settings.max_file_size_bytes}
    assert mw.DownloadError is slack_files.DownloadError
