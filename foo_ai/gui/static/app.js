/* The desktop page. It keeps no transcript of its own: it draws the events the
   hub appends, one card per block, and polls /api/events while a request runs.

   A request is one POST; everything the model streams comes back through the
   poll, so a reload of the page shows the same conversation from
   /api/history. */

const $ = (id) => document.getElementById(id);

const transcript = $('transcript');
const transcriptEmpty = $('transcript-empty');
const activity = $('activity');
const rightPanel = $('right');
const promptEl = $('prompt');
const confirmBar = $('confirm');
const confirmCommand = $('confirm-command');
const askBar = $('ask');
const askBody = $('ask-body');
const askTitle = $('ask-title');
const askHint = $('ask-hint');
const todoBox = $('todos');
const todoList = $('todo-list');
const todoProgress = $('todo-progress');
const todoFill = $('todo-fill');
const todoMeter = todoFill.parentElement;
const meter = $('meter');
const meterText = $('meter-text');
const meterFill = $('meter-fill');
const statusBox = $('status');
const statusText = $('status-text');
const toastEl = $('toast');
const busyEl = $('busy');
const busyText = $('busy-text');
const settingsLayer = $('settings');
const settingsFile = $('settings-file');
const settingsProvider = $('settings-provider');
const settingsBaseUrl = $('settings-base-url');
const settingsModel = $('settings-model');
const settingsWindow = $('settings-window');
const settingsKey = $('settings-key');
const settingsKeyClear = $('settings-key-clear');
const settingsReasoning = $('settings-reasoning');
const settingsEffort = $('settings-effort');
const settingsThinkingParam = $('settings-thinking-param');

const state = {
  seq: 0,
  status: 'idle',
  usage: null,
  failed: false,
  block: null,
  tool: null,
  autoScroll: true,
  skills: [],
  workspace: '',
  providers: [],
  providerName: '',
  provider: null,
  settingsFile: '',
  maxSteps: null,
  thinking: false,
  verbose: false,
  yes: false,
  confirmCommand: '',
  questions: [],
  lastEventAt: null,
};

let pollTimer = null;
let toastTimer = null;

/* ------------------------------------------------------------------ fetch */

async function fetchJson(path) {
  const response = await fetch(path, { cache: 'no-store', headers: { Accept: 'application/json' } });
  if (!response.ok) throw new Error(`HTTP ${response.status} from ${path}`);
  return response.json();
}

async function postJson(path, body) {
  const response = await fetch(path, {
    method: 'POST',
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body || {}),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status} from ${path}`);
  return response.json();
}

/* ------------------------------------------------------------------- view */

function toast(text, kind) {
  toastEl.textContent = text;
  toastEl.className = `toast ${kind || ''}`.trim();
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toastEl.hidden = true;
  }, 4200);
}

function setStatus(kind, text) {
  statusBox.className = `status ${kind}`.trim();
  statusText.textContent = text;
}

function setBusy(busy, text) {
  busyEl.hidden = !busy;
  if (text) busyText.textContent = text;
}

function clock() {
  return new Date().toLocaleTimeString([], { hour12: false });
}

function logActivity(kind, text) {
  if (!text) return;
  const row = document.createElement('div');
  row.className = `act ${kind}`;
  const when = document.createElement('span');
  when.className = 'when';
  when.textContent = clock();
  const body = document.createElement('span');
  body.className = 'text';
  body.textContent = text;
  row.append(when, body);
  activity.append(row);
  while (activity.childElementCount > 400) activity.firstElementChild.remove();
  rightPanel.scrollTop = rightPanel.scrollHeight;
}

function renderMeter() {
  const usage = state.usage;
  if (!usage) {
    meterText.textContent = '–';
    meterFill.style.width = '0%';
    meter.className = 'meter';
    meter.title = 'context window';
    return;
  }
  meterText.textContent = usage.window ? `${usage.used}/${usage.window}` : usage.used;
  const percent = Math.round(usage.fraction * 1000) / 10;
  meterFill.style.width = `${Math.min(100, percent)}%`;
  meter.title = usage.line;
  meter.className = 'meter';
  if (percent >= 80) meter.classList.add('bad');
  else if (percent >= 60) meter.classList.add('warn');
}

function isLive() {
  return state.status === 'running' || state.status === 'approving' || state.status === 'asking';
}

function renderStatusFromState() {
  if (state.status === 'running') {
    const span = elapsedLabel();
    return setStatus('warn', span ? `working \u00b7 ${span}` : 'working');
  }
  if (state.status === 'approving') return setStatus('warn', 'approval');
  if (state.status === 'asking') return setStatus('warn', 'question');
  setStatus(state.failed ? 'bad' : 'ok', state.failed ? 'error' : 'ready');
}

/* How long since anything arrived from the model. A stalled round shows this
   number climbing instead of an unchanged "working". */
function elapsedLabel() {
  if (!state.lastEventAt) return '';
  const seconds = Math.max(0, Math.floor((Date.now() - state.lastEventAt) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

function autoScroll() {
  if (!state.autoScroll) return;
  requestAnimationFrame(() => {
    transcript.scrollTop = transcript.scrollHeight;
  });
}

function reveal() {
  transcriptEmpty.hidden = true;
}

/* --------------------------------------------------------------- markdown */

function escapeHtml(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function inline(text) {
  let out = text.replace(/`([^`\n]+)`/g, (_, code) => `<code>${code}</code>`);
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  out = out.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noreferrer noopener">$1</a>',
  );
  return out;
}

