"use strict";
const $ = (s) => document.querySelector(s);
const state = { config: null, me: null, convs: [], current: null, models: [], streaming: null, signup: false,
  pending: [] /* 次のメッセージと一緒に送る添付ファイル */, uploading: 0 };

// ---------- API ----------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) { showAuth(); throw new Error("unauthorized"); }
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return res.json();
}

// SSE ストリームを読み取る
async function streamPost(path, body, onEvent, signal) {
  const res = await fetch(path, {
    method: "POST", credentials: "same-origin", signal,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) { showAuth(); return; }
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
      for (const line of chunk.split("\n")) {
        if (line.startsWith("data:")) onEvent(JSON.parse(line.slice(5)));
      }
    }
  }
}

// ---------- rendering helpers ----------
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function renderMarkdown(text) {
  if (window.marked && window.DOMPurify) return DOMPurify.sanitize(marked.parse(text, { breaks: true }));
  return `<p style="white-space:pre-wrap">${escapeHtml(text)}</p>`;
}
function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) e.append(c);
  return e;
}

// ---------- auth ----------
function showAuth() {
  $("#app-view").classList.add("hidden");
  $("#auth-view").classList.remove("hidden");
  $("#auth-toggle").classList.toggle("hidden", !state.config?.allow_signup);
}
function setAuthMode(signup) {
  state.signup = signup;
  $("#auth-submit").textContent = signup ? "アカウント作成" : "ログイン";
  $("#auth-toggle").textContent = signup ? "ログインに戻る" : "アカウントを作成する";
  $("#auth-password").autocomplete = signup ? "new-password" : "current-password";
  $("#auth-error").textContent = "";
}
$("#auth-toggle").onclick = () => setAuthMode(!state.signup);
$("#auth-form").onsubmit = async (e) => {
  e.preventDefault();
  $("#auth-error").textContent = "";
  try {
    await api(state.signup ? "/api/auth/signup" : "/api/auth/login", {
      method: "POST", body: { username: $("#auth-username").value, password: $("#auth-password").value },
    });
    $("#auth-password").value = "";
    await startApp();
  } catch (err) { if (err.message !== "unauthorized") $("#auth-error").textContent = err.message; else $("#auth-error").textContent = "ユーザ名またはパスワードが違います"; }
};
$("#logout").onclick = async () => {
  await api("/api/auth/logout", { method: "POST" });
  state.current = null; state.convs = [];
  showAuth();
};

// ---------- conversation list ----------
function groupLabel(iso) {
  const d = new Date(iso), now = new Date();
  const days = Math.floor((new Date(now.toDateString()) - new Date(d.toDateString())) / 86400000);
  if (days <= 0) return "今日";
  if (days === 1) return "昨日";
  if (days < 7) return "過去7日間";
  if (days < 30) return "過去30日間";
  return `${d.getFullYear()}年${d.getMonth() + 1}月`;
}
function renderConvList() {
  const list = $("#conv-list");
  list.innerHTML = "";
  if (!state.convs.length) {
    list.append(el("div", { class: "conv-group" }, $("#search").value ? "該当する会話はありません" : "まだ会話はありません"));
    return;
  }
  let last = null;
  for (const c of state.convs) {
    const g = c.pinned ? "📌 ピン留め" : groupLabel(c.updated_at);
    if (g !== last) { list.append(el("div", { class: "conv-group" }, g)); last = g; }
    const pin = el("button", { class: "pin" + (c.pinned ? " on" : ""), title: c.pinned ? "ピン留め解除" : "ピン留め",
      onclick: async (ev) => { ev.stopPropagation(); await api(`/api/conversations/${c.id}`, { method: "PATCH", body: { pinned: !c.pinned } }); loadConvs(); } },
      c.pinned ? "★" : "☆");
    list.append(el("div", { class: "conv-item" + (state.current?.id === c.id ? " active" : ""), title: c.title,
      onclick: () => { openConv(c.id); $("#sidebar").classList.remove("open"); } },
      el("span", { class: "t" }, c.title), pin));
  }
}
async function loadConvs() {
  const q = $("#search").value.trim();
  const data = await api("/api/conversations" + (q ? `?q=${encodeURIComponent(q)}` : ""));
  state.convs = data.conversations;
  renderConvList();
}
let searchTimer;
$("#search").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadConvs, 250); };

