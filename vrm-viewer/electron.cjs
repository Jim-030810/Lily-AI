console.log("ELECTRON MAIN STARTED");

const { app, BrowserWindow, ipcMain, shell, dialog, globalShortcut } = require('electron');
const path = require('path');
const http = require('http');
const fs = require('fs/promises');
const { exec } = require('child_process');
console.log("IMPORTS OK");
let win;
let splashWindow;
let codeChatWindow; // ← separate ChatGPT-style coding assistant window

const FIXED_WIDTH = 500;
const FIXED_HEIGHT = 700;

// ── Vite readiness polling + retry-on-load helpers ──
function checkPortAlive(port) {
  return new Promise((resolve) => {
    const req = http.get(`http://127.0.0.1:${port}/`, (res) => {
      resolve(res.statusCode < 500);
      res.resume();
    });
    req.on('error', () => resolve(false));
    req.setTimeout(1000, () => { req.destroy(); resolve(false); });
  });
}

async function waitForVite(basePort, maxWaitMs = 60000) {
  const ports = [basePort, basePort + 1, basePort + 2];
  const start = Date.now();
  while (Date.now() - start < maxWaitMs) {
    for (const port of ports) {
      if (await checkPortAlive(port)) return port;
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  return null;
}

function loadMainWindowWithRetry(win, url, attempt = 1) {
  const MAX_ATTEMPTS = 5;
  win.loadURL(url).catch((err) => {
    console.error(`❌ Load attempt ${attempt} failed:`, err.message);
    if (attempt < MAX_ATTEMPTS) {
      setTimeout(() => loadMainWindowWithRetry(win, url, attempt + 1), 1000);
    } else {
      console.error('❌ Giving up after', MAX_ATTEMPTS, 'attempts');
    }
  });
}

function createSplash() {
  splashWindow = new BrowserWindow({
    width: 640,
    height: 420,
    frame: false,
    transparent: false,
    backgroundColor: '#0f1826',
    alwaysOnTop: true,
    resizable: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      sandbox: false,
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: true,
    },
  });

  const loadingPath = path.join(__dirname, 'lily_loading_screen.html');
  console.log('[splash] loading from:', loadingPath);
  console.log('[splash] file exists:', require('fs').existsSync(loadingPath));

  splashWindow.loadFile(loadingPath)
    .then(() => {
      console.log("✅ SPLASH LOADED");
    })
    .catch(err => {
      console.error("❌ SPLASH FAILED:", err);
    });

  splashWindow.webContents.on('did-finish-load', () => {
    console.log('✅ [splash] lily_loading.html loaded successfully');
  });

  splashWindow.webContents.on('console-message', (_e, level, message, line, sourceId) => {
    console.log(`[splash:${level}] ${message}\nSource: ${sourceId}:${line}`);
  });

  splashWindow.webContents.on('render-process-gone', (_e, details) => {
    console.error('❌ [splash] Renderer crashed:', details);
  });
}

// ── Code Chat window (ChatGPT-style coding assistant) ──
function createCodeChatWindow() {
  if (codeChatWindow && !codeChatWindow.isDestroyed()) {
    codeChatWindow.show();
    codeChatWindow.focus();
    return;
  }

  codeChatWindow = new BrowserWindow({
    width: 900,
    height: 700,
    minWidth: 640,
    minHeight: 480,
    resizable: true,
    frame: true,
    transparent: false,
    backgroundColor: '#1e1e1e',
    title: 'AI Coding Assistant',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  codeChatWindow.setMenu(null);
  codeChatWindow.loadFile(path.join(__dirname, 'codechat.html'));

  codeChatWindow.webContents.on('console-message', (_e, level, message, line, sourceId) => {
    console.log(`[codechat:${level}] ${message}\nSource: ${sourceId}:${line}`);
  });

  codeChatWindow.webContents.on('render-process-gone', (_e, details) => {
    console.error('❌ [codechat] Renderer crashed:', details);
  });

  codeChatWindow.on('closed', () => {
    codeChatWindow = null;
  });
}

app.on('second-instance', () => {
  if (win) {
    if (win.isMinimized()) win.restore();
    win.focus();
  }
});

app.commandLine.appendSwitch('enable-transparent-visuals');
// app.disableHardwareAcceleration() intentionally removed -- this was
// causing Chromium's network service to crash on load (ERR_FAILED),
// confirmed via electron_launch.log. Do not re-add this call.

app.whenReady().then(() => {
  console.log("APP READY");

  // ── Splash goes up first, immediately ──
  createSplash();

  // ── VRM viewer window ──
  win = new BrowserWindow({
    width: FIXED_WIDTH,
    height: FIXED_HEIGHT,

    show: false, // ← stays hidden until startup-complete

    transparent: true,
    backgroundColor: '#00000000',

    frame: false,
    alwaysOnTop: true,
    hasShadow: false,

    resizable: false,
    maximizable: false,
    fullscreenable: false,

    skipTaskbar: false,

    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),

      nodeIntegration: false,
      contextIsolation: true,

      backgroundThrottling: false,
      sandbox: false,
      devTools: true,
    }
  });

  // ── Poll for Vite in the background -- doesn't block the splash,
  // which is already up. Falls back to a production build if Vite
  // never comes up within the timeout. ──
  const basePort = parseInt(process.env.VRM_VITE_PORT || '5173', 10);

  (async () => {
    console.log(`Waiting for Vite on port ${basePort}...`);
    const readyPort = await waitForVite(basePort);
    const url = readyPort
      ? `http://127.0.0.1:${readyPort}`
      : `file://${path.join(__dirname, 'dist', 'index.html')}`;
    console.log('Loading:', url);
    loadMainWindowWithRetry(win, url);
  })();

  // Debug renderer console
  win.webContents.on(
    'console-message',
    (_event, level, message, line, sourceId) => {
      console.log(
        `[Renderer:${level}] ${message}\n` +
        `Source: ${sourceId}:${line}`
      );
    }
  );

  win.webContents.on('render-process-gone', (_event, details) => {
    console.error('❌ Renderer crashed:', details);
  });

  win.webContents.on('did-fail-load', (_event, code, desc, validatedURL) => {
    console.error('❌ Load failed:', code, desc, validatedURL);
  });

  win.webContents.on('did-start-navigation', (_event, url) => {
    console.log('➡ Navigating:', url);
  });

  win.webContents.on('did-finish-load', () => {
    console.log('✅ Renderer loaded');
  });

  win.setMenu(null);

  win.webContents.on('before-input-event', (event, input) => {
    if (input.type === 'keyDown' && input.key === 'F12') {
      win.webContents.toggleDevTools();
    }
  });

  win.setIgnoreMouseEvents(false);

  win.on('maximize', () => {
    win.unmaximize();
  });

  win.on('will-resize', (event) => {
    event.preventDefault();
  });

  win.on('resize', () => {
    const [w, h] = win.getSize();
    if (w !== FIXED_WIDTH || h !== FIXED_HEIGHT) {
      win.setSize(FIXED_WIDTH, FIXED_HEIGHT);
    }
  });

  ipcMain.on('mouse-hit-test', (event, isHit) => {
    if (win && !win.isDestroyed()) {
      win.setIgnoreMouseEvents(!isHit, { forward: true });
    }
    event.returnValue = true;
  });

  ipcMain.on('drag-window', (_event, { dx, dy }) => {
    if (!win || win.isDestroyed()) return;
    const bounds = win.getBounds();
    win.setBounds({
      x: bounds.x + dx,
      y: bounds.y + dy,
      width: FIXED_WIDTH,
      height: FIXED_HEIGHT
    });
  });

  // ── Open search-result links in the user's default browser.
  ipcMain.on('open-search-results', (_event, urls) => {
    if (!Array.isArray(urls)) return;
    for (const url of urls) {
      try {
        shell.openExternal(url);
      } catch (e) {
        console.error('❌ Failed to open URL:', url, e);
      }
    }
  });

  // ── Open the coding assistant window.
  ipcMain.on('open-code-chat', () => {
    createCodeChatWindow();
  });

  // Global shortcut fallback: Ctrl+Shift+C (Cmd+Shift+C on Mac) opens it
  // regardless of which window has focus.
  globalShortcut.register('CommandOrControl+Shift+C', () => {
    createCodeChatWindow();
  });

  win.on('closed', () => {
    win = null;
  });

});

// ── File open/save handlers used by the code chat window's
// "Open File" / "Save" buttons. ──
ipcMain.handle('code-chat:open-file', async () => {
  if (!codeChatWindow) return null;
  const result = await dialog.showOpenDialog(codeChatWindow, {
    properties: ['openFile'],
    filters: [
      { name: 'Code / Text', extensions: ['js', 'ts', 'py', 'json', 'html', 'css', 'md', 'txt', 'jsx', 'tsx', 'java', 'c', 'cpp', 'go', 'rs'] },
      { name: 'All Files', extensions: ['*'] },
    ],
  });
  if (result.canceled || result.filePaths.length === 0) return null;
  const filePath = result.filePaths[0];
  const content = await fs.readFile(filePath, 'utf-8');
  return { filePath, content };
});

ipcMain.handle('code-chat:save-file', async (_event, { filePath, content }) => {
  try {
    let targetPath = filePath;
    if (!targetPath) {
      const result = await dialog.showSaveDialog(codeChatWindow, {
        filters: [
          { name: 'All Files', extensions: ['*'] },
        ],
      });
      if (result.canceled || !result.filePath) return { ok: false };
      targetPath = result.filePath;
    }
    await fs.writeFile(targetPath, content, 'utf-8');
    return { ok: true, filePath: targetPath };
  } catch (e) {
    console.error('❌ Failed to save file:', e);
    return { ok: false, error: e.message };
  }
});

// ═══════════════════════════════════════════════════════════════
// OLLAMA PROXY — runs in the main (Node) process, NOT the renderer.
//
// Why this exists: Ollama's server enforces CORS via an OLLAMA_ORIGINS
// allow-list. A renderer's fetch() gets silently blocked as
// cross-origin (shows up as a generic "Failed to fetch" / "not
// reachable" in the UI) even when Ollama is running fine. Node's
// fetch in the main process is NOT subject to browser CORS, so
// proxying through here sidesteps the problem entirely -- no env
// vars for the user to set, no OLLAMA_ORIGINS config needed.
//
// Renderer talks to this via window.ollamaAPI (see preload.cjs).
// Status/model-list calls use ipcMain.handle (simple request/response).
// Chat is streamed, so it uses ipcMain.on + webContents.send events
// correlated by a requestId, since ipcMain.handle can't stream.
// ═══════════════════════════════════════════════════════════════

ipcMain.handle('ollama:check-status', async (_event, url) => {
  try {
    const base = (url || 'http://127.0.0.1:11434').replace(/\/$/, '');
    const res = await fetch(`${base}/api/tags`);
    return { ok: res.ok, status: res.status };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

ipcMain.handle('ollama:list-models', async (_event, url) => {
  try {
    const base = (url || 'http://127.0.0.1:11434').replace(/\/$/, '');
    const res = await fetch(`${base}/api/tags`);
    if (!res.ok) return { ok: false, error: `HTTP ${res.status}` };
    const data = await res.json();
    return { ok: true, models: (data.models || []).map(m => m.name) };
  } catch (e) {
    return { ok: false, error: e.message };
  }
});

ipcMain.on('ollama:chat:start', async (event, { requestId, url, model, messages }) => {
  const sender = event.sender;
  const send = (channel, payload) => {
    if (sender && !sender.isDestroyed()) sender.send(channel, { requestId, ...payload });
  };

  try {
    const base = (url || 'http://127.0.0.1:11434').replace(/\/$/, '');
    const res = await fetch(`${base}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: model || 'llama3',
        messages,
        stream: true,
      }),
    });

    if (!res.ok || !res.body) {
      throw new Error(`Ollama returned ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let fullReply = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const obj = JSON.parse(line);
          if (obj.message?.content) {
            fullReply += obj.message.content;
            send('ollama:chat:chunk', { chunk: obj.message.content });
          }
        } catch (_) {
          // ignore partial/malformed JSON line
        }
      }
    }

    send('ollama:chat:done', { fullReply });
  } catch (err) {
    console.error('❌ Ollama chat error:', err.message);
    send('ollama:chat:error', { error: err.message });
  }
});

// ── Shared coding-context log — lets lily_bot.py know what happened
// in this separate window, since it has its own independent Ollama
// chat history that Lily's normal conversation memory never sees. ──
const CODE_CONTEXT_FILE = path.join('E:', 'local_llm', 'LilyData', 'code_context.json');
const CODE_CONTEXT_MAX_ENTRIES = 20;

ipcMain.handle('code-chat:log-context', async (_event, entry) => {
  try {
    await fs.mkdir(path.dirname(CODE_CONTEXT_FILE), { recursive: true });

    let entries = [];
    try {
      const raw = await fs.readFile(CODE_CONTEXT_FILE, 'utf-8');
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) entries = parsed;
    } catch (_) {
      // file doesn't exist yet or is corrupt -- start fresh
    }

    entries.push({
      ts: Date.now(),
      userRequest: entry.userRequest || '',
      code: entry.code || '',
      filePath: entry.filePath || null,
    });

    if (entries.length > CODE_CONTEXT_MAX_ENTRIES) {
      entries = entries.slice(-CODE_CONTEXT_MAX_ENTRIES);
    }

    await fs.writeFile(CODE_CONTEXT_FILE, JSON.stringify(entries, null, 2), 'utf-8');
    return { ok: true };
  } catch (e) {
    console.error('❌ Failed to log code context:', e.message);
    return { ok: false, error: e.message };
  }
});

ipcMain.on('startup-complete', () => {
  if (win) win.show();
  if (splashWindow) {
    splashWindow.close();
    splashWindow = null;
  }
});

app.on('window-all-closed', () => {
  app.quit();
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    app.whenReady();
  }
});