"""LLM の回答 (Markdown) を各種ファイル形式に変換する.

LLM には普段どおり Markdown (見出し・箇条書き・表・コードブロック) で書かせ、
ここで Word / Excel / PowerPoint / HTML などに変換する. 内容の種類は固定しない.
"""
from __future__ import annotations

import csv
import html as htmllib
import io
import re
from dataclasses import dataclass, field

from markdown_it import MarkdownIt

FORMATS = {
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "html": ("text/html; charset=utf-8", ".html"),
    "md": ("text/markdown; charset=utf-8", ".md"),
    "txt": ("text/plain; charset=utf-8", ".txt"),
}

_md = MarkdownIt("commonmark", {"breaks": True, "html": False}).enable(["table", "strikethrough"])


# ---------- Markdown → ブロック列 ----------
@dataclass
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False


@dataclass
class Block:
    type: str  # heading / paragraph / list_item / table / code / hr
    runs: list[Run] = field(default_factory=list)
    level: int = 0  # 見出しレベル / リストの深さ
    ordered: bool = False
    number: int = 0
    quote: bool = False
    rows: list[list[str]] = field(default_factory=list)  # table: 先頭行がヘッダ
    lang: str = ""
    text: str = ""

    @property
    def plain(self) -> str:
        return "".join(r.text for r in self.runs)


def _inline_runs(token) -> list[Run]:
    runs: list[Run] = []
    bold = italic = False
    for c in token.children or []:
        t = c.type
        if t == "strong_open":
            bold = True
        elif t == "strong_close":
            bold = False
        elif t == "em_open":
            italic = True
        elif t == "em_close":
            italic = False
        elif t == "code_inline":
            runs.append(Run(c.content, bold, italic, True))
        elif t in ("softbreak", "hardbreak"):
            runs.append(Run("\n", bold, italic))
        elif t in ("text", "html_inline"):
            if c.content:
                runs.append(Run(c.content, bold, italic))
        elif t == "image":
            runs.append(Run(c.content or "", bold, italic))
    return runs


def parse_blocks(markdown: str) -> list[Block]:
    tokens = _md.parse(markdown)
    blocks: list[Block] = []
    lists: list[dict] = []  # ネストしたリストの状態
    quote = 0
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        t = tok.type
        if t in ("bullet_list_open", "ordered_list_open"):
            start = int(tok.attrGet("start") or 1) if t == "ordered_list_open" else 1
            lists.append({"ordered": t == "ordered_list_open", "n": start - 1})
        elif t in ("bullet_list_close", "ordered_list_close"):
            lists.pop()
        elif t == "list_item_open":
            lists[-1]["n"] += 1
            lists[-1]["first"] = True
        elif t == "blockquote_open":
            quote += 1
        elif t == "blockquote_close":
            quote -= 1
        elif t == "heading_open":
            blocks.append(Block("heading", _inline_runs(tokens[i + 1]), level=int(tok.tag[1])))
            i += 2
        elif t == "paragraph_open":
            runs = _inline_runs(tokens[i + 1])
            if lists:
                cur = lists[-1]
                first = cur.pop("first", False)
                blocks.append(Block("list_item" if first else "paragraph", runs, level=len(lists) - 1,
                                    ordered=cur["ordered"], number=cur["n"], quote=quote > 0))
                if not first:
                    blocks[-1].level = len(lists)  # リスト項目の 2 段落目以降はインデント付き段落
            else:
                blocks.append(Block("paragraph", runs, quote=quote > 0))
            i += 2
        elif t in ("fence", "code_block"):
            info = (tok.info or "").strip()
            blocks.append(Block("code", lang=info, text=tok.content.rstrip("\n")))
        elif t == "hr":
            blocks.append(Block("hr"))
        elif t == "table_open":
            rows: list[list[str]] = []
            row: list[str] = []
            while tokens[i].type != "table_close":
                tt = tokens[i].type
                if tt == "tr_open":
                    row = []
                elif tt == "tr_close":
                    rows.append(row)
                elif tt == "inline":
                    row.append("".join(r.text for r in _inline_runs(tokens[i])).replace("\n", " "))
                i += 1
            blocks.append(Block("table", rows=rows))
        i += 1
    return blocks


def title_of(markdown: str, fallback: str) -> str:
    for b in parse_blocks(markdown):
        if b.type == "heading" and b.plain.strip():
            return b.plain.strip()
    return fallback