// ---------- messages ----------
function renderMessages() {
  const box = $("#messages");
  box.innerHTML = "";
  const msgs = state.current?.messages || [];
  if (!msgs.length) {
    box.append(el("div", { class: "empty" }, el("h1", {}, "何をお手伝いしましょうか？"),
      el("p", { class: "muted" }, "会話は自動で保存され、左の履歴からいつでも再開・複製できます。"),
      el("p", { class: "muted" }, "📎 やドラッグ＆ドロップで PDF・Word・Excel・PowerPoint・テキストを添付すると、その内容を踏まえて回答します。")));
    return;
  }
  msgs.forEach((m, idx) => box.append(messageNode(m, idx === msgs.length - 1, msgs[idx - 1])));
  box.scrollTop = box.scrollHeight;
}
// ---------- ファイルとして保存 ----------
const EXPORT_FORMATS = [
  ["docx", "Word (.docx)"], ["xlsx", "Excel (.xlsx)"], ["pptx", "PowerPoint (.pptx)"],
  ["html", "HTML（印刷して PDF にも）"], ["md", "Markdown (.md)"], ["txt", "テキスト (.txt)"],
];
const FORMAT_HINTS = [
  ["xlsx", /excel|エクセル|xlsx|スプレッドシート|表計算/i],
  ["pptx", /powerpoint|パワポ|pptx|スライド|プレゼン/i],
  ["docx", /\bword\b|ワード|docx|文書ファイル/i],
  ["html", /html|web ?ページ|pdf/i],
  ["md", /markdown|マークダウン|\.md\b/i],
];
function suggestFormat(text) {
  for (const [fmt, re] of FORMAT_HINTS) if (re.test(text || "")) return fmt;
  return null;
}
function download(url) {
  const a = el("a", { href: url, download: "" });
  document.body.append(a); a.click(); a.remove();
}
function exportUrl(m, fmt) {
  return `/api/conversations/${state.current.id}/messages/${m.id}/export?format=${fmt}`;
}
function showExportMenu(anchor, m) {
  document.querySelectorAll(".export-menu").forEach((x) => x.remove());
  const menu = el("div", { class: "menu export-menu" },
    ...EXPORT_FORMATS.map(([fmt, label]) => el("button", { onclick: () => { menu.remove(); download(exportUrl(m, fmt)); } }, label)));
  anchor.after(menu);
  // 下に十分な余白がなければ上向きに開く
  const composerTop = $("#composer").getBoundingClientRect().top;
  if (anchor.getBoundingClientRect().bottom + menu.offsetHeight + 8 > composerTop) menu.classList.add("up");
  setTimeout(() => document.addEventListener("click", () => menu.remove(), { once: true }));
}
const CODE_EXT = { python: "py", py: "py", javascript: "js", js: "js", typescript: "ts", ts: "ts", json: "json", csv: "csv",
  tsv: "tsv", html: "html", css: "css", sql: "sql", bash: "sh", sh: "sh", shell: "sh", yaml: "yaml", yml: "yml", xml: "xml",
  markdown: "md", md: "md", java: "java", go: "go", ruby: "rb", php: "php", c: "c", cpp: "cpp", csharp: "cs", cs: "cs",
  rust: "rs", kotlin: "kt", swift: "swift", hcl: "tf", terraform: "tf", powershell: "ps1", text: "txt", txt: "txt", ini: "ini", toml: "toml" };
