const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const path = require('node:path');

const ENTRY_HTML_PATH = process.env.PSAI_ELECTRON_ENTRY_FILE || path.join(__dirname, '..', 'index.html');

function createWindow() {
  const window = new BrowserWindow({
    width: 1240,
    height: 860,
    minWidth: 980,
    minHeight: 700,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  if (fs.existsSync(ENTRY_HTML_PATH)) {
    window.loadFile(ENTRY_HTML_PATH);
    return;
  }

  window.loadURL(
    `data:text/plain,Missing desktop entry file at ${encodeURIComponent(ENTRY_HTML_PATH)}`
  );
}

app.whenReady().then(() => {
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});
