/* Foo AI as a desktop window.

   The window is Electron. The page and the agent stay the Python program: this
   process starts ``python -m foo_ai.gui`` on the loopback interface and shows
   that page with no address bar. Closing the window stops the server.

   From the repository, after ``npm install`` in this directory:

       npm start

   A workspace folder can be named on the command line. The interpreter is
   ``FOO_AI_PYTHON``, else the repository's ``.venv``, else ``python``.
   ``FOO_AI_ROOT`` is the repository when this file is not sitting in ``desktop/``.
*/

const { app, BrowserWindow, Menu, dialog } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const net = require('net');
const path = require('path');

const TITLE_BAR = '#141a26';
const WINDOW_BG = '#0b0e14';
const ANNOUNCE = 'foo-ai-desktop:';

/* The window and taskbar icon from ``build/``: the ICO is what Windows wants,
   the PNG is the same artwork for the other platforms. */
function windowIcon() {
  const name = process.platform === 'win32' ? 'icon.ico' : 'icon.png';
  const file = path.join(__dirname, 'build', name);
  return fs.existsSync(file) ? file : undefined;
}

let win = null;
let server = null;
let pageReady = false;
let lastStderr = '';

function repoRoot() {
  if (process.env.FOO_AI_ROOT) return path.resolve(process.env.FOO_AI_ROOT);
  if (app.isPackaged) return path.join(process.resourcesPath, 'foo-ai');
  return path.resolve(__dirname, '..');
}

function pythonInvocation() {
  if (process.env.FOO_AI_PYTHON) {
    return { command: process.env.FOO_AI_PYTHON, args: [] };
  }
  const root = repoRoot();
  const venv = process.platform === 'win32'
    ? path.join(root, '.venv', 'Scripts', 'python.exe')
    : path.join(root, '.venv', 'bin', 'python');
  if (fs.existsSync(venv)) return { command: venv, args: [] };
  return {
    command: process.platform === 'win32' ? 'python' : 'python3',
    args: [],
  };
}

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

function waitForPort(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tryOnce = () => {
      const socket = net.connect({ host: '127.0.0.1', port }, () => {
        socket.end();
        resolve();
      });
      socket.on('error', () => {
        socket.destroy();
        if (Date.now() > deadline) {
          reject(new Error(`The page did not listen on port ${port}.`));
        } else {
          setTimeout(tryOnce, 40);
        }
      });
    };
    tryOnce();
  });
}

function withTimeout(promise, timeoutMs, message) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(message)), timeoutMs);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

/* The server prints ``foo-ai-desktop: <url>`` before it accepts connections. */
function onceLine(child, prefix) {
  return new Promise((resolve, reject) => {
    let pending = '';
    let settled = false;
    const finish = (fn, value) => {
      if (settled) return;
      settled = true;
      cleanup();
      fn(value);
    };
    const onData = (chunk) => {
      pending += chunk;
      const lines = pending.split(/\r?\n/);
      pending = lines.pop();
      for (const line of lines) {
        if (line.startsWith(prefix)) {
          finish(resolve, line.slice(prefix.length).trim());
          return;
        }
      }
    };
    const onExit = (code) => {
      const detail = lastStderr.trim();
      finish(reject, new Error(detail || `The server exited with status ${code}.`));
    };
    const onError = (error) => {
      finish(reject, error);
    };
    function cleanup() {
      child.stdout.off('data', onData);
      child.off('exit', onExit);
      child.off('error', onError);
    }
    child.stdout.on('data', onData);
    child.on('exit', onExit);
    child.on('error', onError);
  });
}

function workspaceFromArgv(argv) {
  const args = app.isPackaged ? argv.slice(1) : argv.slice(2);
  for (const arg of args) {
    if (!arg || arg.startsWith('-')) continue;
    const resolved = path.resolve(arg);
    if (fs.existsSync(resolved) && fs.statSync(resolved).isDirectory()) return resolved;
  }
  return null;
}

function startServer(port, workspace) {
  const python = pythonInvocation();
  // A folder named on the command line is this run's workspace. Without one the
  // page opens the folder it opened last, and this process's directory is the
  // last resort, so nothing is passed here for that case.
  const args = [
    ...python.args,
    '-m',
    'foo_ai.gui',
    '--no-browser',
    '--host',
    '127.0.0.1',
    '--port',
    String(port),
  ];
  if (workspace) args.push('--workspace', workspace);
  const child = spawn(
    python.command,
    args,
    {
      cwd: repoRoot(),
      windowsHide: true,
      env: {
        ...process.env,
        PYTHONUNBUFFERED: '1',
        PYTHONIOENCODING: 'utf-8',
      },
    },
  );
  child.stdout.setEncoding('utf8');
  child.stderr.setEncoding('utf8');
  child.stdout.on('data', (chunk) => process.stdout.write(chunk));
  child.stderr.on('data', (chunk) => {
    lastStderr += chunk;
    if (lastStderr.length > 8000) lastStderr = lastStderr.slice(-8000);
    process.stderr.write(chunk);
  });
  child.on('exit', (code, signal) => {
    if (!pageReady || server !== child) return;
    const detail = lastStderr.trim() || `The server exited (${signal || code}).`;
    if (win && !win.isDestroyed()) dialog.showErrorBox('Foo AI', detail);
  });
  return child;
}

