"""Cloud Run 上の Local LLM (Ollama / OpenAI 互換) へのクライアント."""
from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator

import httpx

from .config import settings


class LLMError(Exception):
    pass


class _IdTokenCache:
    """Cloud Run の IAM 認証用 ID トークン (メタデータサーバ / ADC から取得) をキャッシュ."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._expiry: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self, audience: str) -> str:
        async with self._lock:
            if self._token and time.time() < self._expiry - 300:
                return self._token
            token = await asyncio.to_thread(self._fetch, audience)
            self._token = token
            self._expiry = time.time() + 3600
            return token

    @staticmethod
    def _fetch(audience: str) -> str:
        import google.auth.transport.requests
        import google.oauth2.id_token

        req = google.auth.transport.requests.Request()
        return google.oauth2.id_token.fetch_id_token(req, audience)


_id_tokens = _IdTokenCache()


async def _headers() -> dict[str, str]:
    """LLM へのリクエストヘッダ.

    Cloud Run の IAM 用 ID トークンは X-Serverless-Authorization で送る
    (Cloud Run が検証後に取り除くため、Authorization ヘッダを LLM アプリ側の API キーに使える).
    """
    headers = {"Content-Type": "application/json"}
    if settings.llm_use_id_token:
        token = await _id_tokens.get(settings.llm_base_url)
        headers["X-Serverless-Authorization"] = f"Bearer {token}"
    if settings.llm_api_key:
        name = settings.llm_api_key_header
        if name.lower() == "authorization":
            headers[name] = f"Bearer {settings.llm_api_key}"
        else:
            headers[name] = settings.llm_api_key
    return headers


def _auth_hint(status: int) -> str:
    if status not in (401, 403):
        return ""
    if settings.llm_api_key:
        return " (LLM_API_KEY / LLM_API_KEY_HEADER が LLM サーバの設定と一致しているか確認してください)"
    return (" (LLM サーバが API キーを要求している可能性があります。LLM_API_KEY を設定してください。"
            "Cloud Run の IAM で拒否された場合は LLM_USE_ID_TOKEN=true と run.invoker 権限を確認してください)")


async def list_models() -> list[str]:
    """利用可能なモデル一覧. 取得できない場合は既定モデルのみ返す."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if settings.llm_api_type == "openai":
                r = await client.get(f"{settings.llm_base_url}/v1/models", headers=await _headers())
                r.raise_for_status()
                names = [m["id"] for m in r.json().get("data", [])]
            else:
                r = await client.get(f"{settings.llm_base_url}/api/tags", headers=await _headers())
                r.raise_for_status()
                names = [m["name"] for m in r.json().get("models", [])]
    except Exception:
        names = []
    if settings.llm_model not in names:
        names.insert(0, settings.llm_model)
    return names


async def stream_chat(messages: list[dict], model: str | None = None) -> AsyncIterator[str]:
    """チャット応答をトークン単位でストリーミング."""
    model = model or settings.llm_model
    headers = await _headers()
    timeout = httpx.Timeout(settings.llm_timeout, connect=30)

    if settings.llm_api_type == "openai":
        url = f"{settings.llm_base_url}/v1/chat/completions"
        payload = {"model": model, "messages": messages, "stream": True}
    else:
        url = f"{settings.llm_base_url}/api/chat"
        payload = {"model": model, "messages": messages, "stream": True}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")[:500]
                    raise LLMError(f"LLM サーバエラー ({resp.status_code}): {body}{_auth_hint(resp.status_code)}")
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    if settings.llm_api_type == "openai":
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        chunk = json.loads(data)
                        choices = chunk.get("choices") or []
                        if choices:
                            delta = choices[0].get("delta", {}).get("content")
                            if delta:
                                yield delta
                    else:
                        chunk = json.loads(line)
                        if chunk.get("error"):
                            raise LLMError(str(chunk["error"]))
                        delta = chunk.get("message", {}).get("content")
                        if delta:
                            yield delta
                        if chunk.get("done"):
                            break
    except httpx.HTTPError as e:
        raise LLMError(f"LLM サーバへの接続に失敗しました: {e}") from e