def safe_filename(name: str, ext: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip(" ._") or "output"
    return name[:80] + ext


# ---------- Word ----------
def to_docx(markdown: str) -> bytes:
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    d = docx.Document()
    normal = d.styles["Normal"]
    normal.font.name = "Yu Gothic"
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "Yu Gothic")
    normal.font.size = Pt(10.5)

    def add_runs(p, runs):
        for r in runs:
            run = p.add_run(r.text)
            run.bold = r.bold or None
            run.italic = r.italic or None
            if r.code:
                run.font.name = "Consolas"
                run.font.color.rgb = RGBColor(0xC7, 0x25, 0x4E)

    for b in parse_blocks(markdown):
        if b.type == "heading":
            add_runs(d.add_heading(level=min(b.level, 9)), b.runs)
        elif b.type == "paragraph":
            p = d.add_paragraph()
            add_runs(p, b.runs)
            if b.level:
                p.paragraph_format.left_indent = Pt(18 * b.level)
            if b.quote:
                p.style = d.styles["Quote"]
        elif b.type == "list_item":
            style = "List Number" if b.ordered else "List Bullet"
            if b.level:
                style += f" {min(b.level + 1, 3)}"
            p = d.add_paragraph(style=style)
            add_runs(p, b.runs)
        elif b.type == "code":
            for line in b.text.split("\n") or [""]:
                p = d.add_paragraph()
                p.paragraph_format.space_after = Pt(0)
                run = p.add_run(line)
                run.font.name = "Consolas"
                run.font.size = Pt(9)
        elif b.type == "table" and b.rows:
            cols = max(len(r) for r in b.rows)
            table = d.add_table(rows=len(b.rows), cols=cols)
            table.style = "Table Grid"
            for ri, row in enumerate(b.rows):
                for ci in range(cols):
                    cell = table.cell(ri, ci)
                    cell.text = row[ci] if ci < len(row) else ""
                    if ri == 0:
                        for r in cell.paragraphs[0].runs:
                            r.bold = True
            d.add_paragraph()
        elif b.type == "hr":
            d.add_paragraph().alignment = WD_ALIGN_PARAGRAPH.CENTER
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


# ---------- Excel ----------
_NUM = re.compile(r"^[+-]?[\d,]*\.?\d+%?$")


def _cell_value(v: str):
    s = v.strip()
    if _NUM.match(s.replace("¥", "").replace("円", "")):
        n = s.replace("¥", "").replace("円", "").replace(",", "")
        try:
            if n.endswith("%"):
                return float(n[:-1]) / 100
            f = float(n)
            return int(f) if f.is_integer() and "." not in n else f
        except ValueError:
            pass
    return s


def _tables(markdown: str) -> list[tuple[str, list[list[str]]]]:
    """回答中の表 (Markdown の表 / CSV・TSV のコードブロック) を (シート名, 行) で返す."""
    out = []
    heading = ""
    for b in parse_blocks(markdown):
        if b.type == "heading":
            heading = b.plain.strip()
        elif b.type == "table" and b.rows:
            out.append((heading, b.rows))
        elif b.type == "code" and b.text.strip():
            lang = b.lang.split(":")[0].lower()
            name = b.lang.split(":", 1)[1] if ":" in b.lang else ""
            if lang in ("csv", "tsv") or name.lower().endswith((".csv", ".tsv")):
                delim = "\t" if lang == "tsv" or name.lower().endswith(".tsv") else ","
                out.append((name.rsplit(".", 1)[0] or heading, list(csv.reader(io.StringIO(b.text), delimiter=delim))))
    return out


def to_xlsx(markdown: str) -> bytes:
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    tables = _tables(markdown)
    used: set[str] = set()

    def sheet_name(base: str, n: int) -> str:
        name = re.sub(r"[\[\]:*?/\\]", "", base)[:28] or f"表{n}"
        cand, k = name, 2
        while cand in used:
            cand, k = f"{name[:25]}_{k}", k + 1
        used.add(cand)
        return cand

    thin = Side(style="thin", color="BBBBBB")
    if tables:
        for n, (title, rows) in enumerate(tables, start=1):
            ws = wb.create_sheet(sheet_name(title, n))
            for ri, r in enumerate(rows, start=1):
                for ci, v in enumerate(r, start=1):
                    c = ws.cell(ri, ci, _cell_value(v))
                    c.border = Border(top=thin, bottom=thin, left=thin, right=thin)
                    c.alignment = Alignment(vertical="top", wrap_text=True)
                    if isinstance(c.value, float) and v.strip().endswith("%"):
                        c.number_format = "0.0%"
            for c in ws[1]:
                c.font = Font(bold=True)
                c.fill = PatternFill("solid", fgColor="E8F0FE")
            for ci in range(1, ws.max_column + 1):
                width = max((len(str(ws.cell(r, ci).value or "")) for r in range(1, ws.max_row + 1)), default=8)
                ws.column_dimensions[get_column_letter(ci)].width = min(max(8, width * 1.8 + 2), 60)
            ws.freeze_panes = "A2"
    else:
        # 表がない場合は本文を 1 行ずつ A 列に書く
        ws = wb.create_sheet("内容")
        for b in parse_blocks(markdown):
            text = b.text if b.type == "code" else b.plain
            for line in (text or "").split("\n"):
                ws.append([("  " * b.level) + line if b.type == "list_item" else line])
                if b.type == "heading":
                    ws.cell(ws.max_row, 1).font = Font(bold=True, size=13)
        ws.column_dimensions["A"].width = 100
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------- PowerPoint ----------
MAX_LINES_PER_SLIDE = 12


