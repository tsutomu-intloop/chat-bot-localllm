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
    # Cloud Run の IAM 認証 (--no-allow-unauthenticated) で保護された LLM を呼ぶ場合 true
    llm_use_id_token: bool = field(default_factory=lambda: _bool("LLM_USE_ID_TOKEN", False))
    llm_timeout: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT", "300")))
    system_prompt: str = field(default_factory=lambda: os.getenv(
        "SYSTEM_PROMPT", "あなたは親切で有能な日本語アシスタントです。"))
    # LLM に送る直近メッセージ数の上限 (コンテキスト長対策)
    max_history_messages: int = field(default_factory=lambda: int(os.getenv("MAX_HISTORY_MESSAGES", "40")))

    # Firestore (未指定時は実行環境の既定プロジェクト / (default) データベース)
    firestore_project: str = field(default_factory=lambda: os.getenv("FIRESTORE_PROJECT", ""))
    firestore_database: str = field(default_factory=lambda: os.getenv("FIRESTORE_DATABASE", "(default)"))
    session_secret: str = field(default_factory=lambda: os.getenv("SESSION_SECRET") or secrets.token_urlsafe(32))
    # "local": ユーザ名/パスワードでログイン, "iap": Identity-Aware Proxy のヘッダでユーザ識別
    auth_mode: str = field(default_factory=lambda: os.getenv("AUTH_MODE", "local").lower())
    allow_signup: bool = field(default_factory=lambda: _bool("ALLOW_SIGNUP", True))
    cookie_secure: bool = field(default_factory=lambda: _bool("COOKIE_SECURE", False))


settings = Settings()
