"""Local LLM チャットボット (FastAPI + Firestore)."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import llm, store
from .auth import current_user, hash_password, verify_password
from .config import settings

log = logging.getLogger("chatbot")
STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_TITLE = "新しい会話"

app = FastAPI(title="Local LLM Chatbot")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="chat_session",
    max_age=60 * 60 * 24 * 30,
    same_site="lax",
    https_only=settings.cookie_secure,
)


# ---------- schemas ----------
class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=4, max_length=256)


class ConversationCreate(BaseModel):
    title: str | None = None
    model: str | None = None
    system_prompt: str | None = None


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    pinned: bool | None = None
    model: str | None = None
    system_prompt: str | None = None


class SendMessage(BaseModel):
    content: str = Field(min_length=1)
    # 指定したメッセージ以降を削除してから送信 (メッセージ編集・再送)
    edit_message_id: str | None = None


class Duplicate(BaseModel):
    # 指定メッセージまでをコピーして新しい会話に分岐. 省略時は全件.
    upto_message_id: str | None = None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def conv_out(c: dict) -> dict:
    return {
        "id": c["id"], "title": c["title"], "model": c.get("model"),
        "system_prompt": c.get("system_prompt"), "pinned": c.get("pinned", False),
        "created_at": _iso(c.get("created_at")), "updated_at": _iso(c.get("updated_at")),
    }


def msg_out(m: dict) -> dict:
    return {"id": m["id"], "role": m["role"], "content": m["content"], "created_at": _iso(m.get("created_at"))}


def own_conversation(user: dict, conv_id: str) -> dict:
    conv = store.get_conversation(user["id"], conv_id)
    if conv is None:
        raise HTTPException(404, "会話が見つかりません")
    return conv


# ---------- auth ----------
@app.get("/api/config")
def get_config():
    return {
        "auth_mode": settings.auth_mode,
        "allow_signup": settings.allow_signup,
        "default_model": settings.llm_model,
        "default_system_prompt": settings.system_prompt,
    }


@app.post("/api/auth/signup")
def signup(body: Credentials, request: Request):
    if settings.auth_mode != "local" or not settings.allow_signup:
        raise HTTPException(403, "新規登録は無効です")
    try:
        user = store.create_user(body.username.strip(), hash_password(body.password))
    except store.AlreadyExists:
        raise HTTPException(409, "このユーザ名は既に使われています")
    request.session["user_id"] = user["id"]
    return {"id": user["id"], "username": user["username"]}


@app.post("/api/auth/login")
def login(body: Credentials, request: Request):
    if settings.auth_mode != "local":
        raise HTTPException(400, "このモードではログイン不要です")
    user = store.get_user_by_username(body.username.strip())
    if user is None or not verify_password(body.password, user.get("password_hash")):
        raise HTTPException(401, "ユーザ名またはパスワードが違います")
    request.session["user_id"] = user["id"]
    return {"id": user["id"], "username": user["username"]}


@app.post("/api/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return {"id": user["id"], "username": user["username"], "auth_mode": settings.auth_mode}


# ---------- models ----------
@app.get("/api/models")
async def models(_: dict = Depends(current_user)):
    return {"models": await llm.list_models(), "default": settings.llm_model}


# ---------- conversations ----------
@app.get("/api/conversations")
def list_conversations(q: str | None = None, user: dict = Depends(current_user)):
    return {"conversations": [conv_out(c) for c in store.list_conversations(user["id"], q)]}


@app.post("/api/conversations")
def create_conversation(body: ConversationCreate, user: dict = Depends(current_user)):
    conv = store.create_conversation(user["id"], (body.title or DEFAULT_TITLE).strip()[:255],
                                     body.model, body.system_prompt)
    return conv_out(conv)


@app.get("/api/conversations/{conv_id}")
def get_conversation(conv_id: str, user: dict = Depends(current_user)):
    conv = own_conversation(user, conv_id)
    msgs = store.list_messages(user["id"], conv_id)
    return {**conv_out(conv), "messages": [msg_out(m) for m in msgs]}


@app.patch("/api/conversations/{conv_id}")
def update_conversation(conv_id: str, body: ConversationUpdate, user: dict = Depends(current_user)):
    conv = own_conversation(user, conv_id)
    data = body.model_dump(exclude_unset=True)
    fields: dict = {}
    if data.get("title") is not None and data["title"].strip():
        fields["title"] = data["title"].strip()
    if data.get("pinned") is not None:
        fields["pinned"] = data["pinned"]
    if "model" in data:
        fields["model"] = data["model"] or None
    if "system_prompt" in data:
        fields["system_prompt"] = data["system_prompt"] or None
    store.update_conversation(user["id"], conv_id, fields)
    return conv_out({**conv, **fields})


@app.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: str, user: dict = Depends(current_user)):
    own_conversation(user, conv_id)
    store.delete_conversation(user["id"], conv_id)
    return {"ok": True}


@app.post("/api/conversations/{conv_id}/duplicate")
def duplicate_conversation(conv_id: str, body: Duplicate, user: dict = Depends(current_user)):
    """過去の会話をコピー (指定メッセージまでで分岐) して再利用する."""
    own_conversation(user, conv_id)
    return conv_out(store.duplicate_conversation(user["id"], conv_id, body.upto_message_id))


@app.get("/api/conversations/{conv_id}/export")
def export_conversation(conv_id: str, format: str = "md", user: dict = Depends(current_user)):
    conv = own_conversation(user, conv_id)
    msgs = store.list_messages(user["id"], conv_id)
    if format == "json":
        data = {**conv_out(conv), "messages": [msg_out(m) for m in msgs]}
        return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="{conv_id}.json"'})
    lines = [f"# {conv['title']}", ""]
    for m in msgs:
        lines += [f"## {'ユーザ' if m['role'] == 'user' else 'アシスタント'}", "", m["content"], ""]
    return PlainTextResponse("\n".join(lines), media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{conv_id}.md"'})


# ---------- chat ----------
def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _build_llm_messages(conv: dict, msgs: list[dict]) -> list[dict]:
    history = [{"role": m["role"], "content": m["content"]} for m in msgs if m["content"]]
    history = history[-settings.max_history_messages:]
    system = conv.get("system_prompt") or settings.system_prompt
    return ([{"role": "system", "content": system}] if system else []) + history


def _stream_reply(user_id: str, conv: dict, msgs: list[dict], extra: dict | None = None) -> StreamingResponse:
    llm_messages = _build_llm_messages(conv, msgs)

    async def gen():
        parts: list[str] = []
        error: str | None = None
        saved = None
        try:
            if extra:
                yield _sse({"type": "meta", **extra})
            try:
                async for delta in llm.stream_chat(llm_messages, conv.get("model")):
                    parts.append(delta)
                    yield _sse({"type": "delta", "content": delta})
            except llm.LLMError as e:
                error = str(e)
            except Exception as e:  # noqa: BLE001
                log.exception("LLM streaming failed")
                error = f"予期しないエラー: {e}"
        finally:
            # 途中で切断されても生成済みの内容は保存する
            content = "".join(parts)
            if content:
                saved = await asyncio.to_thread(store.add_message, user_id, conv["id"], "assistant", content)
        if error:
            yield _sse({"type": "error", "message": error})
        yield _sse({"type": "done", "message_id": saved["id"] if saved else None})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/conversations/{conv_id}/messages")
def send_message(conv_id: str, body: SendMessage, user: dict = Depends(current_user)):
    uid = user["id"]
    conv = own_conversation(user, conv_id)

    if body.edit_message_id:
        target = store.get_message(uid, conv_id, body.edit_message_id)
        if target is None:
            raise HTTPException(404, "メッセージが見つかりません")
        store.truncate_messages(uid, conv_id, target["seq"])

    msgs = store.list_messages(uid, conv_id)
    extra_fields = {}
    if not msgs and conv["title"] == DEFAULT_TITLE:
        extra_fields["title"] = body.content.strip().splitlines()[0][:40] or DEFAULT_TITLE
        conv["title"] = extra_fields["title"]
    user_msg = store.add_message(uid, conv_id, "user", body.content, extra_fields, existing=msgs)
    return _stream_reply(uid, conv, msgs + [user_msg], {"user_message_id": user_msg["id"], "title": conv["title"]})


@app.post("/api/conversations/{conv_id}/regenerate")
def regenerate(conv_id: str, user: dict = Depends(current_user)):
    uid = user["id"]
    conv = own_conversation(user, conv_id)
    msgs = store.list_messages(uid, conv_id)
    if not msgs:
        raise HTTPException(400, "メッセージがありません")
    if msgs[-1]["role"] == "assistant":
        store.truncate_messages(uid, conv_id, msgs[-1]["seq"])
        msgs = msgs[:-1]
    return _stream_reply(uid, conv, msgs)


# ---------- health / static ----------
@app.get("/healthz")
def healthz():
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