function stopServer() {
  const child = server;
  server = null;
  if (!child || child.killed) return;
  if (process.platform === 'win32' && child.pid) {
    spawn('taskkill', ['/pid', String(child.pid), '/t', '/f'], {
      windowsHide: true,
      stdio: 'ignore',
    });
    return;
  }
  child.kill('SIGTERM');
}

function loadingPage() {
  const html = `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  html, body { height: 100%; margin: 0; background: ${WINDOW_BG}; color: #94a0b6;
    font: 13px/1.45 "Segoe UI", sans-serif; }
  .top { height: 48px; box-sizing: border-box; background: ${TITLE_BAR};
    border-bottom: 1px solid #1f2734; display: flex; align-items: center;
    gap: 8px; padding: 0 14px 0 14px; padding-right: 150px; -webkit-app-region: drag; }
  .mark { width: 14px; height: 14px; display: grid; place-items: center; }
  .mark svg { width: 100%; height: 100%;
    filter: drop-shadow(0 0 4px rgba(91, 140, 255, .45)); }
  .title { color: #dde4f0; font-weight: 620; }
  .sub { color: #5e6a80; font-size: 11px; }
  main { height: calc(100% - 48px); display: grid; place-items: center; }
</style>
</head>
<body>
  <div class="top"><span class="mark"><svg viewBox="0 0 32 32"><defs><linearGradient id="brand-mark" gradientUnits="userSpaceOnUse" x1="7" y1="8" x2="25" y2="24"><stop offset="0" stop-color="#5b8cff"/><stop offset="1" stop-color="#8f6bff"/></linearGradient></defs><g fill="none" stroke="url(#brand-mark)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 9.5H8.5V22.5"/><path d="M8.5 16H13"/><path d="M19 22.5h4.5"/></g></svg></span><span class="title">Foo AI</span><span class="sub">coding agent</span></div>
  <main>Starting the agent\u2026</main>
</body>
</html>`;
  return `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
}

function send(channel, ...args) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, ...args);
}

async function chooseWorkspace() {
  const result = await dialog.showOpenDialog(win, {
    title: 'Open a workspace folder',
    properties: ['openDirectory'],
  });
  if (result.canceled || result.filePaths.length === 0) return;
  send('foo-ai-workspace', result.filePaths[0]);
}

function installMenu() {
  const menu = Menu.buildFromTemplate([
    {
      label: 'File',
      submenu: [
        { label: 'Open Workspace\u2026', click: chooseWorkspace },
        { label: 'New Conversation', click: () => send('foo-ai-reset') },
        { type: 'separator' },
        { label: 'Settings\u2026', click: () => send('foo-ai-settings') },
        { type: 'separator' },
        { role: 'quit' },
      ],
    },
    {
      label: 'View',
      submenu: [
        { role: 'reload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    {
      label: 'Help',
      submenu: [
        {
          label: 'About Foo AI',
          click: () => {
            dialog.showMessageBox(win, {
              type: 'info',
              title: 'Foo AI',
              message: 'Foo AI',
              detail: 'A small Python coding agent, in its own window.',
            });
          },
        },
      ],
    },
  ]);
  Menu.setApplicationMenu(menu);
}

function createWindow(port) {
  win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 860,
    minHeight: 600,
    show: false,
    backgroundColor: WINDOW_BG,
    title: 'Foo AI',
    icon: windowIcon(),
    autoHideMenuBar: true,
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'hidden',
    titleBarOverlay: process.platform === 'darwin'
      ? false
      : { color: TITLE_BAR, symbolColor: '#94a0b6', height: 48 },
    trafficLightPosition: { x: 14, y: 16 },
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  win.on('page-title-updated', (event) => {
    event.preventDefault();
  });
  win.once('ready-to-show', () => {
    if (win && !win.isDestroyed()) win.show();
  });
  win.on('closed', () => {
    win = null;
  });
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', (event, url) => {
    const local = url.startsWith(`http://127.0.0.1:${port}/`) || url.startsWith('data:');
    if (!local) event.preventDefault();
  });
  installMenu();
  win.loadURL(loadingPage());
}

function fail(error) {
  const detail = lastStderr.trim();
  const message = detail && !String(error.message).includes(detail)
    ? `${error.message}\n\n${detail}`
    : error.message;
  dialog.showErrorBox('Foo AI', message);
  stopServer();
  app.exit(1);
}

async function boot() {
  app.setAppUserModelId('io.foo-ai.desktop');
  const workspace = workspaceFromArgv(process.argv);
  const port = await freePort();
  server = startServer(port, workspace);
  const announced = onceLine(server, ANNOUNCE);
  createWindow(port);
  try {
    await withTimeout(announced, 60000, 'The agent did not start within 60 seconds.');
    await waitForPort(port, 10000);
  } catch (error) {
    fail(error);
    return;
  }
  if (!win || win.isDestroyed()) {
    stopServer();
    return;
  }
  await win.loadURL(`http://127.0.0.1:${port}/`);
  pageReady = true;
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (!win || win.isDestroyed()) return;
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
  });
  app.whenReady().then(boot).catch(fail);
  app.on('window-all-closed', () => {
    stopServer();
    app.quit();
  });
  app.on('before-quit', stopServer);
}
