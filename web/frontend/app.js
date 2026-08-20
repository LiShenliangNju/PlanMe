/* ============================================================
   Planme 前端 · 应用逻辑
   路由 / 状态管理 / REST 对接 / 多轮对话 / 白名单编辑 / 错误处理
   后端基址与页面同源（/ui 由 FastAPI 挂载），故所有请求走相对路径 /api
   ============================================================ */
'use strict';

/* ---------------- 全局状态 ---------------- */
const state = {
  route: 'dashboard',
  chatHistory: [],          // [{role:'user'|'assistant', content, data?}]  —— 多轮上下文
  homeworkItems: [],
  homeworkFilter: 'all',
  hwWhitelist: [],
  imgWhitelist: [],
};

const TT = {
  dashboard: ['仪表盘', '系统总览 · 本地服务状态与近期动态'],
  chat:      ['智能对话', '用自然语言创建日程与待办 · POST /api/chat'],
  manual:    ['手动添加', '表单直提交 · POST /api/manual-item'],
  homework:  ['QQ 作业', '落库列表与决策状态 · GET /api/homework/items'],
  lecture:   ['讲座 / 通知', '白名单群图片 OCR 存档 · GET /api/lecture/notes'],
  status:    ['连接状态', '连接 / 运行状态与错误处理 · GET /api/health · /api/napcat/status'],
  config:    ['配置 / 白名单', '编辑作业群与图片 OCR 群 · GET·POST /api/config/whitelist'],
};

/* ---------------- 基础设施 ---------------- */
async function api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* 非 JSON 响应 */ }
  if (!res.ok) {
    const msg = (data && (data.detail || data.message)) || `请求失败（${res.status}）`;
    throw new Error(msg);
  }
  return data;
}

let toastTimer = null;
function toast(msg, type = '') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast ' + type;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 2600);
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* 简单 Markdown → HTML（用于 lecture OCR 展示） */
function inlineFmt(s) {
  return s
    .replace(/\*\*\*(.*?)\*\*\*/g, '<strong class="md-strong"><em class="md-em">$1</em></strong>')
    .replace(/\*\*(.*?)\*\*/g, '<strong class="md-strong">$1</strong>')
    .replace(/\*(.*?)\*/g, '<em class="md-em">$1</em>');
}

function parseTable(block) {
  const lines = block.trim().split(/\r?\n/).filter(l => l.trim());
  if (lines.length < 2) return null;
  const sep = lines[1].trim();
  if (!/^\|(?:\s*:?-+:?\s*\|)+$/.test(sep)) return null;
  const headers = lines[0].split('|').slice(1, -1).map(h => h.trim());
  const aligns = sep.split('|').slice(1, -1).map(c => {
    c = c.trim();
    if (c.startsWith(':') && c.endsWith(':')) return 'center';
    if (c.endsWith(':')) return 'right';
    return 'left';
  });
  let html = '<table class="md-table"><thead><tr>';
  headers.forEach((h, i) => {
    html += `<th style="text-align:${aligns[i] || 'left'}">${inlineFmt(esc(h))}</th>`;
  });
  html += '</tr></thead><tbody>';
  for (let i = 2; i < lines.length; i++) {
    const cells = lines[i].split('|').slice(1, -1).map(c => c.trim());
    html += '<tr>';
    headers.forEach((_, j) => {
      const v = cells[j] != null ? cells[j] : '';
      html += `<td style="text-align:${aligns[j] || 'left'}">${inlineFmt(esc(v))}</td>`;
    });
    html += '</tr>';
  }
  html += '</tbody></table>';
  return html;
}

