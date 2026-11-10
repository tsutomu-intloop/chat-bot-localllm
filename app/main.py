"""Local LLM チャットボット (FastAPI)."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import llm
from .auth import current_user, hash_password, verify_password
from .config import settings
from .db import Conversation, Message, SessionLocal, User, get_db, init_db, utcnow

log = logging.getLogger("chatbot")
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Local LLM Chatbot", lifespan=lifespan)
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


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def conv_dict(c: Conversation, preview: str | None = None) -> dict:
    return {
        "id": c.id,
        "title": c.title,
        "model": c.model,
        "system_prompt": c.system_prompt,
        "pinned": c.pinned,
        "created_at": _iso(c.created_at),
        "updated_at": _iso(c.updated_at),
        **({"preview": preview} if preview is not None else {}),
    }


def msg_dict(m: Message) -> dict:
    return {"id": m.id, "role": m.role, "content": m.content, "created_at": _iso(m.created_at)}


def own_conversation(db: Session, user: User, conv_id: str) -> Conversation:
    conv = db.get(Conversation, conv_id)
    if conv is None or conv.user_id != user.id:
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
def signup(body: Credentials, request: Request, db: Session = Depends(get_db)):
    if settings.auth_mode != "local" or not settings.allow_signup:
        raise HTTPException(403, "新規登録は無効です")
    username = body.username.strip()
    if db.scalar(select(User).where(User.username == username)):
        raise HTTPException(409, "このユーザ名は既に使われています")
    user = User(username=username, password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    request.session["user_id"] = user.id
    return {"id": user.id, "username": user.username}


@app.post("/api/auth/login")
def login(body: Credentials, request: Request, db: Session = Depends(get_db)):
    if settings.auth_mode != "local":
        raise HTTPException(400, "このモードではログイン不要です")
    user = db.scalar(select(User).where(User.username == body.username.strip()))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "ユーザ名またはパスワードが違います")
    request.session["user_id"] = user.id
    return {"id": user.id, "username": user.username}


@app.post("/api/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "username": user.username, "auth_mode": settings.auth_mode}


# ---------- models ----------
@app.get("/api/models")
async def models(_: User = Depends(current_user)):
    return {"models": await llm.list_models(), "default": settings.llm_model}


# ---------- conversations ----------
@app.get("/api/conversations")
def list_conversations(q: str | None = None, user: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    stmt = select(Conversation).where(Conversation.user_id == user.id)
    if q:
        like = f"%{q}%"
        matched = select(Message.conversation_id).where(Message.content.ilike(like))
        stmt = stmt.where(or_(Conversation.title.ilike(like), Conversation.id.in_(matched)))
    stmt = stmt.order_by(Conversation.pinned.desc(), Conversation.updated_at.desc())
    convs = db.scalars(stmt).all()
    return {"conversations": [conv_dict(c) for c in convs]}


@app.post("/api/conversations")
def create_conversation(body: ConversationCreate, user: User = Depends(current_user),
                        db: Session = Depends(get_db)):
    conv = Conversation(user_id=user.id, title=(body.title or "新しい会話").strip()[:255],
                        model=body.model, system_prompt=body.system_prompt)
    db.add(conv)
    db.commit()
    return conv_dict(conv)


@app.get("/api/conversations/{conv_id}")
def get_conversation(conv_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    conv = own_conversation(db, user, conv_id)
    return {**conv_dict(conv), "messages": [msg_dict(m) for m in conv.messages]}


@app.patch("/api/conversations/{conv_id}")
def update_conversation(conv_id: str, body: ConversationUpdate, user: User = Depends(current_user),
                        db: Session = Depends(get_db)):
    conv = own_conversation(db, user, conv_id)
    data = body.model_dump(exclude_unset=True)
    if "title" in data and data["title"] is not None:
        conv.title = data["title"].strip() or conv.title
    if "pinned" in data and data["pinned"] is not None:
        conv.pinned = data["pinned"]
    if "model" in data:
        conv.model = data["model"] or None
    if "system_prompt" in data:
        conv.system_prompt = data["system_prompt"] or None
    db.commit()
    return conv_dict(conv)


@app.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    conv = own_conversation(db, user, conv_id)
    db.delete(conv)
    db.commit()
    return {"ok": True}


@app.post("/api/conversations/{conv_id}/duplicate")
def duplicate_conversation(conv_id: str, body: Duplicate, user: User = Depends(current_user),
                           db: Session = Depends(get_db)):
    """過去の会話をコピー (指定メッセージまでで分岐) して再利用する."""
    src = own_conversation(db, user, conv_id)
    new = Conversation(user_id=user.id, title=f"{src.title} (コピー)"[:255],
                       model=src.model, system_prompt=src.system_prompt)
    db.add(new)
    db.flush()
    for i, m in enumerate(src.messages):
        db.add(Message(conversation_id=new.id, seq=i, role=m.role, content=m.content))
        if body.upto_message_id and m.id == body.upto_message_id:
            break
    db.commit()
    return conv_dict(new)


@app.get("/api/conversations/{conv_id}/export")
def export_conversation(conv_id: str, format: str = "md", user: User = Depends(current_user),
                        db: Session = Depends(get_db)):
    conv = own_conversation(db, user, conv_id)
    if format == "json":
        data = {**conv_dict(conv), "messages": [msg_dict(m) for m in conv.messages]}
        return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="{conv.id}.json"'})
    lines = [f"# {conv.title}", ""]
    for m in conv.messages:
        lines += [f"## {'ユーザ' if m.role == 'user' else 'アシスタント'}", "", m.content, ""]
    return PlainTextResponse("\n".join(lines), media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{conv.id}.md"'})


# ---------- chat ----------
def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _build_llm_messages(conv: Conversation) -> list[dict]:
    history = [{"role": m.role, "content": m.content} for m in conv.messages if m.content]
    history = history[-settings.max_history_messages:]
    system = conv.system_prompt or settings.system_prompt
    return ([{"role": "system", "content": system}] if system else []) + history


def _next_seq(db: Session, conv_id: str) -> int:
    cur = db.scalar(select(func.max(Message.seq)).where(Message.conversation_id == conv_id))
    return (cur if cur is not None else -1) + 1


def _stream_reply(conv_id: str, extra: dict | None = None) -> StreamingResponse:
    async def gen():
        db = SessionLocal()
        parts: list[str] = []
        error: str | None = None
        try:
            conv = db.get(Conversation, conv_id)
            if extra:
                yield _sse({"type": "meta", **extra})
            try:
                async for delta in llm.stream_chat(_build_llm_messages(conv), conv.model):
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
            msg = None
            if content:
                msg = Message(conversation_id=conv_id, seq=_next_seq(db, conv_id),
                              role="assistant", content=content)
                db.add(msg)
                conv = db.get(Conversation, conv_id)
                conv.updated_at = utcnow()
                db.commit()
            db.close()
        if error:
            yield _sse({"type": "error", "message": error})
        yield _sse({"type": "done", "message_id": msg.id if msg else None})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/conversations/{conv_id}/messages")
def send_message(conv_id: str, body: SendMessage, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    conv = own_conversation(db, user, conv_id)

    if body.edit_message_id:
        target = db.get(Message, body.edit_message_id)
        if target is None or target.conversation_id != conv.id:
            raise HTTPException(404, "メッセージが見つかりません")
        db.execute(delete(Message).where(Message.conversation_id == conv.id, Message.seq >= target.seq))
        db.flush()

    is_first = db.scalar(select(func.count()).select_from(Message)
                         .where(Message.conversation_id == conv.id)) == 0
    user_msg = Message(conversation_id=conv.id, seq=_next_seq(db, conv.id), role="user",
                       content=body.content)
    db.add(user_msg)
    if is_first and conv.title == "新しい会話":
        conv.title = body.content.strip().splitlines()[0][:40] or conv.title
    conv.updated_at = utcnow()
    db.commit()
    return _stream_reply(conv.id, {"user_message_id": user_msg.id, "title": conv.title})


@app.post("/api/conversations/{conv_id}/regenerate")
def regenerate(conv_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    conv = own_conversation(db, user, conv_id)
    msgs = conv.messages
    if not msgs:
        raise HTTPException(400, "メッセージがありません")
    if msgs[-1].role == "assistant":
        db.delete(msgs[-1])
        db.commit()
    return _stream_reply(conv.id)


# ---------- health / static ----------
@app.get("/healthz")
def healthz():
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
