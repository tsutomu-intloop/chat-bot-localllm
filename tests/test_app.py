import json
import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SESSION_SECRET"] = "test"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlalchemy import create_engine

from app import db as dbmod
from app import llm, main


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    dbmod.SessionLocal.configure(bind=engine)
    monkeypatch.setattr(dbmod, "engine", engine)
    dbmod.Base.metadata.create_all(engine)

    seen = []

    async def fake_stream(messages, model=None):
        seen.append(messages)
        for t in ["こん", "にちは"]:
            yield t

    async def fake_models():
        return ["m1"]

    monkeypatch.setattr(llm, "stream_chat", fake_stream)
    monkeypatch.setattr(llm, "list_models", fake_models)
    with TestClient(main.app) as c:
        c.seen = seen
        yield c


def events(resp):
    return [json.loads(l[5:]) for l in resp.text.splitlines() if l.startswith("data:")]


def signup(c, name="alice"):
    r = c.post("/api/auth/signup", json={"username": name, "password": "pass1234"})
    assert r.status_code == 200, r.text


def test_requires_login(client):
    assert client.get("/api/conversations").status_code == 401


def test_chat_history_flow(client):
    signup(client)
    conv = client.post("/api/conversations", json={}).json()
    r = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "はじめまして\n2行目"})
    ev = events(r)
    assert ev[0]["type"] == "meta" and ev[0]["title"] == "はじめまして"
    assert "".join(e["content"] for e in ev if e["type"] == "delta") == "こんにちは"
    assert ev[-1]["type"] == "done"

    detail = client.get(f"/api/conversations/{conv['id']}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["content"] == "こんにちは"

    # 2回目: 履歴が LLM に渡る
    client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "次"})
    sent = client.seen[-1]
    assert sent[0]["role"] == "system"
    assert [m["content"] for m in sent[1:]] == ["はじめまして\n2行目", "こんにちは", "次"]

    # 検索
    assert len(client.get("/api/conversations", params={"q": "こんにち"}).json()["conversations"]) == 1
    assert len(client.get("/api/conversations", params={"q": "存在しない"}).json()["conversations"]) == 0

    # 分岐 (最初の応答まで)
    first_assistant = detail["messages"][1]["id"]
    dup = client.post(f"/api/conversations/{conv['id']}/duplicate",
                      json={"upto_message_id": first_assistant}).json()
    assert len(client.get(f"/api/conversations/{dup['id']}").json()["messages"]) == 2

    # 編集して再送: 最初のユーザメッセージ以降を置き換え
    first_user = detail["messages"][0]["id"]
    client.post(f"/api/conversations/{conv['id']}/messages",
                json={"content": "やり直し", "edit_message_id": first_user})
    msgs = client.get(f"/api/conversations/{conv['id']}").json()["messages"]
    assert [m["content"] for m in msgs] == ["やり直し", "こんにちは"]

    # 再生成
    client.post(f"/api/conversations/{conv['id']}/regenerate")
    assert len(client.get(f"/api/conversations/{conv['id']}").json()["messages"]) == 2

    # エクスポート
    md = client.get(f"/api/conversations/{conv['id']}/export?format=md").text
    assert "やり直し" in md


def test_users_are_isolated(client):
    signup(client, "alice")
    conv = client.post("/api/conversations", json={"title": "secret"}).json()
    client.post("/api/auth/logout")
    signup(client, "bob")
    assert client.get("/api/conversations").json()["conversations"] == []
    assert client.get(f"/api/conversations/{conv['id']}").status_code == 404
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"username": "alice", "password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "alice", "password": "pass1234"}).status_code == 200
    assert len(client.get("/api/conversations").json()["conversations"]) == 1
