"""Local LLM チャットボット (FastAPI + Firestore)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

from urllib.parse import quote

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import export as exporter
from . import files as filesvc, llm, store
from .auth import current_user, hash_password, verify_password
from .config import settings

log = logging.getLogger("chatbot")
STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_TITLE = "新しい会話"

# 回答をファイルとして保存できることを LLM に伝え、「ファイルは作れません」と答えさせないための指示
FILE_OUTPUT_GUIDE = (
    "このチャットでは、ユーザはあなたの回答を Word・Excel・PowerPoint・Markdown・HTML などのファイルとして"
    "ダウンロードできます。ファイルの作成を頼まれたら「ファイルは作成できない」とは答えず、そのファイルの中身を"
    "回答として出力してください。文書は見出し（#）・箇条書き・表で構成し、表形式のデータは Markdown の表で、"
    "スライドは「## スライドのタイトル」ごとに箇条書きで書いてください。プログラム・CSV・JSON などはコードブロックで書き、"
    "ファイル名が決まっている場合は ```言語:ファイル名 の形式にしてください（例: ```csv:売上.csv）。"
    "前置きや補足は短くしてください。"
)

app = FastAPI(title="Local LLM Chatbot")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="chat_session",
    max_age=60 * 60 * 24 * 30,
    same_site="lax",
    https_only=settings.cookie_secure,
)


@app.middleware("http")
async def no_cache_for_ui(request: Request, call_next):
    """画面 (HTML/JS/CSS) は毎回更新を確認させ、デプロイ後に古い JavaScript が使われないようにする."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def _asset_version(name: str) -> str:
    return hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]


