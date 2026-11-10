import json
import os

os.environ["SESSION_SECRET"] = "test"

import pytest
from fastapi.testclient import TestClient
from app import llm, main, store
from tests.fake_firestore import FakeFirestore


@pytest.fixture()
def client(monkeypatch):
    fs = FakeFirestore()
    store.set_client(fs)

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
        c.fs = fs
        yield c
    store.set_client(None)


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


def test_duplicate_signup_rejected(client):
    signup(client, "alice")
    client.post("/api/auth/logout")
    r = client.post("/api/auth/signup", json={"username": "alice", "password": "other123"})
    assert r.status_code == 409


def test_pin_order_and_delete_removes_messages(client):
    signup(client)
    a = client.post("/api/conversations", json={"title": "A"}).json()
    b = client.post("/api/conversations", json={"title": "B"}).json()
    client.post(f"/api/conversations/{a['id']}/messages", json={"content": "hi"})
    titles = [c["title"] for c in client.get("/api/conversations").json()["conversations"]]
    assert titles == ["A", "B"]  # 更新日時の新しい順
    client.patch(f"/api/conversations/{b['id']}", json={"pinned": True})
    titles = [c["title"] for c in client.get("/api/conversations").json()["conversations"]]
    assert titles == ["B", "A"]  # ピン留めが先頭

    client.delete(f"/api/conversations/{a['id']}")
    assert not any(a["id"] in p for p in client.fs.docs)  # メッセージのサブコレクションも削除


def test_iap_mode(client, monkeypatch):
    from dataclasses import replace
    from app import auth
    monkeypatch.setattr(auth, "settings", replace(auth.settings, auth_mode="iap"))
    h = {"X-Goog-Authenticated-User-Email": "accounts.google.com:user@example.com"}
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers=h).json()["username"] == "user@example.com"
    client.post("/api/conversations", json={"title": "x"}, headers=h)
    assert len(client.get("/api/conversations", headers=h).json()["conversations"]) == 1
