"""アップロード・配置ファイルの安全性検査。

- ファイル名の無害化（パストラバーサル・制御文字対策）
- 拡張子とマジックバイトによる形式検証
- PPTX/DOCX（ZIP 形式）の ZIP 爆弾検査
- Slack へ投稿する外部由来文字列の無害化（メンション・リンク埋め込みの防止）
ファイルを実行・展開して書き出すことは一切しない。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from classbot.config import Settings

SUPPORTED_TYPES = {"pdf", "pptx", "docx", "txt", "md"}

_EXT_MAP = {
    ".pdf": "pdf",
    ".pptx": "pptx",
    ".docx": "docx",
    ".txt": "txt",
    ".md": "md",
    ".markdown": "md",
}

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# 表示を偽装できる不可視文字（RTL オーバーライド U+202E、ゼロ幅文字、行/段落区切りなど）。
# 絵文字の結合に使う ZWJ（U+200D）と異体字セレクタは表示に必要なため残す。
_KEEP_FORMAT_CHARS = {"\u200d", "\ufe0e", "\ufe0f"}


def strip_invisible(text: str) -> str:
    """Unicode カテゴリ Cc/Cf/Co/Cs/Zl/Zp の文字を除去する（改行・タブは残す）。"""
    return "".join(
        ch for ch in text
        if ch in "\n\t" or ch in _KEEP_FORMAT_CHARS
        or unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs", "Zl", "Zp"))


def escape_slack(text: str) -> str:
    """Slack のメッセージ書式で特別な意味を持つ & < > をエスケープする。
    <!channel> / <!here> / <@U…> / <https://…|…> などの埋め込みを無効化する。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def sanitize_for_slack(text: str) -> str:
    """モデル出力・資料名など外部由来の文字列を Slack に投稿する前の無害化。
    md_to_mrkdwn() のリンク変換より前に適用すること（変換で生成した <url|text> は残すため）。"""
    return escape_slack(strip_invisible(text))
_UNSAFE_CHARS_RE = re.compile(r'[/\\:*?"<>|]')
_MAX_NAME_BYTES = 200


class UnsupportedFileError(Exception):
    """利用者にそのまま表示できる日本語の理由を持つ検証エラー。"""


def safe_filename(name: str) -> str:
    """ファイル名からディレクトリ成分・制御文字・危険文字・先頭ドットを除去する。"""
    n = unicodedata.normalize("NFC", name or "")
    # 区切り文字を含む場合は最後の成分だけを使う（../../etc/passwd → passwd）
    n = n.replace("\\", "/").split("/")[-1]
    n = strip_invisible(_CONTROL_RE.sub("", n)).replace("\n", "").replace("\t", "")
    n = _UNSAFE_CHARS_RE.sub("_", n)
    n = n.strip().lstrip(".").rstrip(". ").strip()
    if not n:
        n = "file"
    # UTF-8 バイト長で制限（拡張子は保持）
    if len(n.encode("utf-8")) > _MAX_NAME_BYTES:
        stem, dot, ext = n.rpartition(".")
        if not dot or len(ext) > 10:
            stem, ext = n, ""
        ext_part = ("." + ext) if ext else ""
        budget = _MAX_NAME_BYTES - len(ext_part.encode("utf-8"))
        encoded = stem.encode("utf-8")[:budget]
        stem = encoded.decode("utf-8", errors="ignore")
        n = stem + ext_part
    return n


def detect_file_type(filename: str) -> str | None:
    """拡張子から資料形式を判定する。未対応なら None。"""
    return _EXT_MAP.get(Path(filename).suffix.lower())


def is_within(base: Path, target: Path) -> bool:
    """シンボリックリンクを解決した上で target が base 配下にあるか。"""
    try:
        return target.resolve().is_relative_to(base.resolve())
    except (OSError, RuntimeError):
        return False


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _check_zip(path: Path, file_type: str, settings: Settings) -> None:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise UnsupportedFileError(f"{file_type.upper()} ファイルとして読み込めません（壊れている可能性があります）") from e
    with zf:
        infos = zf.infolist()
        if len(infos) > settings.zip_max_entries:
            raise UnsupportedFileError("ファイル内部の要素数が多すぎるため処理できません")
        total = 0
        max_total = settings.zip_max_uncompressed_mb * 1024 * 1024
        for info in infos:
            total += info.file_size
            if total > max_total:
                raise UnsupportedFileError("展開後のサイズが大きすぎるため処理できません")
            if info.compress_size > 0 and info.file_size / info.compress_size > settings.zip_max_ratio \
                    and info.file_size > 1024 * 1024:
                raise UnsupportedFileError("異常な圧縮率のデータを含むため処理できません")
            if info.compress_size == 0 and info.file_size > 1024 * 1024:
                raise UnsupportedFileError("異常な圧縮率のデータを含むため処理できません")
        names = {i.filename for i in infos}
        required = "ppt/presentation.xml" if file_type == "pptx" else "word/document.xml"
        if "[Content_Types].xml" not in names or required not in names:
            raise UnsupportedFileError(f"{file_type.upper()} 形式として正しくありません")


