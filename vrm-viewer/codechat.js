// codechat.js — runs in the code-chat BrowserWindow's renderer.
// Talks to Ollama through window.ollamaAPI, which proxies the request
// through the Electron main process (see main.js). This avoids the
// CORS block you'd hit calling Ollama's HTTP API directly from a
// renderer (Ollama only allows origins listed in OLLAMA_ORIGINS).
// Uses window.codeChatAPI (exposed by preload.cjs) for file I/O.

const messagesEl = document.getElementById('messages');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send-btn');
const openFileBtn = document.getElementById('open-file-btn');
const saveFileBtn = document.getElementById('save-file-btn');
const clearBtn = document.getElementById('clear-btn');
const fileInfoEl = document.getElementById('file-info');
const activeFileNameEl = document.getElementById('active-file-name');
const statusDot = document.getElementById('status-dot');
const statusText = document.getElementById('status-text');
const ollamaUrlInput = document.getElementById('ollama-url-input');
const modelInput = document.getElementById('model-input');
const attachBtn = document.getElementById('attach-btn');
const fileAttachInput = document.getElementById('file-attach-input');
const attachmentTray = document.getElementById('attachment-tray');

// conversation history sent to Ollama on every request
const CODING_SYSTEM_PROMPT =
  'You are a focused local coding assistant running inside a desktop app. ' +
  'Prioritize working code over lengthy explanation. When asked for code, ' +
  'output it in a single fenced code block ready to copy or save. Keep prose ' +
  'brief — a short explanation before or after the block is fine, but avoid ' +
  'padding. Ask a clarifying question only when the request is genuinely ' +
  'ambiguous; otherwise make a reasonable assumption and proceed.';

let history = [{ role: 'system', content: CODING_SYSTEM_PROMPT }];
let currentFile = null; // { filePath, content }
let lastAssistantCodeBlock = null; // most recent code block text, for quick "Save"
// ── Live link to Lily's own bot process (ws_animation_server.py),
// the same WebSocket server lily-viewer.js already connects to. Lets
// Lily push her own code suggestions directly into this window's
// message stream, visually marked so it's obviously not the local
// Ollama assistant talking. ──
let lilyWs = null;
function connectLilyWS() {
  try {
    lilyWs = new WebSocket('ws://127.0.0.1:8765');
    lilyWs.onopen = () => console.log('[codechat] Connected to Lily WS');
    lilyWs.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === 'lily_code') {
          addLilyMessage(msg.code, msg.explanation);
        }
      } catch (err) {
        console.warn('[codechat] Lily WS parse error', err);
      }
    };
    lilyWs.onclose = () => setTimeout(connectLilyWS, 3000);
    lilyWs.onerror = () => { try { lilyWs.close(); } catch (_) { } };
  } catch (e) {
    setTimeout(connectLilyWS, 3000);
  }
}
connectLilyWS();

function addLilyMessage(code, explanation) {
  const bubble = document.createElement('div');
  bubble.className = 'msg lily';

  const header = document.createElement('div');
  header.className = 'lily-bubble-header';
  header.innerHTML = '<span class="lily-dot"></span><span>Lily</span>';
  bubble.appendChild(header);

  if (explanation && explanation.includes('```')) {
    renderContentIntoBubble(bubble, explanation, true);
  } else if (code) {
    bubble.appendChild(buildCodeBlock(code, true, null));
  }

  messagesEl.appendChild(bubble);
  messagesEl.scrollTop = messagesEl.scrollHeight;

  // Also add it to `history` as an assistant turn so if the user then
  // asks the local Ollama model to "improve what Lily wrote," the
  // model actually has that code in context instead of being blind to it.
  history.push({ role: 'assistant', content: explanation || `\`\`\`\n${code}\n\`\`\`` });

  // Let "Save Current Code" in the sidebar pick up her code too.
  if (code) lastAssistantCodeBlock = code;
}

