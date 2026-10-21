"""環境変数から読み込むアプリケーション設定。

秘密情報（APIキー・トークン）は SecretStr で保持し、ログや repr に出さない。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Slack ---
    slack_bot_token: SecretStr = SecretStr("")
    slack_app_token: SecretStr = SecretStr("")
    slack_channel_id: str = ""
    slack_admin_user_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # 設定するとこのワークスペース（T...）以外のユーザーからのイベント・操作を無視する（Slack Connect 対策）
    slack_team_id: str = ""

    # --- Anthropic ---
    anthropic_api_key: SecretStr = SecretStr("")
    claude_model: str = ""
    claude_effort: str = "medium"  # low | medium | high | xhigh | max
    claude_max_tokens: int = 8000
    claude_timeout_sec: float = 120.0
    claude_max_retries: int = 3
    # 拒否時のサーバー側フォールバック（beta）。既定は無効。対応モデルは llm.FALLBACK_MODELS を参照
    claude_refusal_fallback: bool = False
    claude_prompt_cache: bool = True

    # --- コスト管理 ---
    daily_request_limit_per_user: int = 30
    monthly_budget_usd: float = 20.0
    max_concurrent_requests: int = 3
    admins_exempt_from_daily_limit: bool = True
    max_pending_per_user: int = 2
    # 入力トークン数の保守的推定に使う係数（1文字あたりのトークン数の上限見積もり）
    token_estimate_per_char: float = 1.2
    pricing_file: Path = Path("config/pricing.toml")
    timezone: str = "Asia/Tokyo"

    # --- パス ---
    materials_dir: Path = Path("materials")
    data_dir: Path = Path("data")
    system_prompt_file: Path = Path("config/system_prompt.md")

    # --- 資料処理 ---
    max_file_size_mb: int = 30
    max_pages: int = 2000
    max_extracted_chars: int = 5_000_000
    extract_timeout_sec: int = 300
    extract_max_memory_mb: int = 1536
    zip_max_uncompressed_mb: int = 300
    zip_max_entries: int = 5000
    zip_max_ratio: int = 200
    chunk_size_chars: int = 800
    chunk_overlap_chars: int = 120
    pptx_include_notes: bool = False
    ocr_enabled: bool = False
    ocr_language: str = "jpn+eng"
    scan_interval_sec: int = 300
    file_stable_sec: float = 5.0
    use_watchdog: bool = True

    # --- 検索・会話 ---
    search_top_k: int = 6
    search_max_per_material: int = 3
    search_context_max_chars: int = 12000
    history_max_turns: int = 6
    history_max_chars: int = 6000
    question_max_chars: int = 2000

    # --- 画像入力（学生の画像付き質問） ---
    image_input_enabled: bool = True
    image_max_per_message: int = 4        # 1つの投稿で受け付ける画像の枚数
    image_max_per_request: int = 6        # 1回の API 呼び出しに含める画像（スレッド履歴の画像を含む。20以下）
    image_max_file_mb: int = 10           # Slack から取得する元画像の上限
    image_max_source_pixels: int = 40_000_000  # 展開前に拒否する画素数（画像爆弾対策）
    image_max_long_edge: int = 1568       # 送信前に縮小する長辺(px)
    image_max_pixels: int = 1_200_000     # 送信前に縮小する総画素数（トークン数の上限を抑える）
    image_retention_hours: float = 24.0   # 処理済み画像の保存期間（0 = 回答後すぐ削除）
    image_cleanup_interval_sec: int = 600
    image_event_grace_sec: float = 2.0    # メンションと添付が別イベントで届く場合の待ち時間

    # --- Slack 操作 ---
    upload_pending_minutes: int = 10
    list_page_size: int = 15

    # --- 運用 ---
    log_level: str = "INFO"
    backup_keep: int = 14
    backup_interval_hours: int = 24
    heartbeat_interval_sec: int = 30
    socket_disconnect_exit_sec: int = 600
    qa_job_max_age_min: int = 15

    @field_validator("image_max_per_request")
    @classmethod
    def _cap_images(cls, v: int) -> int:
        # 21枚以上だと API の画像ごとの寸法上限が 2000px に厳しくなるため 20 枚までに制限する
        return max(0, min(int(v), 20))

    @field_validator("slack_admin_user_ids", mode="before")
    @classmethod
    def _split_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [s.strip() for s in v.replace(" ", ",").split(",") if s.strip()]
        return v

    # 派生パス
    @property
    def db_path(self) -> Path:
        return self.data_dir / "classbot.db"

    @property
    def uploads_dir(self) -> Path:
        """Slack から登録したファイルの保存先（materials/ とは分離）。"""
        return self.data_dir / "uploads"

    @property
    def trash_dir(self) -> Path:
        return self.data_dir / "trash"

    @property
    def images_dir(self) -> Path:
        """学生の質問画像（正規化済み）の一時保存先。保存期間を過ぎると自動削除する。"""
        return self.data_dir / "images"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def heartbeat_file(self) -> Path:
        return self.data_dir / "heartbeat.json"

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    def is_admin(self, user_id: str | None) -> bool:
        return bool(user_id) and user_id in self.slack_admin_user_ids

    def ensure_dirs(self) -> None:
        for d in (self.materials_dir, self.data_dir, self.uploads_dir, self.trash_dir,
                  self.tmp_dir, self.backup_dir, self.images_dir):
            d.mkdir(parents=True, exist_ok=True)

    def validate_for_bot(self) -> list[str]:
        """ボット起動に必須の設定の不足を返す（値そのものは返さない）。"""
        missing = []
        if not self.slack_bot_token.get_secret_value():
            missing.append("SLACK_BOT_TOKEN")
        if not self.slack_app_token.get_secret_value():
            missing.append("SLACK_APP_TOKEN")
        if not self.slack_channel_id:
            missing.append("SLACK_CHANNEL_ID")
        if not self.anthropic_api_key.get_secret_value():
            missing.append("ANTHROPIC_API_KEY")
        if not self.claude_model:
            missing.append("CLAUDE_MODEL")
        return missing


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