def to_pptx(markdown: str, fallback_title: str = "") -> bytes:
    from pptx import Presentation
    from pptx.util import Pt

    blocks = parse_blocks(markdown)
    prs = Presentation()
    title_layout, content_layout, title_only = prs.slide_layouts[0], prs.slide_layouts[1], prs.slide_layouts[5]

    # スライドに分割: 最上位の見出しが 1 つだけならタイトルスライド、その下の見出しごとに 1 枚
    levels = sorted({b.level for b in blocks if b.type == "heading"})
    top = levels[0] if levels else 1
    top_count = sum(1 for b in blocks if b.type == "heading" and b.level == top)
    split_level = levels[1] if top_count == 1 and len(levels) > 1 else top

    slides: list[dict] = []
    cover = None
    cur = None
    for b in blocks:
        if b.type == "heading" and b.level <= split_level:
            if b.level < split_level and cover is None and not slides:
                cover = {"title": b.plain, "sub": []}
                continue
            cur = {"title": b.plain, "items": []}
            slides.append(cur)
            continue
        if cur is None:
            if cover is not None:
                if b.type in ("paragraph", "list_item"):
                    cover["sub"].append(b.plain)
                continue
            cur = {"title": fallback_title, "items": []}
            slides.append(cur)
        cur["items"].append(b)

    def add_title_slide(title: str, sub: str):
        s = prs.slides.add_slide(title_layout)
        s.shapes.title.text = title
        if len(s.placeholders) > 1:
            s.placeholders[1].text = sub

    if cover:
        add_title_slide(cover["title"], "\n".join(cover["sub"]))

    for sd in slides:
        lines: list[tuple[int, str, bool]] = []  # (level, text, bold)
        tables = []
        for b in sd["items"]:
            if b.type == "list_item":
                lines.append((b.level, b.plain, False))
            elif b.type == "paragraph":
                lines.append((b.level, b.plain, False))
            elif b.type == "heading":
                lines.append((0, b.plain, True))
            elif b.type == "code":
                lines.extend((1, ln, False) for ln in b.text.split("\n"))
            elif b.type == "table":
                tables.append(b.rows)
        chunks = [lines[k:k + MAX_LINES_PER_SLIDE] for k in range(0, len(lines), MAX_LINES_PER_SLIDE)] or ([[]] if not tables else [])
        for ci, chunk in enumerate(chunks):
            s = prs.slides.add_slide(content_layout)
            s.shapes.title.text = sd["title"] + (f" ({ci + 1})" if len(chunks) > 1 else "")
            tf = s.placeholders[1].text_frame
            tf.clear()
            for li, (lvl, text, bold) in enumerate(chunk):
                p = tf.paragraphs[0] if li == 0 else tf.add_paragraph()
                p.text = text
                p.level = min(lvl, 4)
                p.font.size = Pt(18 if lvl == 0 else 16)
                p.font.bold = bold or None
        for rows in tables:
            s = prs.slides.add_slide(title_only)
            s.shapes.title.text = sd["title"]
            cols = max(len(r) for r in rows)
            left, topm = Pt(36), Pt(110)
            width, height = prs.slide_width - Pt(72), Pt(28) * len(rows)
            tbl = s.shapes.add_table(len(rows), cols, left, topm, width, height).table
            for ri, row in enumerate(rows):
                for ci in range(cols):
                    cell = tbl.cell(ri, ci)
                    cell.text = row[ci] if ci < len(row) else ""
                    for p in cell.text_frame.paragraphs:
                        p.font.size = Pt(12)
    if not prs.slides:
        add_title_slide(fallback_title or "資料", "")
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


# ---------- HTML ----------
def to_html(markdown: str, title: str) -> bytes:
    body = _md.render(markdown)
    page = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{htmllib.escape(title)}</title>
<style>
body {{ font-family: system-ui, "Hiragino Sans", "Yu Gothic", sans-serif; max-width: 860px; margin: 2em auto; padding: 0 16px; line-height: 1.7; color: #1d2129; }}
table {{ border-collapse: collapse; }} th, td {{ border: 1px solid #ccc; padding: 4px 10px; }} th {{ background: #f0f4fa; }}
pre {{ background: #f5f5f7; padding: 12px; border-radius: 6px; overflow-x: auto; }} code {{ font-family: Consolas, monospace; }}
blockquote {{ border-left: 4px solid #ddd; margin-left: 0; padding-left: 1em; color: #555; }}
@media print {{ body {{ margin: 0; }} }}
</style></head><body>
{body}
</body></html>"""
    return page.encode("utf-8")


def convert(markdown: str, fmt: str, title: str) -> bytes:
    if fmt == "docx":
        return to_docx(markdown)
    if fmt == "xlsx":
        return to_xlsx(markdown)
    if fmt == "pptx":
        return to_pptx(markdown, title)
    if fmt == "html":
        return to_html(markdown, title)
    if fmt in ("md", "txt"):
        return markdown.encode("utf-8")
    raise ValueError(f"unsupported format: {fmt}")