/* A pipe outside inline code. Pipes inside `code` and escaped \| do not
   count, so a sentence that mentions a command is not a table row. */
function hasPipe(line) {
  let code = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === '`') {
      code = !code;
      continue;
    }
    if (ch === '\\' && line[i + 1] === '|') {
      i += 1;
      continue;
    }
    if (ch === '|' && !code) return true;
  }
  return false;
}

/* Split one GFM row. Leading and trailing pipes are the borders, not cells.
   A pipe inside `code` or written as \| stays in the cell. */
function splitRow(line) {
  let text = line.trim().replace(/\r$/, '');
  if (text.startsWith('|')) text = text.slice(1);
  if (text.endsWith('|')) text = text.slice(0, -1);
  const cells = [];
  let buf = '';
  let code = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === '`') {
      code = !code;
      buf += ch;
      continue;
    }
    if (ch === '\\' && text[i + 1] === '|') {
      buf += '|';
      i += 1;
      continue;
    }
    if (ch === '|' && !code) {
      cells.push(buf.trim());
      buf = '';
      continue;
    }
    buf += ch;
  }
  cells.push(buf.trim());
  return cells;
}

/* | --- | :---: | ---: | : at least three dashes, optional alignment colons. */
function isSeparatorRow(line) {
  if (!hasPipe(line)) return false;
  const cells = splitRow(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function columnAlign(cell) {
  const left = cell.startsWith(':');
  const right = cell.endsWith(':');
  if (left && right) return 'center';
  if (right) return 'right';
  return '';
}

function tableCell(tag, text, align) {
  const cls = align === 'center' ? ' class="c"' : align === 'right' ? ' class="r"' : '';
  return `<${tag}${cls}>${inline(text)}</${tag}>`;
}

function renderTable(header, aligns, rows) {
  const width = Math.max(
    header.length,
    aligns.length,
    ...rows.map((row) => row.length),
    1,
  );
  const head = [];
  for (let col = 0; col < width; col++) {
    head.push(tableCell('th', header[col] || '', aligns[col] || ''));
  }
  const body = rows.map((row) => {
    const cells = [];
    for (let col = 0; col < width; col++) {
      cells.push(tableCell('td', row[col] || '', aligns[col] || ''));
    }
    return `<tr>${cells.join('')}</tr>`;
  }).join('');
  const bodyHtml = body ? `<tbody>${body}</tbody>` : '';
  return `<div class="md-table"><table><thead><tr>${head.join('')}</tr></thead>${bodyHtml}</table></div>`;
}

/* A small, safe subset: fences, headings, lists, tables, inline code, bold,
   italics, and http links. Everything is escaped before a tag is added, so a
   reply can never inject markup. An unterminated fence streams as code, which
   is what a half-written block looks like. A table needs its separator row
   before it is drawn; until then the lines stay text. */
function renderMarkdown(text) {
  const lines = escapeHtml(text).split('\n');
  const out = [];
  let fence = null;
  let list = null;
  let para = [];

  const flushPara = () => {
    if (para.length === 0) return;
    out.push(`<p>${inline(para.join(' '))}</p>`);
    para = [];
  };
  const flushList = () => {
    if (list === null) return;
    out.push(`</${list}>`);
    list = null;
  };
  const flushFence = () => {
    if (fence === null) return;
    out.push(`<pre><code>${fence.join('\n')}</code></pre>`);
    fence = null;
  };

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    if (fence !== null) {
      if (/^\s*```/.test(raw)) flushFence();
      else fence.push(raw);
      continue;
    }
    if (/^\s*```/.test(raw)) {
      flushPara();
      flushList();
      fence = [];
      continue;
    }
    if (raw.trim() === '') {
      flushPara();
      flushList();
      continue;
    }
    if (hasPipe(raw) && i + 1 < lines.length && isSeparatorRow(lines[i + 1])) {
      flushPara();
      flushList();
      const header = splitRow(raw);
      const aligns = splitRow(lines[i + 1]).map(columnAlign);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].trim() !== '' && hasPipe(lines[i]) && !/^\s*```/.test(lines[i])) {
        /* A following table starts at its own header, so leave that line for
           the next pass instead of swallowing it as a body row. */
        if (i + 1 < lines.length && isSeparatorRow(lines[i + 1]) && !isSeparatorRow(lines[i])) break;
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      out.push(renderTable(header, aligns, rows));
      i -= 1;
      continue;
    }
    const heading = raw.match(/^#{1,6}\s+(.*)$/);
    if (heading) {
      flushPara();
      flushList();
      out.push(`<h3>${inline(heading[1].trim())}</h3>`);
      continue;
    }
    const bullet = raw.match(/^\s*[-*+]\s+(.*)$/);
    if (bullet) {
      flushPara();
      if (list !== 'ul') {
        flushList();
        out.push('<ul>');
        list = 'ul';
      }
      out.push(`<li>${inline(bullet[1])}</li>`);
      continue;
    }
    const numbered = raw.match(/^\s*\d+[.)]\s+(.*)$/);
    if (numbered) {
      flushPara();
      if (list !== 'ol') {
        flushList();
        out.push('<ol>');
        list = 'ol';
      }
      out.push(`<li>${inline(numbered[1])}</li>`);
      continue;
    }
    flushList();
    para.push(raw.trim());
  }
  flushPara();
  flushList();
  flushFence();
  return out.join('');
}

/* ------------------------------------------------------------------ cards */

function makeBlock(role, chip) {
  const msg = document.createElement('article');
  msg.className = `msg ${role}`;
  const head = document.createElement('div');
  head.className = 'msg-head';
  const kind = document.createElement('span');
  kind.className = 'kind';
  kind.textContent = chip;
  head.append(kind);
  const body = document.createElement('div');
  body.className = 'msg-body';
  msg.append(head, body);
  msg.body = body;
  msg.text = '';
  return msg;
}

function scheduleRender(block) {
  if (block.pending) return;
  block.pending = true;
  requestAnimationFrame(() => {
    block.pending = false;
    block.body.innerHTML = renderMarkdown(block.text);
  });
}

function assistantBlock() {
  if (state.block) return state.block;
  const msg = makeBlock('assistant', 'foo');
  transcript.append(msg);
  reveal();
  state.block = msg;
  return msg;
}

function addUser(text) {
  const msg = makeBlock('user', 'you');
  msg.body.textContent = text;
  transcript.append(msg);
  reveal();
  state.block = null;
  state.tool = null;
  autoScroll();
}

function addContent(text) {
  const block = assistantBlock();
  block.text += text;
  scheduleRender(block);
  autoScroll();
}

function addReasoning(text) {
  const block = assistantBlock();
  let body = block.reasoning;
  if (!body) {
    const details = document.createElement('details');
    details.className = 'reasoning';
    const summary = document.createElement('summary');
    summary.textContent = 'thinking';
    body = document.createElement('div');
    body.className = 'reasoning-body';
    details.append(summary, body);
    block.append(details);
    block.reasoning = body;
  }
  body.textContent += text;
  autoScroll();
}

function addTool(name) {
  const msg = document.createElement('article');
  msg.className = 'msg tool';
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  const kind = document.createElement('span');
  kind.className = 'kind';
  kind.textContent = 'tool';
  const label = document.createElement('span');
  label.className = 'name';
  label.textContent = name || '…';
  const chars = document.createElement('span');
  chars.className = 'chars';
  chars.textContent = 'running…';
  summary.append(kind, label, chars);
  const pre = document.createElement('pre');
  pre.className = 'tool-result';
  pre.hidden = true;
  details.append(summary, pre);
  msg.append(details);
  transcript.append(msg);
  reveal();
  const tool = { msg, details, label, chars, pre, filled: false };
  state.tool = tool;
  state.block = null;
  autoScroll();
  return tool;
}

function fillTool(name, text, chars) {
  let tool = state.tool;
  if (!tool || tool.filled) tool = addTool(name);
  tool.filled = true;
  if (name) tool.label.textContent = name;
  tool.chars.textContent = `${Number(chars || 0).toLocaleString()} chars`;
  tool.pre.textContent = text;
  tool.pre.hidden = false;
  autoScroll();
}

function addError(text) {
  const msg = makeBlock('error', 'error');
  msg.body.textContent = text;
  transcript.append(msg);
  reveal();
  state.block = null;
  autoScroll();
}

function clearTranscript() {
  transcript.textContent = '';
  transcript.append(transcriptEmpty);
  transcriptEmpty.hidden = false;
  state.block = null;
  state.tool = null;
  state.usage = null;
  renderMeter();
}

/* ----------------------------------------------------------------- events */

function handle(event) {
  state.lastEventAt = event.kind === 'done' ? null : Date.now();
  switch (event.kind) {
    case 'user':
      addUser(event.text || '');
      break;
    case 'start':
      state.failed = false;
      hideAsk();
      setRunning(true);
      break;
    case 'content':
      addContent(event.text || '');
      break;
    case 'reasoning':
      addReasoning(event.text || '');
      break;
    case 'tool_name':
      addTool(event.text || '');
      break;
    case 'tool_result':
      fillTool(event.name || '', event.text || '', event.chars || 0);
      break;
    case 'status':
    case 'notice':
      logActivity(event.kind, event.text || '');
      break;
    case 'confirm':
      showConfirm(event.text || '', event.auto === true);
      break;
    case 'confirm_done':
      hideConfirm();
      logActivity('approve', `${event.text || 'answered'}: ${state.confirmCommand}`);
      break;
    case 'ask':
      showAsk(event.questions || []);
      break;
    case 'ask_done':
      hideAsk();
      break;
    case 'todos':
      renderTodos(event.todos || []);
      break;
    case 'reset':
      clearTranscript();
      renderTodos([]);
      hideAsk();
      logActivity('reset', event.text || 'Conversation cleared.');
      break;
    case 'error':
      addError(event.text || 'the request failed');
      logActivity('error', event.text || 'the request failed');
      state.failed = true;
      toast(event.text || 'the request failed', 'bad');
      break;
    case 'done':
      setRunning(false);
      break;
    default:
      break;
  }
}

function apply(data) {
  if (data.usage) {
    state.usage = data.usage;
    renderMeter();
  }
  for (const event of data.events || []) handle(event);
  state.status = data.status || 'idle';
  state.seq = Math.max(state.seq, Number(data.seq) || 0);
  if (data.approval && confirmBar.hidden) showConfirm(data.approval, false);
  if (data.question && askBar.hidden) showAsk(data.question);
  renderStatusFromState();
}

async function poll() {
  pollTimer = null;
  try {
    const data = await fetchJson(`/api/events?after=${state.seq}`);
    apply(data);
  } catch (error) {
    setStatus('bad', 'offline');
  }
  pollTimer = setTimeout(poll, isLive() ? 200 : 900);
}

function pollNow() {
  if (pollTimer !== null) clearTimeout(pollTimer);
  pollTimer = null;
  poll();
}

function setRunning(running) {
  $('stop').hidden = !running;
  if (running) {
    if (!state.lastEventAt) state.lastEventAt = Date.now();
    renderStatusFromState();
  } else {
    state.lastEventAt = null;
    hideConfirm();
    hideAsk();
    renderStatusFromState();
    if (document.activeElement !== promptEl) promptEl.focus();
  }
}

function showConfirm(command, auto) {
  state.confirmCommand = command;
  if (auto) {
    confirmBar.hidden = true;
    logActivity('approve', `auto: ${command}`);
    return;
  }
  confirmCommand.textContent = command;
  confirmBar.hidden = false;
  setStatus('warn', 'approval');
  autoScroll();
}

function hideConfirm() {
  confirmBar.hidden = true;
}

/* The ask tool stops the request until this popup is answered. A pick is
   optional: an empty answer is the same as Dismiss, and both tell the model to
   decide nothing and wait. A typed answer is the user's own choice. */
function showAsk(questions) {
  state.questions = Array.isArray(questions) ? questions : [];
  if (state.questions.length === 0) {
    hideAsk();
    return;
  }
  const count = state.questions.length;
  askTitle.textContent = count > 1 ? 'A few decisions' : 'Foo AI asks';
  askHint.textContent = `${count} question${count > 1 ? 's' : ''}`;
  askBody.textContent = '';
  state.questions.forEach((question, index) => {
    const id = question.id || `q${index + 1}`;
    const multi = question.multiSelect === true;

    const block = document.createElement('div');
    block.className = 'ask-q';
    block.setAttribute('role', 'group');
    block.setAttribute('aria-labelledby', `ask-${id}-head`);

    const head = document.createElement('div');
    head.className = 'ask-head';
    const kind = document.createElement('span');
    kind.className = 'kind';
    kind.id = `ask-${id}-head`;
    kind.textContent = question.header || `Question ${index + 1}`;
    const pick = document.createElement('span');
    pick.className = 'pick';
    pick.textContent = multi ? 'pick any' : 'pick one';
    head.append(kind, pick);

    const prompt = document.createElement('p');
    prompt.className = 'ask-prompt';
    prompt.textContent = question.question || '';

    const options = document.createElement('div');
    options.className = 'ask-options';
    for (const option of question.options || []) {
      const label = document.createElement('label');
      label.className = 'ask-option';
      const input = document.createElement('input');
      input.type = multi ? 'checkbox' : 'radio';
      input.name = `ask-${id}`;
      input.value = option.label || '';
      const text = document.createElement('span');
      text.className = 'ask-label';
      text.textContent = option.label || '';
      label.append(input, text);
      if (option.description) {
        const note = document.createElement('small');
        note.className = 'ask-note';
        note.textContent = option.description;
        label.append(note);
      }
      options.append(label);
    }

    const custom = document.createElement('label');
    custom.className = 'ask-custom';
    const customLabel = document.createElement('span');
    customLabel.textContent = 'Or type your own answer';
    const customInput = document.createElement('input');
    customInput.type = 'text';
    customInput.className = 'ask-custom-input';
    customInput.name = `ask-custom-${id}`;
    customInput.autocomplete = 'off';
    customInput.spellcheck = false;
    custom.append(customLabel, customInput);

    block.append(head, prompt, options, custom);
    askBody.append(block);
  });
  askBar.hidden = false;
  setStatus('warn', 'question');
  const first = askBody.querySelector('input');
  if (first) first.focus();
}

function hideAsk() {
  if (askBar.hidden) return;
  askBar.hidden = true;
  askBody.textContent = '';
  state.questions = [];
}

function renderTodos(items) {
  const list = Array.isArray(items) ? items : [];
  todoList.textContent = '';
  if (list.length === 0) {
    todoBox.hidden = true;
    todoProgress.textContent = '';
    todoFill.style.width = '0%';
    todoMeter.classList.remove('done');
    return;
  }
  let completed = 0;
  let active = 0;
  for (const item of list) {
    const status = item.status === 'completed' || item.status === 'in_progress' ? item.status : 'pending';
    if (status === 'completed') completed += 1;
    if (status === 'in_progress') active += 1;
    const row = document.createElement('li');
    row.className = `todo ${status}`;
    const mark = document.createElement('span');
    mark.className = 'mark';
    mark.textContent = status === 'completed' ? 'x' : status === 'in_progress' ? '>' : '\u00b7';
    const text = document.createElement('span');
    text.className = 'text';
    text.textContent = item.content || '';
    row.title = status === 'completed' ? 'completed' : status === 'in_progress' ? 'in progress' : 'pending';
    row.append(mark, text);
    todoList.append(row);
  }
  const total = list.length;
  const parts = [`${completed}/${total} done`];
  if (active > 0) parts.push(`${active} active`);
  todoProgress.textContent = parts.join(' \u00b7 ');
  todoFill.style.width = `${Math.round((completed / total) * 100)}%`;
  todoMeter.classList.toggle('done', completed === total);
  todoBox.hidden = false;
}

async function submitAsk(dismiss) {
  const answers = [];
  if (!dismiss) {
    state.questions.forEach((question, index) => {
      const id = question.id || `q${index + 1}`;
      let picked = [...askBody.querySelectorAll(`input[name="ask-${id}"]:checked`)].map((input) => input.value);
      const custom = askBody.querySelector(`input[name="ask-custom-${id}"]`);
      const typed = custom ? custom.value.trim() : '';
      if (typed) {
        picked = question.multiSelect === true ? [...picked, typed] : [typed];
      }
      answers.push({ id, selected: picked });
    });
  }
  const summary = answers
    .filter((item) => item.selected.length > 0)
    .map((item) => item.selected.join(', '))
    .join(' \u00b7 ');
  hideAsk();
  logActivity('ask', summary === '' ? 'question dismissed' : `answered ${summary}`);
  try {
    const result = await postJson('/api/answer', { answers });
    if (!result.ok) toast(result.error || 'no question is waiting', 'bad');
  } catch (error) {
    toast(String(error.message || error), 'bad');
  }
  pollNow();
}

/* --------------------------------------------------------------- commands */

async function send() {
  const text = promptEl.value.trim();
  if (text === '') return;
  if (state.status !== 'idle') {
    toast('a request is already running \u2014 press Stop to interrupt it', 'bad');
    return;
  }
  const slash = text.split(/\s+/, 1)[0];
  const skill = slash.startsWith('/') ? slash.slice(1) : '';
  promptEl.value = '';
  state.failed = false;
  try {
    let result;
    if (text === '/reset') {
      result = await postJson('/api/reset', {});
    } else if (text === '/exit' || text === '/quit') {
      window.close();
      return;
    } else if (skill && state.skills.includes(skill)) {
      result = await postJson('/api/slash', { command: text });
    } else {
      result = await postJson('/api/ask', { text });
    }
    if (!result.ok) {
      toast(result.error || 'the request was refused', 'bad');
      if (text !== '/reset' && promptEl.value === '') promptEl.value = text;
    } else if (text === '/reset') {
      clearTranscript();
      toast(result.note || 'Conversation cleared.', 'ok');
    }
  } catch (error) {
    toast(String(error.message || error), 'bad');
    promptEl.value = text;
  }
  pollNow();
}

async function stop() {
  try {
    const result = await postJson('/api/cancel', {});
    if (!result.ok) toast(result.error || 'nothing is running', 'bad');
  } catch (error) {
    toast(String(error.message || error), 'bad');
  }
  pollNow();
}

/* The desktop window's menu item. The page itself starts a new conversation
   with /reset in the composer, so this posts the reset directly and never
   overwrites a draft the composer is holding. */
async function resetConversation() {
  try {
    const result = await postJson('/api/reset', {});
    if (!result.ok) throw new Error(result.error || 'the conversation was not reset');
    clearTranscript();
    toast(result.note || 'Conversation cleared.', 'ok');
  } catch (error) {
    toast(String(error.message || error), 'bad');
  }
  pollNow();
}

async function answer(approve) {
  hideConfirm();
  try {
    const result = await postJson('/api/approve', { approve: approve === true });
    if (!result.ok) toast(result.error || 'no command is waiting', 'bad');
  } catch (error) {
    toast(String(error.message || error), 'bad');
  }
  pollNow();
}

async function changeProvider(name) {
  if (!name || name === state.providerName) return;
  setBusy(true, `reading ${name}…`);
  try {
    const result = await postJson('/api/provider', { name });
    if (!result.ok) throw new Error(result.error || 'the provider was refused');
    await loadState();
    toast(`Provider ${name}.`, 'ok');
  } catch (error) {
    toast(String(error.message || error), 'bad');
    await loadState();
  } finally {
    setBusy(false);
  }
}

async function changeWorkspace(path) {
  if (!path) return;
  if (path === state.workspace) {
    toast('That folder is already open.', 'ok');
    return;
  }
  setBusy(true, 'reading the workspace…');
  try {
    const result = await postJson('/api/workspace', { path });
    if (!result.ok) throw new Error(result.error || 'the workspace was refused');
    hideWorkspacePicker();
    clearTranscript();
    await loadState();
    await loadHistory();
    toast(`Workspace ${path}`, 'ok');
  } catch (error) {
    toast(String(error.message || error), 'bad');
  } finally {
    setBusy(false);
  }
}

/* ------------------------------------------------------- workspace picker */

/* One folder walker under the Open row. It only fills the field: Open commits
   it, so a mis-click in the list cannot move the session to another folder. */
function hideWorkspacePicker() {
  const picker = $('workspace-picker');
  picker.hidden = true;
  picker.textContent = '';
  $('workspace-browse').setAttribute('aria-expanded', 'false');
}

async function browseWorkspace(path) {
  try {
    const query = path ? `?${new URLSearchParams({ path })}` : '';
    const listing = await fetchJson(`/api/browse${query}`);
    if (!listing.ok) throw new Error(listing.error || 'the folder could not be read');
    renderWorkspacePicker(listing);
  } catch (error) {
    toast(String(error.message || error), 'bad');
  }
}

/* The head names the folder it lists. A long path keeps its tail, because the
   last folder is what tells two listings apart, and the pane is narrow. */
function pathLabel(path, limit = 34) {
  return path.length <= limit ? path : '…' + path.slice(1 - limit);
}

function renderWorkspacePicker(listing) {
  const picker = $('workspace-picker');
  picker.textContent = '';
  const head = document.createElement('div');
  head.className = 'picker-head';
  if (listing.parent) {
    const up = document.createElement('button');
    up.type = 'button';
    up.className = 'icon';
    up.dataset.dir = listing.parent;
    up.title = 'Up one folder';
    up.setAttribute('aria-label', 'Up one folder');
    up.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 12V4M4 8l4-4 4 4"/></svg>';
    head.append(up);
  }
  const where = document.createElement('span');
  where.className = 'where';
  where.textContent = pathLabel(listing.path);
  where.title = listing.path;
  head.append(where);

  const list = document.createElement('div');
  list.className = 'picker-list';
  const entries = Array.isArray(listing.entries) ? listing.entries : [];
  if (entries.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'picker-empty';
    empty.textContent = 'No folders here.';
    list.append(empty);
  }
  for (const entry of entries) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'picker-entry';
    button.dataset.dir = entry.path;
    button.title = entry.path;
    const kind = document.createElement('span');
    kind.className = 'kind';
    kind.textContent = '\u25b8';
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = `${entry.name}/`;
    button.append(kind, name);
    list.append(button);
  }

  const foot = document.createElement('div');
  foot.className = 'picker-foot';
  const use = document.createElement('button');
  use.type = 'button';
  use.className = 'ghost';
  use.dataset.use = listing.path;
  use.textContent = 'Use this folder';
  foot.append(use);

  picker.append(head, list, foot);
  picker.hidden = false;
  $('workspace-browse').setAttribute('aria-expanded', 'true');
}

/* ------------------------------------------------------------------ state */

function folderName(path) {
  const trimmed = String(path || '').replace(/[\\/]+$/, '');
  /* A root is all separator: "/" trims to nothing, and the head still has to
     name it, so the path itself is the name there. */
  if (!trimmed) return String(path || '');
  const cut = Math.max(trimmed.lastIndexOf('/'), trimmed.lastIndexOf('\\'));
  return cut === -1 ? trimmed : trimmed.slice(cut + 1);
}

function fact(rows, label, value, title) {
  const row = document.createElement('div');
  row.className = 'fact';
  const key = document.createElement('span');
  key.textContent = label;
  const val = document.createElement('b');
  val.textContent = value;
  if (title) val.title = title;
  row.append(key, val);
  rows.append(row);
}

function applyState(data) {
  state.workspace = data.workspace || '';
  state.providers = data.providers || [];
  state.provider = data.provider || null;
  state.providerName = data.provider ? data.provider.name : '';
  state.settingsFile = data.settings_file || '';
  state.maxSteps = data.max_steps;
  state.skills = (data.skills || []).map((skill) => skill.name);
  state.thinking = data.thinking === true;
  state.verbose = data.verbose === true;
  state.yes = data.yes === true;
  state.status = data.status || 'idle';
  if (Array.isArray(data.todos)) renderTodos(data.todos);

  $('show-thinking').checked = state.thinking;
  $('verbose').checked = state.verbose;
  $('auto-approve').checked = state.yes;
  if (settingsLayer.hidden) fillProviderForm();

  const pathInput = $('workspace-path');
  if (document.activeElement !== pathInput) pathInput.value = state.workspace;

  const where = $('where');
  where.textContent = '';
  const folder = document.createElement('span');
  folder.className = 'mono';
  folder.textContent = folderName(state.workspace) || 'no workspace';
  if (state.workspace) folder.title = state.workspace;
  where.append(folder);
  if (data.provider) {
    where.append(document.createTextNode(` · ${data.provider.name} · ${data.provider.model}`));
  }
  where.title = [state.workspace, data.provider && data.provider.name, data.provider && data.provider.model]
    .filter(Boolean)
    .join(' · ');

  const providerFacts = $('provider-facts');
  providerFacts.textContent = '';
  if (data.provider) {
    fact(providerFacts, 'window', String(data.provider.context_window), 'tokens');
    fact(providerFacts, 'url', data.provider.base_url, data.provider.base_url);
  }

  const select = $('provider-select');
  select.textContent = '';
  const names = state.providers.length ? state.providers : [state.providerName].filter(Boolean);
  for (const name of names) {
    const option = document.createElement('option');
    option.value = name;
    option.textContent = name;
    select.append(option);
  }
  select.value = state.providerName;

  const skills = $('skills');
  skills.textContent = '';
  if ((data.skills || []).length === 0) {
    const none = document.createElement('p');
    none.className = 'muted';
    none.textContent = 'No skills loaded.';
    skills.append(none);
  }
  for (const skill of data.skills || []) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'skill';
    button.title = skill.description;
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = skill.name;
    const run = document.createElement('span');
    run.className = 'run';
    run.textContent = skill.run;
    const what = document.createElement('span');
    what.className = 'what';
    what.textContent = skill.description;
    button.append(name, run, what);
    button.addEventListener('click', () => {
      promptEl.value = `/${skill.name} `;
      promptEl.focus();
    });
    skills.append(button);
  }

  const sessionFacts = $('session-facts');
  sessionFacts.textContent = '';
  fact(sessionFacts, 'file', data.session.file);
  fact(sessionFacts, 'messages', String(data.session.messages));
  fact(sessionFacts, 'steps', String(state.maxSteps === null ? '–' : state.maxSteps));
  $('session-hint').textContent = data.note ? 'resumed' : '';

  const mcpPane = $('mcp-pane');
  const mcpFacts = $('mcp-facts');
  mcpFacts.textContent = '';
  const lines = data.mcp || [];
  mcpPane.hidden = lines.length === 0;
  for (const line of lines) {
    const row = document.createElement('div');
    row.className = 'fact';
    const value = document.createElement('b');
    value.textContent = line;
    row.append(value);
    mcpFacts.append(row);
  }

  renderMeter();
  renderStatusFromState();
}

async function loadState() {
  applyState(await fetchJson('/api/state'));
}

async function loadHistory() {
  const data = await fetchJson('/api/history');
  clearTranscript();
  const messages = data.messages || [];
  for (const item of messages) {
    if (item.role === 'user') {
      addUser(item.text || '');
    } else if (item.role === 'assistant') {
      const block = assistantBlock();
      if (item.reasoning) {
        const details = document.createElement('details');
        details.className = 'reasoning';
        const summary = document.createElement('summary');
        summary.textContent = 'thinking';
        const body = document.createElement('div');
        body.className = 'reasoning-body';
        body.textContent = item.reasoning;
        details.append(summary, body);
        block.append(details);
        block.reasoning = body;
      }
      block.text = item.text || '';
      block.body.innerHTML = renderMarkdown(block.text);
      if (block.text === '' && !item.reasoning) {
        const note = document.createElement('p');
        note.className = 'muted';
        note.textContent = item.tools && item.tools.length
          ? `called ${item.tools.join(', ')}`
          : '(empty)';
        block.body.append(note);
      }
      state.block = null;
    } else if (item.role === 'tool') {
      addTool(item.name || 'tool');
      fillTool(item.name || 'tool', item.text || '', (item.text || '').length);
    }
  }
  state.block = null;
  state.tool = null;
  if (messages.length > 0) reveal();
  autoScroll();
}

/* -------------------------------------------------------------- listeners */

/* A pane collapses by giving its grid column no width, so the stage keeps the
   room and the other pane stays where it is. The button reports the state and
   dims while its pane is off. */
function togglePane(side) {
  const body = $('body');
  const off = body.classList.toggle(`${side}-off`);
  $(`toggle-${side}`).setAttribute('aria-pressed', String(!off));
}

/* The sheet shows the provider the page is on; Save writes it to the settings
   file and switches to it. The key is write-only: the page never reads it.
   The local reasoning block is shown only for a provider that sends one, so a
   cloud provider has no fields for a body it never carries. */
function reasoningBlock() {
  const provider = state.provider;
  const block = provider && provider.chat_template_kwargs;
  return block && typeof block === 'object' ? block : null;
}

function fillProviderForm() {
  const provider = state.provider;
  settingsFile.textContent = state.settingsFile || '–';
  settingsFile.title = state.settingsFile || '';
  settingsProvider.value = provider ? provider.name : '';
  settingsBaseUrl.value = provider ? provider.base_url : '';
  settingsModel.value = provider ? provider.model : '';
  settingsWindow.value = provider ? String(provider.context_window) : '';
  settingsKey.value = '';
  settingsKeyClear.checked = false;
  settingsKey.placeholder = provider && provider.api_key_set
    ? 'set · leave blank to keep it'
    : 'no key';
  const block = reasoningBlock();
  settingsReasoning.hidden = block === null;
  settingsEffort.value = block && typeof block.reasoning_effort === 'string'
    ? block.reasoning_effort
    : '';
  settingsThinkingParam.checked = block !== null && block.thinking === true;
}

function openSettings() {
  fillProviderForm();
  settingsLayer.hidden = false;
  settingsBaseUrl.focus();
  settingsBaseUrl.select();
}

function closeSettings() {
  settingsLayer.hidden = true;
}

async function saveProviderSettings() {
  if (!state.providerName) {
    toast('No provider to save.', 'bad');
    return;
  }
  const body = {
    name: state.providerName,
    base_url: settingsBaseUrl.value.trim(),
    model: settingsModel.value.trim(),
    context_window: settingsWindow.value.trim(),
  };
  if (!settingsReasoning.hidden) {
    body.chat_template_kwargs = {
      reasoning_effort: settingsEffort.value.trim(),
      thinking: settingsThinkingParam.checked,
    };
  }
  if (settingsKeyClear.checked) body.api_key = '';
  else if (settingsKey.value !== '') body.api_key = settingsKey.value;
  setBusy(true, 'saving the provider…');
  try {
    const saved = await postJson('/api/provider/save', body);
    if (!saved.ok) throw new Error(saved.error || 'the provider was refused');
    await loadState();
    closeSettings();
    toast(`Provider ${saved.provider.name} saved.`, 'ok');
  } catch (error) {
    toast(String(error.message || error), 'bad');
  } finally {
    setBusy(false);
  }
}

async function toggleSetting(field, value) {
  try {
    const saved = await postJson('/api/settings', { [field]: value });
    if (!saved.ok) throw new Error(saved.error || 'the setting was refused');
    state.thinking = saved.thinking === true;
    state.verbose = saved.verbose === true;
    state.yes = saved.yes === true;
  } catch (error) {
    toast(String(error.message || error), 'bad');
    await loadState();
  }
}

function wire() {
  $('composer').addEventListener('submit', (event) => {
    event.preventDefault();
    send();
  });
  promptEl.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send();
    }
  });
  promptEl.addEventListener('input', () => {
    promptEl.style.height = 'auto';
    promptEl.style.height = `${Math.min(180, promptEl.scrollHeight)}px`;
  });

  $('stop').addEventListener('click', stop);
  $('confirm-allow').addEventListener('click', () => answer(true));
  $('confirm-deny').addEventListener('click', () => answer(false));
  $('ask-form').addEventListener('submit', (event) => {
    event.preventDefault();
    submitAsk(false);
  });
  $('ask-dismiss').addEventListener('click', () => submitAsk(true));
  $('ask-close').addEventListener('click', () => submitAsk(true));
  $('ask-scrim').addEventListener('click', () => submitAsk(true));

  $('toggle-left').addEventListener('click', () => togglePane('left'));
  $('toggle-right').addEventListener('click', () => togglePane('right'));

  $('show-thinking').addEventListener('change', (event) => toggleSetting('thinking', event.target.checked));
  $('verbose').addEventListener('change', (event) => toggleSetting('verbose', event.target.checked));
  $('auto-approve').addEventListener('change', (event) => toggleSetting('yes', event.target.checked));

  $('provider-select').addEventListener('change', (event) => changeProvider(event.target.value));

  $('workspace-form').addEventListener('submit', (event) => {
    event.preventDefault();
    changeWorkspace($('workspace-path').value.trim());
  });

  $('workspace-browse').addEventListener('click', () => {
    if (!$('workspace-picker').hidden) {
      hideWorkspacePicker();
      return;
    }
    browseWorkspace($('workspace-path').value.trim() || null);
  });

  $('workspace-picker').addEventListener('click', (event) => {
    const folder = event.target.closest('[data-dir]');
    const use = event.target.closest('[data-use]');
    if (folder) browseWorkspace(folder.dataset.dir);
    else if (use) {
      $('workspace-path').value = use.dataset.use;
      hideWorkspacePicker();
      $('workspace-path').focus();
    }
  });

  $('settings-open').addEventListener('click', openSettings);
  $('settings-close').addEventListener('click', closeSettings);
  $('settings-cancel').addEventListener('click', closeSettings);
  $('settings-scrim').addEventListener('click', closeSettings);
  $('settings-form').addEventListener('submit', (event) => event.preventDefault());
  $('settings-provider-save').addEventListener('click', saveProviderSettings);
  /* Enter in a provider field saves that provider. */
  for (const field of [settingsBaseUrl, settingsModel, settingsWindow, settingsKey, settingsEffort]) {
    field.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.isComposing) {
        event.preventDefault();
        saveProviderSettings();
      }
    });
  }

  transcript.addEventListener('scroll', () => {
    const slack = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight;
    state.autoScroll = slack < 48;
  });

  document.addEventListener('keydown', (event) => {
    const target = event.target;
    const typing = target instanceof HTMLElement && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName);
    if (event.key === 'Escape') {
      if (!askBar.hidden) submitAsk(true);
      else if (!settingsLayer.hidden) closeSettings();
      else if (!$('workspace-picker').hidden) hideWorkspacePicker();
      return;
    }
    if (typing) return;
    if (event.key === ',' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      openSettings();
      return;
    }
    if (event.key === '[') togglePane('left');
    if (event.key === ']') togglePane('right');
  });

  /* The desktop shell's menu reaches the page through the preload bridge. */
  const desktop = window.fooAiDesktop;
  if (desktop) {
    if (typeof desktop.onSettings === 'function') desktop.onSettings(openSettings);
    if (typeof desktop.onReset === 'function') desktop.onReset(resetConversation);
    if (typeof desktop.onWorkspace === 'function') {
      desktop.onWorkspace((directory) => changeWorkspace(directory));
    }
  }
}

async function boot() {
  wire();
  setInterval(() => {
    if (state.status === 'running') renderStatusFromState();
  }, 1000);
  try {
    await loadState();
    await loadHistory();
    /* The transcript on disk is already drawn, so the log replays only the
       lines that belong to the activity pane. A shell command or a question
       that still waits is raised from the snapshot, not from an old event. */
    const snapshot = await fetchJson('/api/events?after=0');
    for (const event of snapshot.events || []) {
      if (event.kind === 'notice' || event.kind === 'status') handle(event);
    }
    state.seq = Number(snapshot.seq) || 0;
    if (snapshot.usage) {
      state.usage = snapshot.usage;
      renderMeter();
    }
    if (snapshot.approval) showConfirm(snapshot.approval, false);
    if (snapshot.question && typeof showAsk === 'function') showAsk(snapshot.question);
  } catch (error) {
    toast(String(error.message || error), 'bad');
    setStatus('bad', 'offline');
  }
  setRunning(isLive());
  promptEl.focus();
  poll();
}

boot();
