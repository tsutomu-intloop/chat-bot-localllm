import io

import pytest

from app import files as filesvc
from tests.test_app import client, events, signup  # noqa: F401 (fixture)


def _minimal_pdf(text: str) -> bytes:
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, o in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + o + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


def _docx() -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph("社内規程: 有給休暇は入社6か月後に10日付与されます。")
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text = "項目"
    t.rows[0].cells[1].text = "値"
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _xlsx() -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "売上"
    ws.append(["月", "売上"])
    ws.append(["4月", 1200])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _pptx() -> bytes:
    from pptx import Presentation

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "四半期計画"
    slide.placeholders[1].text = "新製品をリリースする"
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


@pytest.mark.parametrize("name, data, expected", [
    ("a.txt", "こんにちは".encode("utf-8"), "こんにちは"),
    ("sjis.csv", "名前,値\n山田,1".encode("cp932"), "山田"),
    ("a.pdf", _minimal_pdf("Hello PDF World"), "Hello PDF World"),
    ("a.docx", _docx(), "有給休暇"),
    ("a.xlsx", _xlsx(), "4月,1200"),
    ("a.pptx", _pptx(), "新製品をリリースする"),
])
def test_extract_text(name, data, expected):
    assert expected in filesvc.extract_text(name, data)


def test_extract_unsupported():
    with pytest.raises(filesvc.UnsupportedFile):
        filesvc.extract_text("image.png", b"\x89PNG")


def test_chunk_and_rank_japanese():
    paras = [f"段落{i}: これは関係のない一般的な説明文です。" * 20 for i in range(10)]
    paras.insert(6, "経費精算の締め日は毎月25日です。申請はワークフローから行います。")
    chunks = filesvc.chunk_text("\n\n".join(paras), size=500, overlap=50)
    assert len(chunks) > 3
    best = filesvc.bm25_rank("経費精算の締め日はいつですか", chunks)[0][0]
    assert "締め日は毎月25日" in chunks[best]


def _upload(client, conv_id, name, data):
    return client.post(f"/api/conversations/{conv_id}/files", files=[("files", (name, data, "application/octet-stream"))])


def test_upload_and_answer_with_file(client):
    signup(client)
    conv = client.post("/api/conversations", json={}).json()
    original = _docx()
    r = _upload(client, conv["id"], "規程.docx", original)
    assert r.status_code == 200, r.text
    f = r.json()["files"][0]
    assert f["name"] == "規程.docx"

    r = client.post(f"/api/conversations/{conv['id']}/messages",
                    json={"content": "有給は何日？", "attachment_ids": [f["id"]]})
    assert events(r)[-1]["type"] == "done"
    system = client.seen[-1][0]["content"]
    assert "規程.docx" in system and "入社6か月後に10日付与" in system

    detail = client.get(f"/api/conversations/{conv['id']}").json()
    assert detail["messages"][0]["attachments"] == [{"id": f["id"], "name": "規程.docx"}]
    assert [x["name"] for x in detail["files"]] == ["規程.docx"]

    # ダウンロードは元ファイル
    dl = client.get(f"/api/conversations/{conv['id']}/files/{f['id']}")
    assert dl.status_code == 200 and dl.content == original
    assert "filename*=UTF-8''" in dl.headers["content-disposition"]

    # 複製先でもファイルが参照される
    dup = client.post(f"/api/conversations/{conv['id']}/duplicate", json={}).json()
    client.post(f"/api/conversations/{dup['id']}/messages", json={"content": "続き"})
    assert "入社6か月後に10日付与" in client.seen[-1][0]["content"]

    # 削除すると以降は参照されない
    assert client.delete(f"/api/conversations/{conv['id']}/files/{f['id']}").status_code == 200
    client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "もう一度"})
    assert "入社6か月後" not in client.seen[-1][0]["content"]


def test_large_file_uses_relevant_excerpts(client, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(filesvc, "settings", replace(filesvc.settings, file_context_chars=1500, file_chunk_chars=500))
    signup(client)
    conv = client.post("/api/conversations", json={}).json()
    filler = "\n\n".join(f"第{i}章 これは関係のない一般的な説明文です。" * 15 for i in range(30))
    text = filler + "\n\n経費精算の締め日は毎月25日です。\n\n" + filler
    _upload(client, conv["id"], "manual.txt", text.encode())
    client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "経費精算の締め日は？"})
    system = client.seen[-1][0]["content"]
    assert "締め日は毎月25日" in system
    assert "抜粋" in system
    assert len(system) < 3000


def test_upload_rejections_and_isolation(client):
    signup(client, "alice")
    conv = client.post("/api/conversations", json={}).json()
    r = _upload(client, conv["id"], "photo.png", b"\x89PNG")
    assert r.status_code == 400 and "未対応" in r.json()["detail"]

    ok = _upload(client, conv["id"], "memo.txt", "秘密".encode()).json()["files"][0]
    client.post("/api/auth/logout")
    signup(client, "bob")
    assert _upload(client, conv["id"], "x.txt", b"x").status_code == 404
    assert client.get(f"/api/conversations/{conv['id']}/files/{ok['id']}").status_code == 404


def test_delete_conversation_removes_files(client, tmp_path):
    signup(client)
    conv = client.post("/api/conversations", json={}).json()
    _upload(client, conv["id"], "memo.txt", "abc".encode())
    stored = list((tmp_path / "uploads").rglob("*.txt"))
    assert stored
    client.delete(f"/api/conversations/{conv['id']}")
    assert not list((tmp_path / "uploads").rglob("*.*"))
    assert not any("files" in p for p in client.fs.docs)
