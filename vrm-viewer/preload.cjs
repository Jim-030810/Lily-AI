const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
    notifyStartupComplete: () => ipcRenderer.send('startup-complete'),
    quitApp: () => ipcRenderer.send('quit-app'),
});
contextBridge.exposeInMainWorld('mascotAPI', {
    // sendSync, not send: blocks until the main process has actually
    // flipped setIgnoreMouseEvents, so the very next click is guaranteed
    // to reach the page instead of racing the IPC round-trip.
    setHit: (isHit) => ipcRenderer.sendSync('mouse-hit-test', isHit),
    dragBy: (dx, dy) => ipcRenderer.send('drag-window', { dx, dy }),
    // NEW: opens the AI coding assistant window (see main.js createCodeChatWindow()).
    openCodeChat: () => ipcRenderer.send('open-code-chat'),
});

// ── Ollama bridge — mirrors the requestId-correlated streaming
// pattern used by main.js's ollama:chat:start handler. ──
contextBridge.exposeInMainWorld('ollamaAPI', {
    checkStatus: (url) => ipcRenderer.invoke('ollama:check-status', url),
    listModels: (url) => ipcRenderer.invoke('ollama:list-models', url),

    // Returns a Promise<string> (the full reply), calling onChunk as
    // tokens stream in — matches how codechat.js already calls this.
    sendChat: ({ url, model, messages }, onChunk) => {
        const requestId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;

        return new Promise((resolve, reject) => {
            const onChunkEvt = (_e, data) => {
                if (data.requestId !== requestId) return;
                onChunk?.(data.chunk);
            };
            const onDone = (_e, data) => {
                if (data.requestId !== requestId) return;
                cleanup();
                resolve(data.fullReply);
            };
            const onError = (_e, data) => {
                if (data.requestId !== requestId) return;
                cleanup();
                reject(new Error(data.error));
            };
            const cleanup = () => {
                ipcRenderer.removeListener('ollama:chat:chunk', onChunkEvt);
                ipcRenderer.removeListener('ollama:chat:done', onDone);
                ipcRenderer.removeListener('ollama:chat:error', onError);
            };

            ipcRenderer.on('ollama:chat:chunk', onChunkEvt);
            ipcRenderer.on('ollama:chat:done', onDone);
            ipcRenderer.on('ollama:chat:error', onError);

            ipcRenderer.send('ollama:chat:start', { requestId, url, model, messages });
        });
    },
});

// NEW: used by codechat.html / codechat.js (the coding assistant window),
// which now shares this same preload.cjs.
contextBridge.exposeInMainWorld('codeChatAPI', {
    openFile: () => ipcRenderer.invoke('code-chat:open-file'),
    saveFile: (filePath, content) => ipcRenderer.invoke('code-chat:save-file', { filePath, content }),
    // NEW: writes one entry (user request + resulting code + file path)
    // to a shared rolling log on disk, so lily_bot.py can read it and
    // let Lily answer questions about what happened in this window.
    // See main.js's 'code-chat:log-context' handler for the actual
    // file write.
    logContext: (entry) => ipcRenderer.invoke('code-chat:log-context', entry),
});