const MIME = { csv: "text/csv", json: "application/json", html: "text/html", md: "text/markdown" };
// コードブロックに「ダウンロード」ボタンを付ける (```言語:ファイル名 ならその名前で保存)
function enhanceCodeBlocks(bubble) {
  bubble.querySelectorAll("pre > code").forEach((code) => {
    const cls = [...code.classList].find((c) => c.startsWith("language-")) || "";
    const info = cls.slice("language-".length);
    let [lang, name] = info.includes(":") ? [info.slice(0, info.indexOf(":")), info.slice(info.indexOf(":") + 1)] : [info, ""];
    if (!name && /\.[a-z0-9]{1,5}$/i.test(lang)) { name = lang; lang = lang.split(".").pop(); }
    const ext = (name.split(".").pop() || CODE_EXT[lang.toLowerCase()] || "txt").toLowerCase();
    const filename = name || `${(state.current?.title || "file").replace(/[\\/:*?"<>|]+/g, "_").slice(0, 40)}.${CODE_EXT[lang.toLowerCase()] || "txt"}`;
    const btn = el("button", { class: "code-dl", title: `${filename} としてダウンロード`, onclick: () => {
      let text = code.textContent;
      const bom = ext === "csv" || ext === "tsv" ? "\ufeff" : ""; // Excel で文字化けしないよう BOM を付ける
      const url = URL.createObjectURL(new Blob([bom + text], { type: (MIME[ext] || "text/plain") + ";charset=utf-8" }));
      const a = el("a", { href: url, download: filename }); document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } }, `⬇ ${filename}`);
    code.parentElement.classList.add("has-dl");
    code.parentElement.prepend(btn);
  });
}

function messageNode(m, isLast, prev) {
  const bubble = el("div", { class: "bubble" });
  if (m.role === "user") bubble.textContent = m.content; else { bubble.innerHTML = renderMarkdown(m.content); enhanceCodeBlocks(bubble); }
  const atts = (m.attachments || []).length
    ? el("div", { class: "chips" }, ...m.attachments.map((a) => el("span", { class: "chip", title: a.name }, "📄 ", el("span", { class: "n" }, a.name))))
    : "";
  const actions = el("div", { class: "actions" },
    el("button", { onclick: () => navigator.clipboard.writeText(m.content) }, "コピー"));
  if (m.role === "user" && m.id) actions.append(el("button", { onclick: () => startEdit(m, wrap) }, "編集"));
  let suggest = "";
  if (m.role === "assistant" && m.id) {
    const saveBtn = el("button", { onclick: (e) => { e.stopPropagation(); showExportMenu(saveBtn, m); } }, "ファイルに保存 ▾");
    actions.append(saveBtn);
    const fmt = prev?.role === "user" ? suggestFormat(prev.content) : null;
    if (fmt) {
      const label = EXPORT_FORMATS.find(([f]) => f === fmt)[1].replace(/（.*）/, "");
      suggest = el("div", { class: "suggest" }, el("button", { onclick: () => download(exportUrl(m, fmt)) }, `📄 ${label} で保存`));
    }
  }
  if (m.role === "assistant" && m.id) actions.append(el("button", { onclick: () => branchFrom(m) }, "ここから分岐"));
  if (m.role === "assistant" && isLast && m.id) actions.append(el("button", { onclick: regenerate }, "再生成"));
  const wrap = el("div", { class: `msg ${m.role}` }, atts, bubble, suggest, actions);
  return wrap;
}
function startEdit(m, wrap) {
  if (state.streaming) return;
  const ta = el("textarea", { rows: "4" }); ta.value = m.content;
  const box = el("div", { class: "edit-box" }, ta, el("div", { class: "row" },
    el("button", { type: "button", onclick: renderMessages }, "キャンセル"),
    el("button", { type: "button", class: "primary", onclick: () => { const v = ta.value.trim(); if (v) send(v, m.id); } }, "送信")));
  wrap.replaceChildren(box); ta.focus();
}