function renderMd(md) {
  if (!md) return '';
  const codeBlocks = [];
  const mdWrapBlocks = [];
  const tableBlocks = [];
  let text = md;

  // 1) 先提取代码块（raw code 需要 escape）
  //    用 \x00 做占位符边界，避免后续 esc() 破坏 HTML 注释风格的标记
  //    特别处理 ```markdown：OCR 模型常把整段结果包在 markdown 代码块里，
  //    这种情况下应把内部内容当 Markdown 再渲染，而不是原样显示代码。
  text = text.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    const l = (lang || '').trim().toLowerCase();
    if (l === 'markdown' || l === 'md') {
      mdWrapBlocks.push(renderMd(code));
      return `\n\x00MDWRAP${mdWrapBlocks.length - 1}\x00\n`;
    }
    codeBlocks.push(`<pre class="md-code"><code>${esc(code)}</code></pre>`);
    return `\n\x00CODEBLOCK${codeBlocks.length - 1}\x00\n`;
  });

  // 2) 提取 Markdown 表格
  const tableRe = /((?:^\|[^\n]*\|(?:\r?\n|$))+)/gm;
  text = text.replace(tableRe, (block) => {
    const html = parseTable(block);
    if (!html) return block;
    tableBlocks.push(html);
    return `\n\x00TABLEBLOCK${tableBlocks.length - 1}\x00\n`;
  });

  // 3) 对其余普通文本做 HTML 转义
  text = esc(text);

  // 4) 行内代码
  text = text.replace(/`([^`]+)`/g, '<code class="md-code-inline">$1</code>');

  // 5) 解析为 token：heading / list_item / blank / paragraph / codeblock / tableblock / mdwrap
  const tokens = [];
  for (let raw of text.split('\n')) {
    const cb = raw.match(/\x00CODEBLOCK(\d+)\x00/);
    if (cb) {
      tokens.push({ kind: 'codeblock', index: parseInt(cb[1], 10) });
      continue;
    }
    const mw = raw.match(/\x00MDWRAP(\d+)\x00/);
    if (mw) {
      tokens.push({ kind: 'mdwrap', index: parseInt(mw[1], 10) });
      continue;
    }
    const tb = raw.match(/\x00TABLEBLOCK(\d+)\x00/);
    if (tb) {
      tokens.push({ kind: 'tableblock', index: parseInt(tb[1], 10) });
      continue;
    }
    const h = raw.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      tokens.push({ kind: 'heading', level: h[1].length, text: h[2] });
      continue;
    }
    if (raw.trim() === '') {
      tokens.push({ kind: 'blank' });
      continue;
    }
    const leading = raw.match(/^(\s*)/)[1].length;
    const trimmed = raw.trimStart();
    const depth = Math.floor(leading / 2);
    const ul = trimmed.match(/^[-*]\s+(.*)$/);
    const ol = trimmed.match(/^(\d+)\.\s+(.*)$/);
    if (ul) {
      tokens.push({ kind: 'list_item', type: 'ul', depth, text: ul[1] });
      continue;
    }
    if (ol) {
      tokens.push({ kind: 'list_item', type: 'ol', depth, order: ol[1], text: ol[2] });
      continue;
    }
    tokens.push({ kind: 'paragraph', text: raw });
  }

  const out = [];
  const listStack = []; // {type, depth}
  let openLi = false;   // 是否有未关闭的 <li>

  const closeLi = () => {
    if (openLi) { out.push('</li>'); openLi = false; }
  };
  const closeListsTo = (depth, type) => {
    // 关闭到指定深度/类型；同时关闭未关闭的 li
    while (listStack.length) {
      const top = listStack[listStack.length - 1];
      if (top.depth < depth) break;
      if (top.depth === depth && top.type === type) break;
      closeLi();
      listStack.pop();
      out.push(top.type === 'ul' ? '</ul>' : '</ol>');
    }
  };
  const ensureList = (type, depth) => {
    if (listStack.length === 0) {
      listStack.push({ type, depth });
      out.push(`<${type} class="md-${type}">`);
      return;
    }
    const top = listStack[listStack.length - 1];
    if (top.depth === depth && top.type === type) {
      // 同级同类型：关闭前一个 li，开新 li
      closeLi();
      return;
    }
    if (top.depth < depth) {
      // 进入子列表：不关闭父 li
      listStack.push({ type, depth });
      out.push(`<${type} class="md-${type}">`);
      return;
    }
    // 回溯到合适层级
    closeListsTo(depth, type);
    if (listStack.length && listStack[listStack.length - 1].depth === depth && listStack[listStack.length - 1].type === type) {
      closeLi();
      return;
    }
    listStack.push({ type, depth });
    out.push(`<${type} class="md-${type}">`);
  };
  const flushLists = () => {
    closeLi();
    while (listStack.length) {
      const top = listStack.pop();
      out.push(top.type === 'ul' ? '</ul>' : '</ol>');
    }
  };

  for (let i = 0; i < tokens.length; i++) {
    const tok = tokens[i];
    if (tok.kind === 'codeblock' || tok.kind === 'tableblock' || tok.kind === 'mdwrap') {
      closeLi();
      flushLists();
      out.push(
        tok.kind === 'codeblock' ? codeBlocks[tok.index]
        : tok.kind === 'tableblock' ? tableBlocks[tok.index]
        : mdWrapBlocks[tok.index]
      );
      continue;
    }
    if (tok.kind === 'heading') {
      closeLi();
      flushLists();
      out.push(`<h${tok.level} class="md-h${tok.level}">${inlineFmt(tok.text)}</h${tok.level}>`);
      continue;
    }
    if (tok.kind === 'blank') continue; // 空行不切断列表
    if (tok.kind === 'paragraph') {
      closeLi();
      flushLists();
      out.push(`<p class="md-p">${inlineFmt(tok.text)}</p>`);
      continue;
    }
    if (tok.kind === 'list_item') {
      ensureList(tok.type, tok.depth);
      const valueAttr = tok.order != null ? ` value="${tok.order}"` : '';
      // 统一延迟关闭 li，便于嵌套子列表；在切换/离开列表时由 closeLi 关闭
      out.push(`<li${valueAttr}>${inlineFmt(tok.text)}`);
      openLi = true;
    }
  }
  flushLists();
  return out.join('\n');
}

/* ---------------- 路由 ---------------- */
function navigate(route) {
  state.route = route;
  document.querySelectorAll('.page').forEach(p => p.classList.add('hidden'));
  const page = document.getElementById('page-' + route);
  if (page) page.classList.remove('hidden');

  document.querySelectorAll('.nav-item').forEach(n =>
    n.classList.toggle('active', n.dataset.route === route));

  const [main, sub] = TT[route] || ['', ''];
  document.getElementById('ttMain').textContent = main;
  document.getElementById('ttSub').textContent = sub;

  // 离开对话页时不打断；进入各页按需加载数据
  if (route === 'dashboard') loadDashboard();
  if (route === 'homework') loadHomework();
  if (route === 'lecture') loadLecture();
  if (route === 'status') loadStatus();
  if (route === 'config') loadConfig();
  window.scrollTo(0, 0);
}

/* ---------------- 状态药丸 / 健康 ---------------- */
async function fetchStatus() {
  let backendOk = false, napcatOk = false, model = '未知', scannerRunning = false;
  try {
    const h = await api('GET', '/api/health');
    backendOk = h && h.status === 'healthy';
    model = (h && h.agent_model) || model;
  } catch (_) { /* 后端未启动 */ }

  try {
    const n = await api('GET', '/api/napcat/status');
    napcatOk = !!(n && n.connected);
    scannerRunning = !!(n && n.scanner_running);
  } catch (_) { /* 未连接 */ }

  // 顶栏药丸
  setPill('pillBackend', backendOk, backendOk ? '后端正常' : '后端离线');
  setPill('pillNapcat', napcatOk, napcatOk ? 'NapCat 已连接' : 'NapCat 离线');

  // 仪表盘统计
  document.getElementById('dbModel').textContent = `${model} · ${backendOk ? '正常' : '离线'}`;
  document.getElementById('dbScanner').textContent =
    scannerRunning ? '运行中 · 0 待确认' : (napcatOk ? '已连接' : '未连接');
  return { backendOk, napcatOk, model, scannerRunning };
}
function setPill(id, ok, txt) {
  const el = document.getElementById(id);
  el.classList.toggle('off', !ok);
  el.querySelector('.pill-txt').textContent = txt;
}

/* ---------------- 仪表盘 ---------------- */
async function loadDashboard() {
  await fetchStatus();
  try {
    const r = await api('GET', '/api/homework/items');
    const items = (r && r.items) || [];
    const pending = items.filter(i => i.status === 'pending').length;
    const todo = items.length;
    document.getElementById('dbTodo').textContent = Math.max(3, todo);
    document.getElementById('dbHw').textContent = pending;
    document.getElementById('dbToday').textContent = `${Math.max(3, todo)} 待办 · ${pending} 待确认`;
  } catch (_) { /* 静默 */ }
  await loadActivity();
}

async function loadActivity() {
  const box = document.getElementById('activityList');
  if (!box) return;
  try {
    const r = await api('GET', '/api/activity?days=3');
    const acts = (r && r.activities) || [];
    if (!acts.length) {
      box.innerHTML = '<div class="act-item">• 近三天暂无动态</div>';
      return;
    }
    box.innerHTML = acts.map(a =>
      `<div class="act-item">• ${esc(a.formatted || '')} ${esc(a.text || '')}</div>`
    ).join('');
  } catch (_) {
    box.innerHTML = '<div class="act-item">• 近期动态加载失败</div>';
  }
}

/* ---------------- 智能对话（多轮） ---------------- */
const GREETING = '你好！我是 Planme 📅 你可以用自然语言告诉我任何日程或待办，我会自动同步到 iCloud 日历。';

function renderChat() {
  const box = document.getElementById('chatMessages');
  box.innerHTML = '';
  const bubble = (cls, html) => {
    const d = document.createElement('div');
    d.className = 'msg ' + cls;
    d.innerHTML = html;
    return d;
  };
  box.appendChild(bubble('assistant', esc(GREETING)));
  state.chatHistory.forEach(m => {
    if (m.role === 'user') {
      box.appendChild(bubble('user', esc(m.content)));
    } else {
      let html = esc(m.content);
      if (m.data) html += renderItemCard(m.data);
      box.appendChild(bubble('assistant', html));
    }
  });
  box.scrollTop = box.scrollHeight;
}

function renderItemCard(d) {
  const typeIcon = d.item_type === 'Todo' ? '📌' : '📅';
  const rows = [
    d.start_time ? `🕒 ${esc(d.start_time)}` : '',
    d.location ? `📍 ${esc(d.location)}` : '',
    d.url ? `🔗 ${esc(d.url)}` : '',
  ].filter(Boolean).join('<br>');
  return `<div class="item-card">
      <div class="ic-title">${typeIcon} ${esc(d.item_type || 'Item')} · ${esc(d.summary || '')}</div>
      ${rows ? `<div class="ic-row">${rows}</div>` : ''}
    </div>`;
}

async function sendChat() {
  const input = document.getElementById('chatInput');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';

  // 先把本轮用户消息入上下文（后端会再追加，避免重复）
  state.chatHistory.push({ role: 'user', content: text });
  renderChat();

  // 思考中指示
  const box = document.getElementById('chatMessages');
  const typing = document.createElement('div');
  typing.className = 'typing';
  typing.innerHTML = '<span></span><span></span><span></span>';
  box.appendChild(typing);
  box.scrollTop = box.scrollHeight;

  try {
    const r = await api('POST', '/api/chat', {
      text,
      history: state.chatHistory.slice(0, -1), // 仅传此前历史，当前 user 由后端拼接
    });
    typing.remove();
    const reply = (r && r.message) || '（无回复）';
    state.chatHistory.push({ role: 'assistant', content: reply, data: r && r.data });
    renderChat();
    if (r && r.status === 'error') toast(reply, 'err');
  } catch (e) {
    typing.remove();
    state.chatHistory.push({ role: 'assistant', content: '⚠️ ' + e.message });
    renderChat();
    toast('对话请求失败：' + e.message, 'err');
  }
}

/* ---------------- 手动添加 ---------------- */
function initManual() {
  document.querySelectorAll('#manualType .seg-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#manualType .seg-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
    });
  });
}

async function submitManual() {
  const itemType = document.querySelector('#manualType .seg-btn.active').dataset.type;
  const summary = document.getElementById('mSummary').value.trim();
  const date = document.getElementById('mDate').value;
  const time = document.getElementById('mTime').value || '00:00';
  if (!summary || !date) { toast('标题与开始日期为必填', 'err'); return; }

  const payload = {
    item_type: itemType,
    summary,
    start_time: `${date}T${time}:00`,
  };
  if (itemType === 'Event') {
    const dur = parseInt(document.getElementById('mDuration').value, 10);
    if (!isNaN(dur)) payload.duration_minutes = dur;
  }
  const loc = document.getElementById('mLocation').value.trim();
  const url = document.getElementById('mUrl').value.trim();
  const desc = document.getElementById('mDesc').value.trim();
  if (loc) payload.location = loc;
  if (url) payload.url = url;
  if (desc) payload.description = desc;

  try {
    const r = await api('POST', '/api/manual-item', payload);
    toast('✅ 已提交至 iCloud', 'ok');
    document.getElementById('mSummary').value = '';
    document.getElementById('mDuration').value = '';
    document.getElementById('mLocation').value = '';
    document.getElementById('mUrl').value = '';
    document.getElementById('mDesc').value = '';
    console.log('创建结果：', r);
  } catch (e) {
    toast('提交失败：' + e.message, 'err');
  }
}

/* ---------------- QQ 作业 ---------------- */
const HW_STATUS = {
  pending:  { label: '⏳ 待确认', cls: 'amber', key: 'pending' },
  confirmed:{ label: '✅ 已确认', cls: 'green', key: 'confirmed' },
  auto:     { label: '✅ 已自动加入', cls: 'green', key: 'confirmed' },
  ignored:  { label: '🚫 已忽略', cls: 'gray', key: 'ignored' },
  drop:     { label: '🚫 已忽略', cls: 'gray', key: 'ignored' },
};

async function loadHomework() {
  try {
    const r = await api('GET', '/api/homework/items');
    state.homeworkItems = (r && r.items) || [];
  } catch (e) {
    state.homeworkItems = [];
    toast('加载作业失败：' + e.message, 'err');
  }
  renderHomework();
}

function renderHomework() {
  const items = state.homeworkItems;
  const counts = { all: items.length, pending: 0, confirmed: 0, ignored: 0 };
  items.forEach(i => {
    const k = (HW_STATUS[i.status] || {}).key;
    if (k && counts[k] !== undefined) counts[k]++;
  });
  document.getElementById('cntAll').textContent = counts.all;
  document.getElementById('cntPending').textContent = counts.pending;
  document.getElementById('cntConfirmed').textContent = counts.confirmed;
  document.getElementById('cntIgnored').textContent = counts.ignored;

  const list = state.homeworkFilter === 'all'
    ? items
    : items.filter(i => (HW_STATUS[i.status] || {}).key === state.homeworkFilter);

  const rows = document.getElementById('hwRows');
  rows.innerHTML = '';
  document.getElementById('hwEmpty').classList.toggle('hidden', list.length > 0);
  list.forEach(it => {
    const s = HW_STATUS[it.status] || { label: it.status, cls: 'gray' };
    const row = document.createElement('div');
    row.className = 'row-item';
    row.innerHTML = `
      <div class="ri-sub">
        <div class="ri-title">${esc(it.subject || '（未命名作业）')}</div>
        <div class="ri-desc">${esc(it.description || it.raw_content || '')}</div>
      </div>
      <div class="ri-cell">${esc(it.deadline || '—')}</div>
      <div class="ri-cell muted-cell">${esc(it.group_name || it.group_id || '—')}</div>
      <div class="ri-cell">${esc(it.confidence != null ? it.confidence : '—')}</div>
      <div class="ri-cell"><span class="badge ${s.cls}">${s.label}</span></div>`;
    rows.appendChild(row);
  });
}

/* ---------------- 讲座 / 通知 ---------------- */
async function loadLecture() {
  try {
    const r = await api('GET', '/api/lecture/notes');
    const notes = (r && r.notes) || [];
    const pending = (r && r.pending) || 0;
    document.getElementById('lecNote').textContent =
      `白名单群图片经 OCR 后存档于此（Web 只读）。共 ${notes.length} 条，其中 ${pending} 条排队 OCR 中。`;
    const grid = document.getElementById('lecGrid');
    grid.innerHTML = '';
    document.getElementById('lecEmpty').classList.toggle('hidden', notes.length > 0);
    notes.forEach(n => {
      const st = n.status === 'active' || n.status === 'done' ? { t: '✅ 已存档', c: 'green' }
        : n.status === 'pending' ? { t: '⏳ 排队 OCR', c: 'amber' }
        : { t: '⚠️ OCR 失败', c: 'red' };
      const card = document.createElement('div');
      card.className = 'note-card';
      const link = n.local_path || n.image_url || '';
      card.innerHTML = `
        <div class="note-top">
          <div class="note-group">${esc(n.group_name || n.group_id || '群')}</div>
          <span class="badge ${st.c}">${st.t}</span>
        </div>
        <div class="note-time">${esc(n.created_at || '')}</div>
        <div class="note-ocr">${n.ocr_md ? renderMd(n.ocr_md) : '（暂无 OCR 内容）'}</div>
        ${link ? `<div class="note-foot">🔗 原图 · ${esc(link)}</div>` : ''}`;
      grid.appendChild(card);
    });
  } catch (e) {
    toast('加载讲座失败：' + e.message, 'err');
  }
}

/* ---------------- 连接状态 ---------------- */
async function loadStatus() {
  await fetchStatus();
  const s = await apiSafe('GET', '/api/napcat/status');
  const h = await apiSafe('GET', '/api/health');
  const hw = await apiSafe('GET', '/api/homework/status');
  const items = [
    { label: '后端服务 · GET /api/health', ok: !!(h && h.status === 'healthy'), val: (h && h.status === 'healthy') ? '正常' : '离线' },
    { label: 'NapCat · OneBot WS (127.0.0.1:3001)', ok: !!(s && s.connected), val: (s && s.connected) ? '已连接' : '未连接' },
    { label: '作业扫描器 · Scanner', ok: !!(hw && hw.running), val: (hw && hw.running) ? '运行中' : '未运行' },
    { label: 'Ollama 模型', ok: !!(h && h.agent_model), val: (h && h.agent_model) ? h.agent_model : '未知' },
  ];
  const list = document.getElementById('statusList');
  list.innerHTML = '';
  items.forEach(it => {
    const d = document.createElement('div');
    d.className = 'status-item';
    d.innerHTML = `<div class="status-label">${esc(it.label)}</div>
      <div class="status-val ${it.ok ? '' : 'off'}">● ${esc(it.val)}</div>`;
    list.appendChild(d);
  });

  // 事件 / 错误日志：合并不通分发源
  const log = document.getElementById('statusLog');
  log.innerHTML = '';
  const entries = [];
  const pushes = await apiSafe('GET', '/api/napcat/pushes');
  (pushes && pushes.pushes || []).forEach(p => entries.push(p));
  const feed = await apiSafe('GET', '/api/homework/feed');
  (feed && feed.feed || []).forEach(f => entries.push(f));
  if (!entries.length) {
    log.innerHTML = '<div class="act-item">暂无事件记录。系统就绪，等待 QQ 群消息与对话请求。</div>';
    return;
  }
  entries.slice(0, 10).forEach(e => {
    const div = document.createElement('div');
    div.className = 'act-item';
    div.textContent = '• ' + (e.time ? e.time + ' ' : '') + (e.message || e.kind || JSON.stringify(e));
    log.appendChild(div);
  });
}

async function apiSafe(method, path) {
  try { return await api(method, path); } catch (_) { return null; }
}

/* ---------------- 配置 / 白名单 ---------------- */
async function loadConfig() {
  try {
    const r = await api('GET', '/api/config/whitelist');
    state.hwWhitelist = (r && r.homework_groups) || [];
    state.imgWhitelist = (r && r.image_groups) || [];
    renderWhitelist();
  } catch (e) {
    toast('加载白名单失败：' + e.message, 'err');
  }
}

function renderWhitelist() {
  renderTags('hwWhitelist', state.hwWhitelist);
  renderTags('imgWhitelist', state.imgWhitelist);
}
function renderTags(elId, arr) {
  const box = document.getElementById(elId);
  box.innerHTML = '';
  arr.forEach((gid, idx) => {
    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.innerHTML = `<span>${esc(gid)}</span><button class="rm" title="移除">×</button>`;
    tag.querySelector('.rm').addEventListener('click', () => {
      arr.splice(idx, 1);
      renderTags(elId, arr);
    });
    box.appendChild(tag);
  });
}

function addGroup(which, inputId) {
  const input = document.getElementById(inputId);
  const val = parseInt(input.value, 10);
  if (isNaN(val)) { toast('请输入有效的群号', 'err'); return; }
  const arr = which === 'hw' ? state.hwWhitelist : state.imgWhitelist;
  if (!arr.includes(val)) arr.push(val);
  input.value = '';
  renderWhitelist();
}

async function saveWhitelist(which) {
  const payload = which === 'hw'
    ? { homework_groups: state.hwWhitelist.map(Number) }
    : { image_groups: state.imgWhitelist.map(Number) };
  try {
    await api('POST', '/api/config/whitelist', payload);
    toast('✅ 白名单已保存，重启扫描器后生效', 'ok');
  } catch (e) {
    toast('保存失败：' + e.message, 'err');
  }
}

/* ---------------- 事件绑定 ---------------- */
function bindEvents() {
  document.querySelectorAll('.nav-item').forEach(n =>
    n.addEventListener('click', () => navigate(n.dataset.route)));
  document.querySelectorAll('[data-goto]').forEach(b =>
    b.addEventListener('click', () => navigate(b.dataset.goto)));

  document.getElementById('chatSend').addEventListener('click', sendChat);
  document.getElementById('chatInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') sendChat();
  });

  initManual();
  document.getElementById('manualSubmit').addEventListener('click', submitManual);

  document.querySelectorAll('#hwChips .chip').forEach(c =>
    c.addEventListener('click', () => {
      document.querySelectorAll('#hwChips .chip').forEach(x => x.classList.remove('active'));
      c.classList.add('active');
      state.homeworkFilter = c.dataset.status;
      renderHomework();
    }));
  document.getElementById('hwRefresh').addEventListener('click', loadHomework);

  document.getElementById('hwAddBtn').addEventListener('click', () => addGroup('hw', 'hwAdd'));
  document.getElementById('imgAddBtn').addEventListener('click', () => addGroup('img', 'imgAdd'));
  document.getElementById('hwSaveBtn').addEventListener('click', () => saveWhitelist('hw'));
  document.getElementById('imgSaveBtn').addEventListener('click', () => saveWhitelist('img'));
}

/* ---------------- 启动 ---------------- */
document.addEventListener('DOMContentLoaded', () => {
  bindEvents();
  navigate('dashboard');
});