def validate_file(path: Path, file_type: str, settings: Settings) -> None:
    """サイズ・マジックバイト・ZIP 爆弾を検査する。問題があれば UnsupportedFileError。"""
    if file_type not in SUPPORTED_TYPES:
        raise UnsupportedFileError(
            "未対応の形式です（対応形式: PDF, PPTX, DOCX, TXT, Markdown）")
    try:
        size = path.stat().st_size
    except OSError as e:
        raise UnsupportedFileError("ファイルを読み込めませんでした") from e
    if size == 0:
        raise UnsupportedFileError("ファイルが空です")
    if size > settings.max_file_size_bytes:
        raise UnsupportedFileError(
            f"ファイルサイズが上限（{settings.max_file_size_mb}MB）を超えています")
    with open(path, "rb") as f:
        head = f.read(8192)
    if file_type == "pdf":
        # 仕様上 %PDF- は先頭 1024 バイト以内にあればよい
        if b"%PDF-" not in head[:1024]:
            raise UnsupportedFileError("PDF 形式として正しくありません")
    elif file_type in ("pptx", "docx"):
        if not head.startswith(b"PK\x03\x04"):
            raise UnsupportedFileError(f"{file_type.upper()} 形式として正しくありません")
        _check_zip(path, file_type, settings)
    else:  # txt / md
        if b"\x00" in head:
            raise UnsupportedFileError("テキストファイルとして読み込めません（バイナリデータを含みます）")


# ---------------------------------------------------------------------------- 質問中の秘密情報
# 学生がログや設定を貼り付けたときに含まれがちな秘密情報。Anthropic API へ送る前と
# 会話履歴に保存する前に伏せ字にする（完全な検出は不可能なため、回答でも伏せ字を案内する）。
_MASK = "****"
_QUESTION_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
     "-----BEGIN PRIVATE KEY-----（秘密鍵のため省略）-----END PRIVATE KEY-----"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"), "sk-ant-" + _MASK),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "sk-" + _MASK),
    (re.compile(r"\bxox[a-z]-[A-Za-z0-9-]{8,}"), "xox-" + _MASK),
    (re.compile(r"\bxapp-[A-Za-z0-9-]{8,}"), "xapp-" + _MASK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "gh_" + _MASK),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA" + _MASK),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "AIza" + _MASK),
    # URL 中の認証情報（mysql://user:pass@host）
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(@)"), r"\1" + _MASK + r"\2"),
    # wp-config.php: define( 'DB_PASSWORD', '...' ); / AUTH_KEY など
    (re.compile(r"(?i)(define\(\s*['\"](?:DB_PASSWORD|[A-Z_]*(?:KEY|SALT))['\"]\s*,\s*['\"])[^'\"]+(['\"])"),
     r"\1" + _MASK + r"\2"),
    # Authorization ヘッダー
    (re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic|token)\s+)\S+"), r"\1" + _MASK),
    # key=value / key: value 形式（.env、設定ファイル、コマンドライン）
    (re.compile(r"(?i)\b((?:[a-z0-9_]*(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|token|private[_-]?key))"
                r"[\"']?\s*[:=]\s*[\"']?)"
                # コード中の式（request.headers.get(...)、os.getenv(...)、input() など）や型注釈は対象外
                r"(?!\*{4})(?![A-Za-z_][A-Za-z0-9_]*[.(\[])(?!(?:None|True|False|null|str|int|bytes|Optional)\b)"
                r"[^\s\"',;()]{4,}"), r"\1" + _MASK),
    # mysql -p<password> / sshpass -p <password>
    (re.compile(r"(\b(?:mysql|mysqldump|mariadb)\b[^\n]*?\s-p)(?!\s)(\S+)"), r"\1" + _MASK),
    (re.compile(r"(\bsshpass\s+-p\s*)(\S+)"), r"\1" + _MASK),
]


def mask_secrets(text: str) -> tuple[str, int]:
    """質問文中の秘密情報らしき文字列を伏せ字にする。戻り値: (伏せ字後の文字列, 置換件数)。"""
    total = 0
    for pat, repl in _QUESTION_SECRET_PATTERNS:
        text, n = pat.subn(repl, text)
        total += n
    return text, total