def _render_index() -> str:
    """JS/CSS の URL に内容のハッシュを付け、更新時に確実に新しいファイルを読み込ませる."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "style.css"):
        html = html.replace(f"/static/{name}", f"/static/{name}?v={_asset_version(name)}")
    return html


INDEX_HTML = _render_index()


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
    # このメッセージと一緒にアップロードしたファイル (表示用. 回答には会話の全ファイルが使われる)
    attachment_ids: list[str] = Field(default_factory=list)


class Duplicate(BaseModel):
    # 指定メッセージまでをコピーして新しい会話に分岐. 省略時は全件.
    upto_message_id: str | None = None


def content_disposition(filename: str) -> str:
    """日本語ファイル名用 (filename*) と、読めないクライアント向けの ASCII 名 (filename) を併記する."""
    ext = Path(filename).suffix
    ascii_name = filename.encode("ascii", "ignore").decode().strip(" ._") or f"output{ext}"
    if not ascii_name.endswith(ext) or ascii_name == ext:
        ascii_name = f"output{ext}"
    ascii_name = ascii_name.replace('"', "")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def conv_out(c: dict) -> dict:
    return {
        "id": c["id"], "title": c["title"], "model": c.get("model"),
        "system_prompt": c.get("system_prompt"), "pinned": c.get("pinned", False),
        "created_at": _iso(c.get("created_at")), "updated_at": _iso(c.get("updated_at")),
    }


def msg_out(m: dict) -> dict:
    return {"id": m["id"], "role": m["role"], "content": m["content"],
            "attachments": m.get("attachments") or [], "created_at": _iso(m.get("created_at"))}


def file_out(f: dict) -> dict:
    return {"id": f["id"], "name": f["name"], "content_type": f.get("content_type"), "size": f.get("size"),
            "chars": f.get("chars"), "created_at": _iso(f.get("created_at"))}


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
        "max_upload_mb": settings.max_upload_mb,
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
    files = store.list_files(user["id"], conv_id)
    return {**conv_out(conv), "messages": [msg_out(m) for m in msgs], "files": [file_out(f) for f in files]}


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
    filesvc.storage().delete_prefix(f"users/{user['id']}/conversations/{conv_id}/files")
    return {"ok": True}


@app.post("/api/conversations/{conv_id}/duplicate")
def duplicate_conversation(conv_id: str, body: Duplicate, user: dict = Depends(current_user)):
    """過去の会話をコピー (指定メッセージまでで分岐) して再利用する."""
    own_conversation(user, conv_id)
    uid = user["id"]
    new = store.duplicate_conversation(uid, conv_id, body.upto_message_id)
    # 添付ファイルも同じ ID でコピー (メッセージの添付表示がそのまま使える)
    st = filesvc.storage()
    for f in store.list_files(uid, conv_id):
        src, dst = filesvc.file_prefix(uid, conv_id, f["id"]), filesvc.file_prefix(uid, new["id"], f["id"])
        st.copy(f"{src}/original", f"{dst}/original")
        st.copy(f"{src}/text.txt", f"{dst}/text.txt")
        meta = {k: v for k, v in f.items() if k not in ("id", "created_at")}
        store.add_file(uid, new["id"], f["id"], {**meta, "text_key": f"{dst}/text.txt", "original_key": f"{dst}/original"})
    return conv_out(new)


@app.get("/api/conversations/{conv_id}/messages/{message_id}/export")
def export_message(conv_id: str, message_id: str, format: str = "docx", user: dict = Depends(current_user)):
    """回答を Word / Excel / PowerPoint / HTML / Markdown / テキストのファイルに変換してダウンロードする."""
    if format not in exporter.FORMATS:
        raise HTTPException(400, f"未対応の形式です: {format}")
    conv = own_conversation(user, conv_id)
    msg = store.get_message(user["id"], conv_id, message_id)
    if msg is None:
        raise HTTPException(404, "メッセージが見つかりません")
    title = exporter.title_of(msg["content"], conv["title"])
    try:
        data = exporter.convert(msg["content"], format, title)
    except Exception as e:  # noqa: BLE001
        log.exception("export failed")
        raise HTTPException(500, f"ファイルの作成に失敗しました: {e}")
    media_type, ext = exporter.FORMATS[format]
    filename = exporter.safe_filename(title, ext)
    return Response(data, media_type=media_type,
                    headers={"Content-Disposition": content_disposition(filename)})


# ---------- files ----------
@app.post("/api/conversations/{conv_id}/files")
async def upload_files(conv_id: str, files: list[UploadFile] = File(...), user: dict = Depends(current_user)):
    """ファイルをアップロードし、テキストを抽出して会話に添付する."""
    uid = user["id"]
    await run_in_threadpool(own_conversation, user, conv_id)
    existing = await run_in_threadpool(store.list_files, uid, conv_id)
    if len(existing) + len(files) > settings.max_files_per_conversation:
        raise HTTPException(400, f"1 つの会話に添付できるファイルは {settings.max_files_per_conversation} 件までです")

    limit = settings.max_upload_mb * 1024 * 1024
    results, errors = [], []
    for up in files:
        name = Path(up.filename or "file").name[:200]
        data = await up.read(limit + 1)
        if len(data) > limit:
            errors.append(f"{name}: {settings.max_upload_mb}MB を超えています")
            continue
        try:
            text = await run_in_threadpool(filesvc.extract_text, name, data)
        except filesvc.UnsupportedFile as e:
            errors.append(str(e))
            continue
        file_id = store.new_id()
        prefix = filesvc.file_prefix(uid, conv_id, file_id)
        st = filesvc.storage()
        await run_in_threadpool(st.put, f"{prefix}/original", data, up.content_type or "")
        await run_in_threadpool(st.put, f"{prefix}/text.txt", text.encode("utf-8"), "text/plain; charset=utf-8")
        meta = {"name": name, "content_type": up.content_type or "", "size": len(data), "chars": len(text),
                "text_key": f"{prefix}/text.txt", "original_key": f"{prefix}/original"}
        results.append(file_out(await run_in_threadpool(store.add_file, uid, conv_id, file_id, meta)))
    if not results and errors:
        raise HTTPException(400, " / ".join(errors))
    return {"files": results, "errors": errors}


@app.get("/api/conversations/{conv_id}/files")
def list_files(conv_id: str, user: dict = Depends(current_user)):
    own_conversation(user, conv_id)
    return {"files": [file_out(f) for f in store.list_files(user["id"], conv_id)]}


@app.get("/api/conversations/{conv_id}/files/{file_id}")
def download_file(conv_id: str, file_id: str, user: dict = Depends(current_user)):
    own_conversation(user, conv_id)
    f = store.get_file(user["id"], conv_id, file_id)
    if f is None:
        raise HTTPException(404, "ファイルが見つかりません")
    data = filesvc.storage().get(f["original_key"])
    return Response(data, media_type=f.get("content_type") or "application/octet-stream",
                    headers={"Content-Disposition": content_disposition(f["name"])})


@app.delete("/api/conversations/{conv_id}/files/{file_id}")
def delete_file(conv_id: str, file_id: str, user: dict = Depends(current_user)):
    own_conversation(user, conv_id)
    if store.get_file(user["id"], conv_id, file_id) is None:
        raise HTTPException(404, "ファイルが見つかりません")
    store.delete_file(user["id"], conv_id, file_id)
    filesvc.storage().delete_prefix(filesvc.file_prefix(user["id"], conv_id, file_id))
    return {"ok": True}


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


def _build_llm_messages(conv: dict, msgs: list[dict], files: list[dict] | None = None) -> list[dict]:
    history = [{"role": m["role"], "content": m["content"]} for m in msgs if m["content"]]
    history = history[-settings.max_history_messages:]
    system = conv.get("system_prompt") or settings.system_prompt
    system = f"{system}\n\n{FILE_OUTPUT_GUIDE}" if system else FILE_OUTPUT_GUIDE
    if files:
        # 直近のユーザ発言 (2 件) を検索クエリにして、関連するファイル内容を渡す
        query = "\n".join(m["content"] for m in [m for m in msgs if m["role"] == "user"][-2:])
        ctx = filesvc.build_file_context(files, query)
        if ctx:
            system = f"{system}\n\n{ctx}"
    return ([{"role": "system", "content": system}] if system else []) + history


def _stream_reply(user_id: str, conv: dict, msgs: list[dict], extra: dict | None = None) -> StreamingResponse:
    llm_messages = _build_llm_messages(conv, msgs, store.list_files(user_id, conv["id"]))

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
    attachments = []
    for fid in body.attachment_ids:
        f = store.get_file(uid, conv_id, fid)
        if f:
            attachments.append({"id": f["id"], "name": f["name"]})
    user_msg = store.add_message(uid, conv_id, "user", body.content, extra_fields, existing=msgs,
                                 attachments=attachments)
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
    return HTMLResponse(INDEX_HTML)
