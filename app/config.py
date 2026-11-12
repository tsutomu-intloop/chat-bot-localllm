"""アプリケーション設定 (環境変数から読み込み)."""
import os
import secrets
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # Cloud Run 上の Local LLM の URL (例: https://ollama-xxxxx-an.a.run.app)
    llm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", "http://localhost:11434").rstrip("/"))
    # "ollama" (/api/chat) または "openai" (/v1/chat/completions: vLLM, llama.cpp server, Ollama 互換API 等)
    llm_api_type: str = field(default_factory=lambda: os.getenv("LLM_API_TYPE", "ollama").lower())
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gemma3:4b"))
    # OpenAI 互換サーバで API キーが必要な場合
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    # API キーを送るヘッダ. Authorization の場合は "Bearer <key>"、それ以外 (X-API-Key 等) はキーをそのまま送る
    llm_api_key_header: str = field(default_factory=lambda: os.getenv("LLM_API_KEY_HEADER", "Authorization"))
    # Cloud Run の IAM 認証 (--no-allow-unauthenticated) で保護された LLM を呼ぶ場合 true
    llm_use_id_token: bool = field(default_factory=lambda: _bool("LLM_USE_ID_TOKEN", False))
    llm_timeout: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT", "300")))
    system_prompt: str = field(default_factory=lambda: os.getenv(
        "SYSTEM_PROMPT", "あなたは親切で有能な日本語アシスタントです。"))
    # Ollama のコンテキスト長 (num_ctx). Ollama の既定は小さく、長いファイルが切り捨てられるため指定する. 0 で未指定
    llm_num_ctx: int = field(default_factory=lambda: int(os.getenv("LLM_NUM_CTX", "16384")))
    # LLM に送る直近メッセージ数の上限 (コンテキスト長対策)
    max_history_messages: int = field(default_factory=lambda: int(os.getenv("MAX_HISTORY_MESSAGES", "40")))

    # Firestore (未指定時は実行環境の既定プロジェクト / (default) データベース)
    firestore_project: str = field(default_factory=lambda: os.getenv("FIRESTORE_PROJECT", ""))
    firestore_database: str = field(default_factory=lambda: os.getenv("FIRESTORE_DATABASE", "(default)"))
    # ---------- ファイルアップロード ----------
    # Cloud Storage のバケット名. 未指定ならローカルディスク (files_local_dir) に保存
    files_bucket: str = field(default_factory=lambda: os.getenv("FILES_BUCKET", ""))
    files_local_dir: str = field(default_factory=lambda: os.getenv("FILES_LOCAL_DIR", "./data/uploads"))
    max_upload_mb: int = field(default_factory=lambda: int(os.getenv("MAX_UPLOAD_MB", "20")))
    max_files_per_conversation: int = field(default_factory=lambda: int(os.getenv("MAX_FILES_PER_CONVERSATION", "20")))
    # 1 回の回答で LLM に渡すファイル本文の上限文字数. 超える場合は質問に関連する部分だけを渡す
    file_context_chars: int = field(default_factory=lambda: int(os.getenv("FILE_CONTEXT_CHARS", "8000")))
    file_chunk_chars: int = field(default_factory=lambda: int(os.getenv("FILE_CHUNK_CHARS", "1200")))

    session_secret: str = field(default_factory=lambda: os.getenv("SESSION_SECRET") or secrets.token_urlsafe(32))
    # "local": ユーザ名/パスワードでログイン, "iap": Identity-Aware Proxy のヘッダでユーザ識別
    auth_mode: str = field(default_factory=lambda: os.getenv("AUTH_MODE", "local").lower())
    allow_signup: bool = field(default_factory=lambda: _bool("ALLOW_SIGNUP", True))
    cookie_secure: bool = field(default_factory=lambda: _bool("COOKIE_SECURE", False))


settings = Settings()
