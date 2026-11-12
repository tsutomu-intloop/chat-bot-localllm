import io

import docx
import openpyxl
import pytest
from pptx import Presentation

from app import export as ex
from app import llm, main
from tests.test_app import client, events, signup  # noqa: F401 (fixture)

MD = """# 2026年度 営業計画

## 概要
今期は**新規顧客の獲得**に注力します。

- 重点施策
  - 展示会への出展
1. 4月: 準備

## 売上目標
| 月 | 売上 | 達成率 |
|---|---|---|
| 4月 | 1,200 | 85% |

```csv:顧客リスト.csv
会社名,担当
A社,山田
```
"""


def test_docx_structure():
    d = docx.Document(io.BytesIO(ex.to_docx(MD)))
    styles = [(p.style.name, p.text) for p in d.paragraphs if p.text]
    assert ("Heading 1", "2026年度 営業計画") in styles
    assert ("List Bullet 2", "展示会への出展") in styles
    assert ("List Number", "4月: 準備") in styles
    assert d.tables[0].cell(1, 1).text == "1,200"


def test_xlsx_tables_become_sheets_with_numbers():
    wb = openpyxl.load_workbook(io.BytesIO(ex.to_xlsx(MD)))
    assert wb.sheetnames == ["売上目標", "顧客リスト"]
    ws = wb["売上目標"]
    assert ws["B2"].value == 1200 and ws["C2"].value == 0.85 and ws["C2"].number_format == "0.0%"
    assert wb["顧客リスト"]["B2"].value == "山田"


def test_xlsx_without_tables_writes_text():
    wb = openpyxl.load_workbook(io.BytesIO(ex.to_xlsx("# メモ\n本文です")))
    assert [c.value for c in wb.active["A"]] == ["メモ", "本文です"]


def test_pptx_slides_per_heading():
    prs = Presentation(io.BytesIO(ex.to_pptx(MD, "x")))
    titles = [s.shapes.title.text for s in prs.slides]
    assert titles[0] == "2026年度 営業計画"  # 表紙
    assert "概要" in titles and "売上目標" in titles
    assert any(sh.has_table for s in prs.slides for sh in s.shapes)


def test_pptx_splits_long_slides():
    md = "## 長いスライド\n" + "\n".join(f"- 項目{i}" for i in range(30))
    prs = Presentation(io.BytesIO(ex.to_pptx(md, "x")))
    assert len(prs.slides) == 3


@pytest.mark.parametrize("fmt", list(ex.FORMATS))
def test_export_endpoint(client, monkeypatch, fmt):
    async def fake_stream(messages, model=None):
        yield MD

    monkeypatch.setattr(llm, "stream_chat", fake_stream)
    signup(client)
    conv = client.post("/api/conversations", json={}).json()
    done = events(client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "営業計画を Excel で"}))[-1]
    r = client.get(f"/api/conversations/{conv['id']}/messages/{done['message_id']}/export", params={"format": fmt})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(ex.FORMATS[fmt][0].split(";")[0])
    assert ex.FORMATS[fmt][1] in r.headers["content-disposition"]
    assert len(r.content) > 0


def test_export_rejects_unknown_format_and_other_users(client):
    signup(client, "alice")
    conv = client.post("/api/conversations", json={}).json()
    done = events(client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "hi"}))[-1]
    url = f"/api/conversations/{conv['id']}/messages/{done['message_id']}/export"
    assert client.get(url, params={"format": "exe"}).status_code == 400
    client.post("/api/auth/logout")
    signup(client, "bob")
    assert client.get(url, params={"format": "docx"}).status_code == 404


def test_system_prompt_tells_llm_files_can_be_saved(client):
    signup(client)
    conv = client.post("/api/conversations", json={}).json()
    client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "hi"})
    assert main.FILE_OUTPUT_GUIDE in client.seen[-1][0]["content"]