async function openConv(id) {
  if (state.streaming) state.streaming.abort();
  state.current = await api(`/api/conversations/${id}`);
  state.pending = [];
  localStorage.setItem("lastConv", id);
  updateHeader(); renderMessages(); renderConvList(); renderPending();
}
function newConv() {
  if (state.streaming) state.streaming.abort();
  state.current = { id: null, title: "新しい会話", model: null, system_prompt: null, messages: [], files: [] };
  state.pending = [];
  localStorage.removeItem("lastConv");
  updateHeader(); renderMessages(); renderConvList(); renderPending();
  $("#input").focus();
}
async function ensureConv() {
  if (state.current?.id) return state.current;
  const c = await api("/api/conversations", { method: "POST",
    body: { model: state.current?.model || null, system_prompt: state.current?.system_prompt || null } });
  state.current = { ...c, messages: [], files: [] };
  localStorage.setItem("lastConv", c.id);
  return state.current;
}
function updateHeader() {
  const c = state.current;
  $("#conv-title").textContent = c?.title || "新しい会話";
  document.title = `${c?.title || "新しい会話"} - Local LLM Chat`;
  const sel = $("#model-select");
  sel.value = c?.model || state.config.default_model;
  renderFilesMenu();
}

// ---------- files ----------
function toast(msg, ms = 4000) {
  const t = el("div", { class: "toast" }, msg);
  document.body.append(t);
  setTimeout(() => t.remove(), ms);
}
function fmtSize(n) {
  if (n == null) return "";
  if (n < 1024) return `${n}B`;
  if (n < 1048576) return `${(n / 1024).toFixed(0)}KB`;
  return `${(n / 1048576).toFixed(1)}MB`;
}
function renderFilesMenu() {
  const files = state.current?.files || [];
  $("#files-count").textContent = files.length ? files.length : "";
  const menu = $("#files-menu");
  menu.innerHTML = "";
  if (!files.length) {
    menu.append(el("div", { class: "file-row muted" }, "この会話にファイルはありません。📎 から添付できます。"));
    return;
  }
  menu.append(el("div", { class: "file-row muted" }, "この会話のファイル（回答時に参照されます）"));
  for (const f of files) {
    menu.append(el("div", { class: "file-row" },
      el("span", { class: "n", title: f.name }, "📄 " + f.name),
      el("span", { class: "muted" }, fmtSize(f.size)),
      el("a", { href: `/api/conversations/${state.current.id}/files/${f.id}`, title: "ダウンロード" }, "⬇"),
      el("button", { class: "icon", title: "削除（以降の回答で参照されなくなります）", onclick: (e) => { e.stopPropagation(); removeFile(f); } }, "✕")));
  }
}
function renderPending() {
  const box = $("#pending");
  box.innerHTML = "";
  const items = [...state.pending.map((f) => ({ ...f, done: true }))];
  for (let i = 0; i < state.uploading; i++) items.push({ name: "アップロード中…", done: false });
  box.classList.toggle("hidden", !items.length);
  if (!items.length) return;
  box.append(el("div", { class: "chips" }, ...items.map((f) => el("span", { class: "chip" + (f.done ? "" : " uploading"), title: f.name },
    "📄 ", el("span", { class: "n" }, f.name), f.done ? el("span", { class: "muted" }, " " + fmtSize(f.size)) : "",
    f.done ? el("button", { type: "button", title: "添付を取り消す", onclick: () => removeFile(f) }, "✕") : ""))));
}
async function removeFile(f) {
  if (!confirm(`「${f.name}」を削除しますか？`)) return;
  await api(`/api/conversations/${state.current.id}/files/${f.id}`, { method: "DELETE" });
  state.pending = state.pending.filter((p) => p.id !== f.id);
  state.current.files = (state.current.files || []).filter((p) => p.id !== f.id);
  renderPending(); renderFilesMenu();
}
async function uploadFiles(fileList) {
  const list = [...fileList];
  if (!list.length) return;
  const maxMb = state.config.max_upload_mb || 20;
  const ok = list.filter((f) => f.size <= maxMb * 1048576);
  if (ok.length < list.length) toast(`${maxMb}MB を超えるファイルは添付できません`);
  if (!ok.length) return;
  const c = await ensureConv();
  state.uploading += ok.length; renderPending();
  const fd = new FormData();
  ok.forEach((f) => fd.append("files", f, f.name));
  try {
    const res = await fetch(`/api/conversations/${c.id}/files`, { method: "POST", body: fd, credentials: "same-origin" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || res.statusText);
    state.pending.push(...data.files);
    c.files = [...(c.files || []), ...data.files];
    if (data.errors?.length) toast(data.errors.join(" / "), 6000);
  } catch (err) {
    toast("アップロードに失敗しました: " + err.message, 6000);
  } finally {
    state.uploading -= ok.length;
    renderPending(); renderFilesMenu(); loadConvs();
  }
}
$("#attach").onclick = () => $("#file-input").click();
$("#file-input").onchange = (e) => { uploadFiles(e.target.files); e.target.value = ""; };
$("#files-btn").onclick = (e) => { e.stopPropagation(); $("#more-menu").classList.add("hidden"); $("#files-menu").classList.toggle("hidden"); };
$("#files-menu").onclick = (e) => e.stopPropagation();
document.addEventListener("click", () => $("#files-menu").classList.add("hidden"));
// ドラッグ & ドロップ
let dragDepth = 0;
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
window.addEventListener("dragenter", (e) => { if (!hasFiles(e) || $("#app-view").classList.contains("hidden")) return; e.preventDefault(); dragDepth++; $("#drop-overlay").classList.remove("hidden"); });
window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("#drop-overlay").classList.add("hidden"); } });
window.addEventListener("drop", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault(); dragDepth = 0; $("#drop-overlay").classList.add("hidden");
  if (!$("#app-view").classList.contains("hidden")) uploadFiles(e.dataTransfer.files);
});
// クリップボードからの貼り付け (ファイル)
$("#input").addEventListener("paste", (e) => {
  const fs = [...(e.clipboardData?.files || [])];
  if (fs.length) { e.preventDefault(); uploadFiles(fs); }
});

