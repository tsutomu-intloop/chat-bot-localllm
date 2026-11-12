"""アップロードファイルの保存・テキスト抽出・検索.

- 元ファイルと抽出テキストは Cloud Storage (FILES_BUCKET 指定時) またはローカルディスクに保存する.
- 回答時は、会話に添付されたファイルのテキストが予算内なら全文を、超える場合は
  質問に関連するチャンクだけを BM25 (日本語は文字 2-gram) で選んで LLM に渡す.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

from .config import settings


class UnsupportedFile(Exception):
    pass


# ---------- 保存先 ----------
class LocalStorage:
    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("invalid key")
        return p

    def put(self, key: str, data: bytes, content_type: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete_prefix(self, prefix: str) -> None:
        base = self._path(prefix)
        if base.is_dir():
            for f in sorted(base.rglob("*"), reverse=True):
                f.unlink() if f.is_file() else f.rmdir()
            base.rmdir()

    def copy(self, src: str, dst: str) -> None:
        self.put(dst, self.get(src), "")


class GCSStorage:
    def __init__(self, bucket: str) -> None:
        from google.cloud import storage

        self.bucket = storage.Client(project=settings.firestore_project or None).bucket(bucket)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.bucket.blob(key).upload_from_string(data, content_type=content_type or "application/octet-stream")

    def get(self, key: str) -> bytes:
        return self.bucket.blob(key).download_as_bytes()

    def delete_prefix(self, prefix: str) -> None:
        blobs = list(self.bucket.list_blobs(prefix=prefix.rstrip("/") + "/"))
        if blobs:
            self.bucket.delete_blobs(blobs)

    def copy(self, src: str, dst: str) -> None:
        self.bucket.copy_blob(self.bucket.blob(src), self.bucket, dst)


_storage_override = None


def set_storage(s) -> None:
    """テスト用に保存先を差し替える."""
    global _storage_override
    _storage_override = s
    _cached_text.cache_clear()


@lru_cache(maxsize=1)
def _default_storage():
    if settings.files_bucket:
        return GCSStorage(settings.files_bucket)
    return LocalStorage(settings.files_local_dir)


def storage():
    return _storage_override if _storage_override is not None else _default_storage()


def file_prefix(user_id: str, conv_id: str, file_id: str) -> str:
    return f"users/{user_id}/conversations/{conv_id}/files/{file_id}"


# ---------- テキスト抽出 ----------
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml", ".html", ".htm",
    ".log", ".ini", ".toml", ".cfg", ".conf", ".sql", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go",
    ".rb", ".php", ".c", ".h", ".cpp", ".hpp", ".cs", ".rs", ".kt", ".swift", ".sh", ".bat", ".ps1", ".css",
    ".scss", ".vue", ".tf", ".dockerfile", ".env.example", ".r", ".scala", ".pl", ".lua",
}
OFFICE_EXTS = {".pdf", ".docx", ".xlsx", ".xlsm", ".pptx"}
SUPPORTED_EXTS = TEXT_EXTS | OFFICE_EXTS


def _ext(name: str) -> str:
    lower = name.lower()
    if lower == "dockerfile":
        return ".dockerfile"
    return Path(lower).suffix


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp932", "euc-jp"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(f"[ページ {i}]\n{text}")
    return "\n\n".join(pages)


def _docx(data: bytes) -> str:
    import docx

    d = docx.Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for t in d.tables:
        for row in t.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(parts)


def _xlsx(data: bytes) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"[シート {ws.title}]")
        buf = io.StringIO()
        w = csv.writer(buf)
        for row in ws.iter_rows(values_only=True):
            if any(v is not None and str(v).strip() for v in row):
                w.writerow(["" if v is None else v for v in row])
        out.append(buf.getvalue().strip())
    return "\n".join(out)


def _pptx(data: bytes) -> str:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    out = []
    for i, slide in enumerate(prs.slides, start=1):
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t:
                    texts.append(t)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    texts.append(" | ".join(c.text.strip() for c in row.cells))
        if slide.has_notes_slide:
            note = slide.notes_slide.notes_text_frame.text.strip()
            if note:
                texts.append(f"(ノート) {note}")
        if texts:
            out.append(f"[スライド {i}]\n" + "\n".join(texts))
    return "\n\n".join(out)


def extract_text(name: str, data: bytes) -> str:
    ext = _ext(name)
    try:
        if ext == ".pdf":
            text = _pdf(data)
        elif ext == ".docx":
            text = _docx(data)
        elif ext in (".xlsx", ".xlsm"):
            text = _xlsx(data)
        elif ext == ".pptx":
            text = _pptx(data)
        elif ext in TEXT_EXTS:
            text = _decode(data)
            if ext in (".json",):
                try:  # 整形して読みやすく
                    text = json.dumps(json.loads(text), ensure_ascii=False, indent=1)
                except ValueError:
                    pass
        else:
            raise UnsupportedFile(f"未対応のファイル形式です: {ext or name}")
    except UnsupportedFile:
        raise
    except Exception as e:  # noqa: BLE001
        raise UnsupportedFile(f"ファイルを読み取れませんでした ({name}): {e}") from e
    text = re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n")).strip()
    if not text:
        raise UnsupportedFile(f"テキストを抽出できませんでした ({name})。画像のみの PDF などは未対応です")
    return text


# ---------- チャンク分割と検索 ----------
def chunk_text(text: str, size: int | None = None, overlap: int = 200) -> list[str]:
    size = size or settings.file_chunk_chars
    if len(text) <= size:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # 段落・改行・句点の切れ目で区切る
            window = text[start:end]
            cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind("。"))
            if cut > size * 0.5:
                end = start + cut + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


_WORD = re.compile(r"[A-Za-z0-9_]+")
_CJK = re.compile(r"[぀-ヿ㐀-鿿豈-﫿ｦ-ﾟ]+")


def tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens = _WORD.findall(text)
    for run in _CJK.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def bm25_rank(query: str, docs: list[str], k1: float = 1.5, b: float = 0.75) -> list[tuple[int, float]]:
    q = set(tokenize(query))
    if not q or not docs:
        return [(i, 0.0) for i in range(len(docs))]
    toks = [tokenize(d) for d in docs]
    avg = sum(len(t) for t in toks) / len(toks) or 1
    df = Counter(t for ts in toks for t in set(ts))
    n = len(docs)
    scores = []
    for i, ts in enumerate(toks):
        tf = Counter(ts)
        s = 0.0
        for term in q:
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            s += idf * tf[term] * (k1 + 1) / (tf[term] + k1 * (1 - b + b * len(ts) / avg))
        scores.append((i, s))
    return sorted(scores, key=lambda x: x[1], reverse=True)


@lru_cache(maxsize=64)
def _cached_text(key: str) -> str:
    return storage().get(key).decode("utf-8")


def load_text(file_meta: dict) -> str:
    return _cached_text(file_meta["text_key"])


def build_file_context(files: list[dict], query: str) -> str | None:
    """LLM に渡すファイルコンテキストを組み立てる."""
    if not files:
        return None
    budget = settings.file_context_chars
    texts = [(f, load_text(f)) for f in files]
    total = sum(len(t) for _, t in texts)

    sections: list[str] = []
    if total <= budget:
        for f, t in texts:
            sections.append(f"### ファイル: {f['name']}\n{t}")
        mode = "全文"
    else:
        # 関連するチャンクを予算内で選ぶ
        chunks: list[tuple[dict, int, str]] = []
        for f, t in texts:
            for i, c in enumerate(chunk_text(t)):
                chunks.append((f, i, c))
        ranked = bm25_rank(query, [c for _, _, c in chunks])
        picked, used = [], 0
        for idx, score in ranked:
            c = chunks[idx][2]
            if used + len(c) > budget:
                continue
            picked.append(idx)
            used += len(c)
        # ファイル内の順序で並べ直す
        picked.sort()
        for idx in picked:
            f, i, c = chunks[idx]
            sections.append(f"### ファイル: {f['name']} (抜粋 {i + 1})\n{c}")
        mode = "質問に関連する抜粋"

    names = "、".join(f["name"] for f in files)
    return (
        f"ユーザは次のファイルをアップロードしています: {names}\n"
        f"以下はその内容（{mode}）です。質問に答える際はこの内容を根拠にし、"
        "どのファイルに基づくかを示してください。内容に書かれていないことは推測であると明示してください。\n\n"
        + "\n\n".join(sections)
    )
