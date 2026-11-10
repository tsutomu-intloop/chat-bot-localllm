"use strict";
const $ = (s) => document.querySelector(s);
const state = { config: null, me: null, convs: [], current: null, models: [], streaming: null, signup: false };

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
      el("p", { class: "muted" }, "会話は自動で保存され、左の履歴からいつでも再開・複製できます。")));
    return;
  }
  msgs.forEach((m, idx) => box.append(messageNode(m, idx === msgs.length - 1)));
  box.scrollTop = box.scrollHeight;
}
function messageNode(m, isLast) {
  const bubble = el("div", { class: "bubble" });
  if (m.role === "user") bubble.textContent = m.content; else bubble.innerHTML = renderMarkdown(m.content);
  const actions = el("div", { class: "actions" },
    el("button", { onclick: () => navigator.clipboard.writeText(m.content) }, "コピー"));
  if (m.role === "user" && m.id) actions.append(el("button", { onclick: () => startEdit(m, wrap) }, "編集"));
  if (m.role === "assistant" && m.id) actions.append(el("button", { onclick: () => branchFrom(m) }, "ここから分岐"));
  if (m.role === "assistant" && isLast && m.id) actions.append(el("button", { onclick: regenerate }, "再生成"));
  const wrap = el("div", { class: `msg ${m.role}` }, bubble, actions);
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
  localStorage.setItem("lastConv", id);
  updateHeader(); renderMessages(); renderConvList();
}
function newConv() {
  if (state.streaming) state.streaming.abort();
  state.current = { id: null, title: "新しい会話", model: null, system_prompt: null, messages: [] };
  localStorage.removeItem("lastConv");
  updateHeader(); renderMessages(); renderConvList();
  $("#input").focus();
}
async function ensureConv() {
  if (state.current?.id) return state.current;
  const c = await api("/api/conversations", { method: "POST",
    body: { model: state.current?.model || null, system_prompt: state.current?.system_prompt || null } });
  state.current = { ...c, messages: [] };
  localStorage.setItem("lastConv", c.id);
  return state.current;
}
function updateHeader() {
  const c = state.current;
  $("#conv-title").textContent = c?.title || "新しい会話";
  document.title = `${c?.title || "新しい会話"} - Local LLM Chat`;
  const sel = $("#model-select");
  sel.value = c?.model || state.config.default_model;
}

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
  c.messages.push({ role: "user", content: text });
  renderMessages();
  await runStream(`/api/conversations/${c.id}/messages`, { content: text, edit_message_id: editId });
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
  const text = input.value.trim();
  if (!text || state.streaming) return;
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
$("#more-btn").onclick = (e) => { e.stopPropagation(); $("#more-menu").classList.toggle("hidden"); };
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
