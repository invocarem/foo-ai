/* The page stays a web page. This bridge only tells it that it is inside the
   window, and delivers Open Workspace, New Conversation and Settings from the
   menu. */

const { contextBridge, ipcRenderer } = require('electron');

const settingsHandlers = [];
const resetHandlers = [];
const workspaceHandlers = [];
let pendingWorkspace = null;

function markDesktop() {
  document.documentElement.classList.add('desktop', process.platform);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', markDesktop);
} else {
  markDesktop();
}

ipcRenderer.on('foo-ai-settings', () => {
  for (const handler of settingsHandlers) handler();
});

ipcRenderer.on('foo-ai-reset', () => {
  for (const handler of resetHandlers) handler();
});

ipcRenderer.on('foo-ai-workspace', (_event, directory) => {
  if (workspaceHandlers.length === 0) {
    pendingWorkspace = directory;
    return;
  }
  for (const handler of workspaceHandlers) handler(directory);
});

contextBridge.exposeInMainWorld('fooAiDesktop', {
  onSettings(handler) {
    settingsHandlers.push(handler);
  },
  onReset(handler) {
    resetHandlers.push(handler);
  },
  onWorkspace(handler) {
    workspaceHandlers.push(handler);
    if (pendingWorkspace !== null) {
      const directory = pendingWorkspace;
      pendingWorkspace = null;
      handler(directory);
    }
  },
});