function addSystemMessage(text) {
  const div = document.createElement('div');
  div.className = 'msg system';
  div.textContent = text;
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// ── Logs one exchange to the shared code-context file on disk (via
// main.js's code-chat:log-context handler) so lily_bot.py can read
// recent coding-assistant activity and answer questions about it. ──
function logCodeContextEntry({ userText, codeText, filePath }) {
  if (!window.codeChatAPI?.logContext) return;
  window.codeChatAPI.logContext({
    userRequest: (userText || '').slice(0, 300),
    code: (codeText || '').slice(0, 1500),
    filePath: filePath || null,
  }).catch((e) => console.warn('[logCodeContext] failed', e));
}

// ── Renders assistant text, splitting out ``` fenced code blocks.
// When `interactive` is true (final render, stream finished) each code
// block gets a Copy button AND an Edit button that turns the block into
// an editable textarea in place, with Save/Cancel. During streaming we
// pass interactive=false so an in-progress block can't be edited out
// from under itself as new chunks arrive.
//
// The language token captured by the split regex (e.g. "python" in
// ```python) is now threaded through to buildCodeBlock() so it can be
// rendered as a label — previously it was captured into `parts[i+1]`
// and simply discarded, which is why code blocks never showed a
// language tag despite the model reliably fencing with one. ──
function renderContentIntoBubble(bubbleEl, text, interactive = false) {
  bubbleEl.innerHTML = '';
  const parts = text.split(/```(\w*)\n?/);
  // parts alternates: [plainText, lang, code, plainText, lang, code, ...]
  let i = 0;
  while (i < parts.length) {
    const plain = parts[i];
    if (plain) {
      const span = document.createElement('span');
      span.textContent = plain;
      bubbleEl.appendChild(span);
    }
    const lang = parts[i + 1];
    const code = parts[i + 2];
    if (code !== undefined) {
      const codeText = code.replace(/\n$/, '');
      const wrap = buildCodeBlock(codeText, interactive, lang);
      bubbleEl.appendChild(wrap);
      lastAssistantCodeBlock = codeText;
    }
    i += 3;
  }
}

// ── Robust clipboard copy. navigator.clipboard.writeText() can
// silently fail (rejected promise, nothing shown to the user) in
// Electron renderers loaded from file://, since Chromium sometimes
// treats that as an insecure context and blocks the async Clipboard
// API. This tries the modern API first, and if it's unavailable or
// throws/rejects, falls back to the old hidden-textarea +
// document.execCommand('copy') trick, which works regardless of
// secure-context status. ──
async function copyTextToClipboard(text, btn) {
  let ok = false;

  if (navigator.clipboard && window.isSecureContext !== false) {
    try {
      await navigator.clipboard.writeText(text);
      ok = true;
    } catch (err) {
      console.warn('[copy] navigator.clipboard failed, falling back', err);
    }
  }

  if (!ok) {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      // Keep it out of view but still selectable/focusable.
      ta.style.position = 'fixed';
      ta.style.top = '-9999px';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      ok = document.execCommand('copy');
      document.body.removeChild(ta);
    } catch (err) {
      console.error('[copy] execCommand fallback failed', err);
      ok = false;
    }
  }

  if (btn) {
    btn.textContent = ok ? 'Copied!' : 'Copy failed';
    setTimeout(() => (btn.textContent = 'Copy'), 1200);
  }
  if (!ok) {
    addSystemMessage('⚠️ Copy to clipboard failed. You can still select the code text manually and press Ctrl/Cmd+C.');
  }
  return ok;
}

// `lang` is the fence-info-string token (e.g. "python", "js", or ""
// if the model fenced with bare ``` and no language). When present,
// it's rendered as a small label on the left side of the action bar,
// matching the "python" pill shown in the bug report screenshot.
function buildCodeBlock(codeText, interactive, lang) {
  const wrap = document.createElement('div');
  wrap.className = 'code-block';

  const pre = document.createElement('pre');
  const codeEl = document.createElement('code');
  codeEl.textContent = codeText;
  pre.appendChild(codeEl);

  const actions = document.createElement('div');
  actions.className = 'code-block-actions';

  if (lang) {
    const langLabel = document.createElement('span');
    langLabel.className = 'code-lang-label';
    langLabel.textContent = lang;
    actions.appendChild(langLabel);
  }

  const copyBtn = document.createElement('button');
  copyBtn.className = 'code-copy-btn';
  copyBtn.textContent = 'Copy';
  copyBtn.onclick = () => copyTextToClipboard(codeEl.textContent, copyBtn);
  actions.appendChild(copyBtn);

  if (interactive) {
    const editBtn = document.createElement('button');
    editBtn.className = 'code-copy-btn';
    editBtn.textContent = 'Edit';
    editBtn.onclick = () => enterEditMode(wrap, pre, codeEl, actions, lang);
    actions.appendChild(editBtn);
  }

  wrap.appendChild(actions);
  wrap.appendChild(pre);
  return wrap;
}

function enterEditMode(wrap, pre, codeEl, actions, lang) {
  const textarea = document.createElement('textarea');
  textarea.className = 'code-edit-textarea';
  textarea.value = codeEl.textContent;
  textarea.rows = Math.min(24, codeEl.textContent.split('\n').length + 1);
  textarea.spellcheck = false;

  const editActions = document.createElement('div');
  editActions.className = 'code-block-actions';

  if (lang) {
    const langLabel = document.createElement('span');
    langLabel.className = 'code-lang-label';
    langLabel.textContent = lang;
    editActions.appendChild(langLabel);
  }

  const saveBtn = document.createElement('button');
  saveBtn.className = 'code-copy-btn code-save-btn';
  saveBtn.textContent = 'Save';
  saveBtn.onclick = () => {
    codeEl.textContent = textarea.value;
    lastAssistantCodeBlock = textarea.value;
    wrap.replaceChild(pre, textarea);
    wrap.replaceChild(actions, editActions);
    addSystemMessage('Code block updated. Click "Save Current Code" in the sidebar to write it to disk.');
  };

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'code-copy-btn';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.onclick = () => {
    wrap.replaceChild(pre, textarea);
    wrap.replaceChild(actions, editActions);
  };

  editActions.appendChild(saveBtn);
  editActions.appendChild(cancelBtn);

  wrap.replaceChild(textarea, pre);
  wrap.replaceChild(editActions, actions);
  textarea.focus();
}

function addMessageBubble(role) {
  const div = document.createElement('div');
  div.className = `msg ${role}`;
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

async function checkOllamaStatus() {
  const result = await window.ollamaAPI.checkStatus(ollamaUrlInput.value);
  if (result?.ok) {
    statusDot.classList.add('ok');
    statusText.textContent = 'Ollama connected';
  } else {
    statusDot.classList.remove('ok');
    statusText.textContent = 'Ollama not reachable — is it running?';
  }
}

// ═══════════════════════════════════════════
// ATTACHMENTS — files & images, like a chat client
// ═══════════════════════════════════════════
const TEXT_EXTENSIONS = ['js', 'ts', 'py', 'json', 'html', 'css', 'md', 'txt', 'jsx', 'tsx', 'java', 'c', 'cpp', 'go', 'rs', 'yml', 'yaml', 'sh'];
// Known Ollama vision-capable model name fragments. Not exhaustive —
// just enough to warn the user before they attach an image to a
// model that will silently ignore it.
const VISION_MODEL_HINTS = ['llava', 'vision', 'bakllava', 'moondream', 'minicpm-v'];

let pendingAttachments = []; // [{ id, kind: 'text'|'image', name, content?, b64?, mime? }]

// ── Composer code blocks — code the USER pastes into the input gets
// the same Copy/Edit treatment as code the assistant writes, instead
// of sitting in the plain textarea as raw ``` fenced text. Each entry
// renders as its own block above the input; on send, all blocks are
// appended to the outgoing message as labeled fenced code. ──
let pendingCodeBlocks = []; // [{ id, code, lang }]
let composerCodePreview = null; // lazily-created container, inserted above the input row

function ensureComposerCodePreview() {
  if (composerCodePreview) return composerCodePreview;
  composerCodePreview = document.createElement('div');
  composerCodePreview.id = 'composer-code-preview';
  composerCodePreview.className = 'composer-code-preview';
  // Sits right above the attachment tray / input row, inside the composer.
  attachmentTray.parentNode.insertBefore(composerCodePreview, attachmentTray);
  return composerCodePreview;
}

function addComposerCodeBlock(code, lang) {
  const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  pendingCodeBlocks.push({ id, code, lang });
  renderComposerCodeBlocks();
}

function removeComposerCodeBlock(id) {
  pendingCodeBlocks = pendingCodeBlocks.filter((b) => b.id !== id);
  renderComposerCodeBlocks();
}

function renderComposerCodeBlocks() {
  const container = ensureComposerCodePreview();
  container.innerHTML = '';
  container.style.display = pendingCodeBlocks.length ? 'flex' : 'none';

  for (const block of pendingCodeBlocks) {
    const wrap = document.createElement('div');
    wrap.className = 'code-block composer-code-block';

    const pre = document.createElement('pre');
    const codeEl = document.createElement('code');
    codeEl.textContent = block.code;
    pre.appendChild(codeEl);

    const actions = document.createElement('div');
    actions.className = 'code-block-actions';

    if (block.lang) {
      const langLabel = document.createElement('span');
      langLabel.className = 'code-lang-label';
      langLabel.textContent = block.lang;
      actions.appendChild(langLabel);
    }

    const copyBtn = document.createElement('button');
    copyBtn.className = 'code-copy-btn';
    copyBtn.textContent = 'Copy';
    copyBtn.onclick = () => copyTextToClipboard(codeEl.textContent, copyBtn);
    actions.appendChild(copyBtn);

    const editBtn = document.createElement('button');
    editBtn.className = 'code-copy-btn';
    editBtn.textContent = 'Edit';
    editBtn.onclick = () => enterComposerEditMode(wrap, pre, codeEl, actions, block);
    actions.appendChild(editBtn);

    const removeBtn = document.createElement('button');
    removeBtn.className = 'code-copy-btn';
    removeBtn.textContent = 'Remove';
    removeBtn.onclick = () => removeComposerCodeBlock(block.id);
    actions.appendChild(removeBtn);

    wrap.appendChild(actions);
    wrap.appendChild(pre);
    container.appendChild(wrap);
  }
}

// Same in-place edit pattern as enterEditMode() for assistant code
// blocks, but writes back into the pendingCodeBlocks entry rather
// than into `lastAssistantCodeBlock`.
function enterComposerEditMode(wrap, pre, codeEl, actions, block) {
  const textarea = document.createElement('textarea');
  textarea.className = 'code-edit-textarea';
  textarea.value = codeEl.textContent;
  textarea.rows = Math.min(24, codeEl.textContent.split('\n').length + 1);
  textarea.spellcheck = false;

  const editActions = document.createElement('div');
  editActions.className = 'code-block-actions';

  if (block.lang) {
    const langLabel = document.createElement('span');
    langLabel.className = 'code-lang-label';
    langLabel.textContent = block.lang;
    editActions.appendChild(langLabel);
  }

  const saveBtn = document.createElement('button');
  saveBtn.className = 'code-copy-btn code-save-btn';
  saveBtn.textContent = 'Save';
  saveBtn.onclick = () => {
    codeEl.textContent = textarea.value;
    block.code = textarea.value;
    wrap.replaceChild(pre, textarea);
    wrap.replaceChild(actions, editActions);
  };

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'code-copy-btn';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.onclick = () => {
    wrap.replaceChild(pre, textarea);
    wrap.replaceChild(actions, editActions);
  };

  editActions.appendChild(saveBtn);
  editActions.appendChild(cancelBtn);

  wrap.replaceChild(textarea, pre);
  wrap.replaceChild(editActions, actions);
  textarea.focus();
}

function currentModelSupportsVision() {
  const model = (modelInput.value || '').toLowerCase();
  return VISION_MODEL_HINTS.some((hint) => model.includes(hint));
}

attachBtn.addEventListener('click', () => fileAttachInput.click());

fileAttachInput.addEventListener('change', async () => {
  for (const file of fileAttachInput.files) {
    await addAttachment(file);
  }
  fileAttachInput.value = '';
});

// Drag-and-drop onto the whole window
window.addEventListener('dragover', (e) => e.preventDefault());
window.addEventListener('drop', async (e) => {
  e.preventDefault();
  if (!e.dataTransfer?.files?.length) return;
  for (const file of e.dataTransfer.files) {
    await addAttachment(file);
  }
});

async function addAttachment(file) {
  const ext = file.name.split('.').pop().toLowerCase();
  const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;

  if (file.type.startsWith('image/')) {
    if (!currentModelSupportsVision()) {
      addSystemMessage(
        `⚠️ "${modelInput.value || 'this model'}" likely can't read images. ` +
        `Attaching "${file.name}" anyway, but consider switching to a vision model (e.g. "llava") to actually use it.`
      );
    }
    const b64 = await fileToBase64(file);
    pendingAttachments.push({ id, kind: 'image', name: file.name, b64, mime: file.type });
  } else if (TEXT_EXTENSIONS.includes(ext) || file.type.startsWith('text/')) {
    const content = await file.text();
    pendingAttachments.push({ id, kind: 'text', name: file.name, content });
  } else {
    addSystemMessage(`⚠️ Can't attach "${file.name}" — unsupported file type.`);
    return;
  }
  renderAttachmentTray();
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(',')[1]);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function removeAttachment(id) {
  pendingAttachments = pendingAttachments.filter((a) => a.id !== id);
  renderAttachmentTray();
}

function renderAttachmentTray() {
  attachmentTray.innerHTML = '';
  attachmentTray.style.display = pendingAttachments.length ? 'flex' : 'none';
  for (const att of pendingAttachments) {
    const chip = document.createElement('div');
    chip.className = 'attachment-chip';
    if (att.kind === 'image') {
      const thumb = document.createElement('img');
      thumb.src = `data:${att.mime};base64,${att.b64}`;
      chip.appendChild(thumb);
    } else {
      const icon = document.createElement('span');
      icon.textContent = '📄';
      chip.appendChild(icon);
    }
    const label = document.createElement('span');
    label.className = 'attachment-name';
    label.textContent = att.name;
    chip.appendChild(label);

    const removeBtn = document.createElement('button');
    removeBtn.textContent = '✕';
    removeBtn.className = 'attachment-remove';
    removeBtn.onclick = () => removeAttachment(att.id);
    chip.appendChild(removeBtn);

    attachmentTray.appendChild(chip);
  }
}

async function sendMessage() {
  const text = inputEl.value.trim();
  if (!text && pendingAttachments.length === 0 && pendingCodeBlocks.length === 0) return;

  inputEl.value = '';
  inputEl.style.height = 'auto';
  sendBtn.disabled = true;

  // Build the actual text sent to the model: user's text + any pasted
  // code blocks + any attached text files, each appended as labeled
  // fenced blocks. Images go in a separate `images` array on the
  // history entry (Ollama's format).
  let fullText = text;
  for (const block of pendingCodeBlocks) {
    fullText += `\n\n\`\`\`${block.lang || ''}\n${block.code}\n\`\`\`\n`;
  }
  const imageB64s = [];
  for (const att of pendingAttachments) {
    if (att.kind === 'text') {
      fullText += `\n\nAttached file: ${att.name}\n\`\`\`\n${att.content}\n\`\`\`\n`;
    } else if (att.kind === 'image') {
      imageB64s.push(att.b64);
    }
  }

  // Show what the user sent — text, pasted code blocks (still
  // Copy/Edit-able after send, for reference), and attachments.
  const userBubble = addMessageBubble('user');
  if (text) {
    const span = document.createElement('span');
    span.textContent = text;
    userBubble.appendChild(span);
  } else if (pendingCodeBlocks.length === 0) {
    userBubble.textContent = '(attachment)';
  }
  for (const block of pendingCodeBlocks) {
    userBubble.appendChild(buildCodeBlock(block.code, true, block.lang));
  }
  if (pendingAttachments.length) {
    const attSummary = document.createElement('div');
    attSummary.className = 'sent-attachments';
    attSummary.textContent = pendingAttachments.map((a) => `📎 ${a.name}`).join('  ');
    userBubble.appendChild(attSummary);
  }

  const historyEntry = { role: 'user', content: fullText };
  if (imageB64s.length) historyEntry.images = imageB64s; // Ollama vision models read this
  history.push(historyEntry);

  pendingAttachments = [];
  pendingCodeBlocks = [];
  renderAttachmentTray();
  renderComposerCodeBlocks();

  const aiBubble = addMessageBubble('ai');
  aiBubble.textContent = '…';

  let streamedText = '';
  let fullReply = '';

  try {
    fullReply = await window.ollamaAPI.sendChat(
      {
        url: ollamaUrlInput.value,
        model: modelInput.value.trim() || 'qwen2.5:7b',
        messages: history,
      },
      (chunk) => {
        streamedText += chunk;
        renderContentIntoBubble(aiBubble, streamedText, false);
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }
    );
  } catch (err) {
    renderContentIntoBubble(
      aiBubble,
      `⚠️ Error talking to Ollama: ${err.message}\n\nMake sure Ollama is running and the model name is correct (e.g. "ollama pull llama3").`
    );
    sendBtn.disabled = false;
    checkOllamaStatus();
    return;
  }

  renderContentIntoBubble(aiBubble, fullReply, true);
  history.push({ role: 'assistant', content: fullReply });

  logCodeContextEntry({
    userText: text,
    codeText: lastAssistantCodeBlock,
    filePath: currentFile ? currentFile.filePath : null,
  });

  sendBtn.disabled = false;
  checkOllamaStatus();
}

sendBtn.addEventListener('click', sendMessage);
inputEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});
inputEl.addEventListener('input', () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + 'px';
});