// ---------- sending ----------
function setStreaming(ctrl) {
  state.streaming = ctrl;
  $("#send").classList.toggle("hidden", !!ctrl);
  $("#stop").classList.toggle("hidden", !ctrl);
}
async function runStream(path, body) {
  const box = $("#messages");
  const assistant = { role: "assistant", content: "" };
  const node = messageNode(assistant, false);
  const bubble = node.querySelector(".bubble");
  bubble.classList.add("cursor");
  box.append(node); box.scrollTop = box.scrollHeight;
  const ctrl = new AbortController();
  setStreaming(ctrl);
  let errorMsg = null;
  try {
    await streamPost(path, body, (ev) => {
      if (ev.type === "meta" && ev.title) { state.current.title = ev.title; updateHeader(); }
      else if (ev.type === "delta") {
        assistant.content += ev.content;
        const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
        bubble.innerHTML = renderMarkdown(assistant.content);
        if (nearBottom) box.scrollTop = box.scrollHeight;
      } else if (ev.type === "error") errorMsg = ev.message;
    }, ctrl.signal);
  } catch (err) {
    if (err.name !== "AbortError") errorMsg = err.message;
  } finally {
    setStreaming(null);
  }
  // サーバに保存された状態で再描画
  const id = state.current.id;
  state.current = await api(`/api/conversations/${id}`);
  updateHeader(); renderMessages(); loadConvs();
  if (errorMsg) $("#messages").append(el("div", { class: "msg assistant" }, el("div", { class: "bubble err" }, "⚠ " + errorMsg)));
}
async function send(text, editId = null) {
  if (state.streaming) return;
  const c = await ensureConv();
  if (editId) {
    const i = c.messages.findIndex((m) => m.id === editId);
    if (i >= 0) c.messages = c.messages.slice(0, i);
  }
  const atts = editId ? [] : state.pending.map((f) => ({ id: f.id, name: f.name }));
  if (!editId) { state.pending = []; renderPending(); }
  c.messages.push({ role: "user", content: text, attachments: atts });
  renderMessages();
  await runStream(`/api/conversations/${c.id}/messages`, { content: text, edit_message_id: editId, attachment_ids: atts.map((a) => a.id) });
}
async function regenerate() {
  if (state.streaming || !state.current?.id) return;
  const msgs = state.current.messages;
  if (msgs.length && msgs[msgs.length - 1].role === "assistant") msgs.pop();
  renderMessages();
  await runStream(`/api/conversations/${state.current.id}/regenerate`);
}
async function branchFrom(m) {
  const c = await api(`/api/conversations/${state.current.id}/duplicate`, { method: "POST", body: { upto_message_id: m.id } });
  await loadConvs(); await openConv(c.id);
}

