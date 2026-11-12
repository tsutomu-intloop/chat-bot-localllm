"""Firestore による会話履歴ストア.

データ構造:
  users/{user_id}                                   username, password_hash, created_at
  users/{user_id}/conversations/{conv_id}           title, model, system_prompt, pinned,
                                                    search_text, created_at, updated_at
  users/{user_id}/conversations/{conv_id}/messages/{msg_id}
                                                    seq, role, content, created_at

user_id はユーザ名の SHA-256 から決定的に生成し、create() でユーザ名の一意性を保証する.
会話はユーザのサブコレクションに置くため、他ユーザのデータには構造的にアクセスできない.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from functools import lru_cache

from .config import settings

# 検索用に会話へ保持する本文の上限 (Firestore のドキュメント上限 1MiB 対策)
SEARCH_TEXT_LIMIT = 200_000
BATCH_LIMIT = 400


class AlreadyExists(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


@lru_cache(maxsize=1)
def _default_client():
    from google.cloud import firestore

    return firestore.Client(project=settings.firestore_project or None,
                            database=settings.firestore_database or "(default)")


_client_override = None


def set_client(client) -> None:
    """テスト用にクライアントを差し替える."""
    global _client_override
    _client_override = client


def client():
    return _client_override if _client_override is not None else _default_client()


def _is_already_exists(exc: Exception) -> bool:
    try:
        from google.api_core.exceptions import AlreadyExists as GAlreadyExists, Conflict
        if isinstance(exc, (GAlreadyExists, Conflict)):
            return True
    except ImportError:  # pragma: no cover
        pass
    return isinstance(exc, AlreadyExists)


# ---------- users ----------
def user_id_for(username: str) -> str:
    return hashlib.sha256(username.encode()).hexdigest()[:40]


def _users():
    return client().collection("users")


def _user_dict(snap) -> dict | None:
    if not snap.exists:
        return None
    return {"id": snap.id, **snap.to_dict()}


def get_user(user_id: str) -> dict | None:
    return _user_dict(_users().document(user_id).get())


def get_user_by_username(username: str) -> dict | None:
    return get_user(user_id_for(username))


def create_user(username: str, password_hash: str | None) -> dict:
    uid = user_id_for(username)
    data = {"username": username, "password_hash": password_hash, "created_at": utcnow()}
    try:
        _users().document(uid).create(data)
    except Exception as e:
        if _is_already_exists(e):
            raise AlreadyExists(username) from e
        raise
    return {"id": uid, **data}


def get_or_create_user(username: str) -> dict:
    user = get_user_by_username(username)
    if user:
        return user
    try:
        return create_user(username, None)
    except AlreadyExists:
        return get_user_by_username(username)


# ---------- conversations ----------
def _convs(user_id: str):
    return _users().document(user_id).collection("conversations")


def _conv_dict(snap) -> dict | None:
    if not snap.exists:
        return None
    d = snap.to_dict()
    return {
        "id": snap.id,
        "title": d.get("title", "新しい会話"),
        "model": d.get("model"),
        "system_prompt": d.get("system_prompt"),
        "pinned": bool(d.get("pinned", False)),
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
        "search_text": d.get("search_text", ""),
    }


def list_conversations(user_id: str, q: str | None = None) -> list[dict]:
    convs = [c for c in (_conv_dict(s) for s in _convs(user_id).stream()) if c]
    if q:
        ql = q.lower()
        convs = [c for c in convs if ql in c["title"].lower() or ql in c["search_text"]]
    convs.sort(key=lambda c: c["updated_at"], reverse=True)
    convs.sort(key=lambda c: c["pinned"], reverse=True)  # 安定ソート: ピン留めを先頭に
    return convs


def get_conversation(user_id: str, conv_id: str) -> dict | None:
    return _conv_dict(_convs(user_id).document(conv_id).get())


def create_conversation(user_id: str, title: str = "新しい会話", model: str | None = None,
                        system_prompt: str | None = None) -> dict:
    now = utcnow()
    data = {"title": title, "model": model, "system_prompt": system_prompt, "pinned": False,
            "search_text": "", "created_at": now, "updated_at": now}
    ref = _convs(user_id).document(new_id())
    ref.set(data)
    return {"id": ref.id, **data}


def update_conversation(user_id: str, conv_id: str, fields: dict) -> None:
    if fields:
        _convs(user_id).document(conv_id).update(fields)


def delete_conversation(user_id: str, conv_id: str) -> None:
    ref = _convs(user_id).document(conv_id)
    _delete_messages(ref, [s.reference for s in ref.collection("messages").stream()])
    _delete_messages(ref, [s.reference for s in ref.collection("files").stream()])
    ref.delete()


# ---------- messages ----------
def _msg_dict(snap) -> dict:
    d = snap.to_dict()
    return {"id": snap.id, "seq": d["seq"], "role": d["role"], "content": d["content"],
            "attachments": d.get("attachments") or [], "created_at": d.get("created_at")}


def list_messages(user_id: str, conv_id: str) -> list[dict]:
    msgs_ref = _convs(user_id).document(conv_id).collection("messages")
    return [_msg_dict(s) for s in msgs_ref.order_by("seq").stream()]


def _delete_messages(conv_ref, refs: list) -> None:
    for i in range(0, len(refs), BATCH_LIMIT):
        batch = client().batch()
        for r in refs[i:i + BATCH_LIMIT]:
            batch.delete(r)
        batch.commit()


def _search_text(messages: list[dict]) -> str:
    return "\n".join(m["content"] for m in messages).lower()[:SEARCH_TEXT_LIMIT]


def add_message(user_id: str, conv_id: str, role: str, content: str,
                extra_conv_fields: dict | None = None, existing: list[dict] | None = None,
                attachments: list[dict] | None = None) -> dict:
    """メッセージを追加し、会話の updated_at / 検索テキストを更新する.

    existing: 呼び出し側が取得済みの既存メッセージ (読み取り回数削減用).
    """
    conv_ref = _convs(user_id).document(conv_id)
    msgs = existing if existing is not None else list_messages(user_id, conv_id)
    seq = (msgs[-1]["seq"] + 1) if msgs else 0
    now = utcnow()
    msg = {"seq": seq, "role": role, "content": content, "created_at": now}
    if attachments:
        msg["attachments"] = attachments  # [{id, name}] 表示用
    msg_ref = conv_ref.collection("messages").document(new_id())
    batch = client().batch()
    batch.set(msg_ref, msg)
    batch.update(conv_ref, {"updated_at": now,
                            "search_text": _search_text(msgs + [msg]),
                            **(extra_conv_fields or {})})
    batch.commit()
    return {"id": msg_ref.id, **msg}


def truncate_messages(user_id: str, conv_id: str, from_seq: int) -> None:
    """seq >= from_seq のメッセージを削除する (編集・再生成用)."""
    conv_ref = _convs(user_id).document(conv_id)
    msgs_ref = conv_ref.collection("messages")
    msgs = list_messages(user_id, conv_id)
    _delete_messages(conv_ref, [msgs_ref.document(m["id"]) for m in msgs if m["seq"] >= from_seq])
    conv_ref.update({"search_text": _search_text([m for m in msgs if m["seq"] < from_seq])})


def get_message(user_id: str, conv_id: str, msg_id: str) -> dict | None:
    snap = _convs(user_id).document(conv_id).collection("messages").document(msg_id).get()
    return _msg_dict(snap) if snap.exists else None


def duplicate_conversation(user_id: str, conv_id: str, upto_message_id: str | None) -> dict:
    src = get_conversation(user_id, conv_id)
    msgs = list_messages(user_id, conv_id)
    if upto_message_id:
        idx = next((i for i, m in enumerate(msgs) if m["id"] == upto_message_id), len(msgs) - 1)
        msgs = msgs[:idx + 1]
    now = utcnow()
    conv_ref = _convs(user_id).document(new_id())
    data = {"title": f"{src['title']} (コピー)"[:255], "model": src["model"],
            "system_prompt": src["system_prompt"], "pinned": False,
            "search_text": _search_text(msgs), "created_at": now, "updated_at": now}
    conv_ref.set(data)
    for i in range(0, len(msgs), BATCH_LIMIT):
        batch = client().batch()
        for j, m in enumerate(msgs[i:i + BATCH_LIMIT], start=i):
            batch.set(conv_ref.collection("messages").document(new_id()),
                      {"seq": j, "role": m["role"], "content": m["content"], "created_at": m["created_at"],
                       **({"attachments": m["attachments"]} if m.get("attachments") else {})})
        batch.commit()
    return {"id": conv_ref.id, **data}


# ---------- files (会話に添付されたファイルのメタ情報) ----------
def _files(user_id: str, conv_id: str):
    return _convs(user_id).document(conv_id).collection("files")


def _file_dict(snap) -> dict | None:
    if not snap.exists:
        return None
    return {"id": snap.id, **snap.to_dict()}


def add_file(user_id: str, conv_id: str, file_id: str, meta: dict) -> dict:
    data = {**meta, "created_at": utcnow()}
    _files(user_id, conv_id).document(file_id).set(data)
    return {"id": file_id, **data}


def list_files(user_id: str, conv_id: str) -> list[dict]:
    files = [f for f in (_file_dict(s) for s in _files(user_id, conv_id).stream()) if f]
    files.sort(key=lambda f: f["created_at"])
    return files


def get_file(user_id: str, conv_id: str, file_id: str) -> dict | None:
    return _file_dict(_files(user_id, conv_id).document(file_id).get())


def delete_file(user_id: str, conv_id: str, file_id: str) -> None:
    _files(user_id, conv_id).document(file_id).delete()