// ── Paste-to-code-block: pasting something that looks like code wraps
// it in ``` fences automatically, the way ChatGPT's composer does. ──
function looksLikeCode(text) {
  const lines = text.split('\n');
  if (lines.length < 3) return false; // short pastes stay plain text
  const codeSignals = /[{};]|=>|^\s*(function|const|let|var|import|export|def|class|if|for|while)\b|<\/?\w+>|^\s{2,}\S/m;
  return codeSignals.test(text);
}

// ── Rough language guess for pasted code, so it gets tagged with a
// label the same way model-generated code (which fences with its own
// language hint) already does. This is intentionally simple pattern
// matching, not a real detector — good enough to label the common
// cases and falls back to no label rather than guessing wrong. ──
function guessLanguage(text) {
  const checks = [
    { lang: 'python', re: /^\s*(def |class |import |from \S+ import|print\()|:\s*$/m },
    { lang: 'python', re: /^\s*#.*\n[\s\S]*def /m },
    { lang: 'javascript', re: /\b(const|let|var)\b.+=|=>|function\s*\(|require\(|console\.log\(/ },
    { lang: 'typescript', re: /:\s*(string|number|boolean|any)\b|interface\s+\w+|<\w+>/ },
    { lang: 'html', re: /<\/?[a-z][\s\S]*>/i },
    { lang: 'css', re: /[.#]?[\w-]+\s*{\s*[\w-]+\s*:\s*[^;]+;/ },
    { lang: 'json', re: /^\s*[{[][\s\S]*[}\]]\s*$/ },
    { lang: 'bash', re: /^\s*(#!\/bin\/(ba)?sh|\$\s|sudo |apt-get|npm |pip )/m },
    { lang: 'java', re: /\b(public|private)\s+(static\s+)?(class|void|int|String)\b/ },
    { lang: 'c', re: /#include\s*<\w+\.h>/ },
    { lang: 'cpp', re: /#include\s*<\w+>|std::/ },
    { lang: 'go', re: /^\s*package\s+\w+|:\=|\bfunc\s+\w+\(/m },
    { lang: 'rust', re: /\bfn\s+\w+\(|let mut\b|::<.*>/ },
  ];
  for (const { lang, re } of checks) {
    if (re.test(text)) return lang;
  }
  return '';
}

function wrapAsCodeBlock(text) {
  if (text.trim().startsWith('```')) return text;
  const lang = guessLanguage(text);
  return '```' + lang + '\n' + text.replace(/\n$/, '') + '\n```\n';
}

function insertAtCursor(el, text) {
  const start = el.selectionStart;
  const end = el.selectionEnd;
  const before = el.value.slice(0, start);
  const after = el.value.slice(end);
  el.value = before + text + after;
  const pos = start + text.length;
  el.selectionStart = el.selectionEnd = pos;
}

inputEl.addEventListener('paste', (e) => {
  // If the clipboard contains an image (e.g. screenshot copy-paste),
  // treat it as an attachment instead of text.
  const items = e.clipboardData?.items;
  if (items) {
    for (const item of items) {
      if (item.type.startsWith('image/')) {
        e.preventDefault();
        const file = item.getAsFile();
        if (file) addAttachment(file);
        return;
      }
    }
  }

  const text = e.clipboardData?.getData('text');
  if (!text || !looksLikeCode(text)) return; // let normal paste happen
  e.preventDefault();
  addComposerCodeBlock(text.replace(/\n$/, ''), guessLanguage(text));
});

clearBtn.addEventListener('click', () => {
  history = [{ role: 'system', content: CODING_SYSTEM_PROMPT }];
  messagesEl.innerHTML = '';
  pendingAttachments = [];
  renderAttachmentTray();
  addSystemMessage('Chat cleared.');
});

openFileBtn.addEventListener('click', async () => {
  const result = await window.codeChatAPI.openFile();
  if (!result) return;
  currentFile = result;
  fileInfoEl.textContent = result.filePath;
  activeFileNameEl.textContent = result.filePath.split(/[\\/]/).pop();
  addSystemMessage(`Opened ${result.filePath}`);
  inputEl.value = `Here's my current file (${result.filePath.split(/[\\/]/).pop()}):\n\n\`\`\`\n${result.content}\n\`\`\`\n\n`;
  inputEl.focus();
  inputEl.dispatchEvent(new Event('input'));
});

saveFileBtn.addEventListener('click', async () => {
  if (!lastAssistantCodeBlock) {
    addSystemMessage('No code from the assistant to save yet — ask it to write or edit some code first.');
    return;
  }
  const target = currentFile ? currentFile.filePath : null;
  const result = await window.codeChatAPI.saveFile(target, lastAssistantCodeBlock);
  if (result?.ok) {
    addSystemMessage(`Saved to ${result.filePath}`);
    if (currentFile) currentFile.content = lastAssistantCodeBlock;
  } else if (result && !result.ok && result.error) {
    addSystemMessage(`⚠️ Save failed: ${result.error}`);
  }
});

checkOllamaStatus();
addSystemMessage('Connected to your local AI. Paste code, ask questions, attach files/images, or open a file to get started.');