const input = $("#input");
function autoGrow() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, window.innerHeight * 0.4) + "px"; }
input.oninput = autoGrow;
input.onkeydown = (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); }
};
$("#composer").onsubmit = (e) => {
  e.preventDefault();
  let text = input.value.trim();
  if (state.streaming) return;
  if (state.uploading) { toast("アップロードの完了をお待ちください"); return; }
  if (!text && state.pending.length) text = "添付したファイルの内容を要約してください。";
  if (!text) return;
  input.value = ""; autoGrow();
  send(text);
};
$("#stop").onclick = () => state.streaming?.abort();
$("#new-chat").onclick = newConv;
$("#toggle-sidebar").onclick = () => $("#sidebar").classList.toggle("open");

// ---------- header actions ----------
$("#conv-title").onclick = async () => {
  if (!state.current?.id) return;
  const t = prompt("会話の名前", state.current.title);
  if (t && t.trim()) {
    await api(`/api/conversations/${state.current.id}`, { method: "PATCH", body: { title: t.trim() } });
    state.current.title = t.trim(); updateHeader(); loadConvs();
  }
};
$("#model-select").onchange = async (e) => {
  const model = e.target.value === state.config.default_model ? null : e.target.value;
  state.current.model = model;
  if (state.current.id) await api(`/api/conversations/${state.current.id}`, { method: "PATCH", body: { model } });
};
$("#settings-btn").onclick = () => {
  $("#system-prompt").value = state.current?.system_prompt || "";
  $("#system-prompt").placeholder = `空欄の場合は既定: ${state.config.default_system_prompt}`;
  $("#settings-dialog").showModal();
};
$("#settings-dialog").onclose = async () => {
  if ($("#settings-dialog").returnValue !== "save") return;
  const sp = $("#system-prompt").value.trim() || null;
  state.current.system_prompt = sp;
  if (state.current.id) await api(`/api/conversations/${state.current.id}`, { method: "PATCH", body: { system_prompt: sp } });
};
$("#more-btn").onclick = (e) => { e.stopPropagation(); $("#files-menu").classList.add("hidden"); $("#more-menu").classList.toggle("hidden"); };
document.addEventListener("click", () => $("#more-menu").classList.add("hidden"));
$("#more-menu").onclick = async (e) => {
  const act = e.target.dataset.act;
  const c = state.current;
  if (!act || !c?.id) return;
  if (act === "duplicate") {
    const n = await api(`/api/conversations/${c.id}/duplicate`, { method: "POST", body: {} });
    await loadConvs(); await openConv(n.id);
  } else if (act === "export-md") location.href = `/api/conversations/${c.id}/export?format=md`;
  else if (act === "export-json") location.href = `/api/conversations/${c.id}/export?format=json`;
  else if (act === "delete" && confirm(`「${c.title}」を削除しますか？`)) {
    await api(`/api/conversations/${c.id}`, { method: "DELETE" });
    await loadConvs(); newConv();
  }
};

// ---------- boot ----------
async function loadModels() {
  const sel = $("#model-select");
  sel.innerHTML = "";
  try {
    const { models } = await api("/api/models");
    state.models = models;
  } catch { state.models = [state.config.default_model]; }
  for (const m of state.models) sel.append(el("option", { value: m }, m));
}
async function startApp() {
  state.me = await api("/api/me");
  $("#auth-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  $("#me").textContent = state.me.username;
  $("#logout").classList.toggle("hidden", state.config.auth_mode !== "local");
  await Promise.all([loadModels(), loadConvs()]);
  const last = localStorage.getItem("lastConv");
  if (last && state.convs.some((c) => c.id === last)) await openConv(last); else newConv();
}
(async () => {
  state.config = await (await fetch("/api/config")).json();
  setAuthMode(false);
  try { await startApp(); } catch { showAuth(); }
})();
