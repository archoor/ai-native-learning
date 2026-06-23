/**
 * Electron 主进程：AI原生学习桌面外壳。
 *
 * 职责：
 *   1. 选一个空闲端口，启动 Python 后端（开发/启动器态 = uv run；打包态 = 内置后端 exe）。
 *   2. 轮询 /api/health 直到后端就绪，期间显示加载页，避免白屏。
 *   3. 后端就绪后窗口加载 http://127.0.0.1:port，内容区固定宽度 1300。
 *   4. 应用退出时彻底结束后端子进程（含子孙进程）。
 *   5. 暴露原生视频文件选择对话框给渲染进程（preload + IPC）。
 *
 * 运行：
 *   npm start         开发/启动器：用本机 uv 运行源码后端
 *   npm run dist      打出便携 exe（启动器：双击即启动，依赖本机 uv + ffmpeg + 源码）
 */

const { app, BrowserWindow, Menu, dialog, ipcMain, shell } = require("electron");
const { spawn, execFile } = require("child_process");
const path = require("path");
const fs = require("fs");
const net = require("net");
const http = require("http");

const DEFAULT_WIDTH  = 1500; // 默认宽度：双栏布局（视频 + 学习面板）
const MIN_WIDTH      = 1100; // 最小宽度：面板折叠后仍可用
const DEFAULT_HEIGHT = 900;
const MIN_HEIGHT     = 640;
const IS_PACKAGED = app.isPackaged || process.env.VT_FORCE_PACKAGED === "1";

let backendProc = null;
let backendPort = 0;
let win = null;

/** 解析项目根：含 pyproject.toml 且含 ai_native_learning/run.py 的目录。 */
function findProjectRoot() {
  if (process.env.VT_PROJECT_ROOT) return process.env.VT_PROJECT_ROOT;
  const bases = [];
  if (process.env.PORTABLE_EXECUTABLE_DIR) bases.push(process.env.PORTABLE_EXECUTABLE_DIR);
  bases.push(path.resolve(__dirname, "..", "..")); // desktop/ → 上两级
  bases.push(process.cwd());
  for (const base of bases) {
    let dir = base;
    for (let i = 0; i < 8; i++) {
      if (
        fs.existsSync(path.join(dir, "pyproject.toml")) &&
        fs.existsSync(path.join(dir, "ai_native_learning", "run.py"))
      ) {
        return dir;
      }
      const parent = path.dirname(dir);
      if (parent === dir) break;
      dir = parent;
    }
  }
  return path.resolve(__dirname, "..", "..");
}

const PROJECT_ROOT = findProjectRoot();

/** 申请一个空闲的本地端口。 */
function getFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

/**
 * 在 GUI 启动时 PATH 可能缺少 uv，主动搜索常见安装位置。
 * 找不到就退回 "uv"，startBackend 里会用 shell 模式再试一次。
 */
function findUv() {
  if (process.env.UV_EXE && fs.existsSync(process.env.UV_EXE)) return process.env.UV_EXE;
  if (process.platform !== "win32") return "uv";
  const home      = process.env.USERPROFILE || process.env.HOME || "";
  const local     = process.env.LOCALAPPDATA || "";
  const appData   = process.env.APPDATA || "";
  const candidates = [
    path.join(local,   "uv", "bin", "uv.exe"),      // 官方 Windows 安装器默认路径
    path.join(appData, "uv", "bin", "uv.exe"),
    path.join(home,    ".local", "bin", "uv.exe"),
    path.join(home,    ".cargo", "bin", "uv.exe"),
    path.join(home,    "AppData", "Local", "uv", "bin", "uv.exe"),
  ];
  for (const c of candidates) if (c && fs.existsSync(c)) return c;
  return "uv"; // 最后回落：若在 PATH 里则 shell 模式能找到
}

/** 启动后端子进程：返回 ChildProcess。 */
function startBackend(port) {
  // 打包态若存在内置后端 exe（自包含构建）则优先用之
  const bundled = path.join(
    process.resourcesPath || "",
    "backend",
    process.platform === "win32" ? "AiNativeLearning-backend.exe" : "AiNativeLearning-backend"
  );
  if (IS_PACKAGED) {
    if (fs.existsSync(bundled)) {
      return spawn(bundled, ["--no-open", "--port", String(port)], {
        cwd: path.dirname(bundled),
        windowsHide: true,
        env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" },
      });
    }
    throw new Error(
      "未找到内置后端（resources/backend/AiNativeLearning-backend.exe）。\n\n" +
        "请使用 release/win-unpacked/AiNativeLearning.exe 启动，或重新执行 npm run dist 生成目录版。"
    );
  }
  // 启动器/开发态：用 uv 运行源码后端
  const uvExe = findUv();
  return spawn(
    uvExe,
    ["run", "python", "ai_native_learning/run.py", "--no-open", "--port", String(port)],
    {
      cwd: PROJECT_ROOT,
      shell: process.platform === "win32",
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" },
    }
  );
}

/** 轮询健康检查，直到后端就绪或超时。 */
function waitForBackend(port, timeoutMs = 60000) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const probe = () => {
      const req = http.get(
        { host: "127.0.0.1", port, path: "/api/health", timeout: 1500 },
        (res) => {
          res.resume();
          if (res.statusCode === 200) return resolve();
          retry();
        }
      );
      req.on("error", retry);
      req.on("timeout", () => {
        req.destroy();
        retry();
      });
    };
    const retry = () => {
      if (Date.now() - start > timeoutMs) {
        return reject(new Error("后端启动超时（60s）"));
      }
      setTimeout(probe, 400);
    };
    probe();
  });
}

/** 彻底结束后端子进程（Windows 需连同子孙进程）。 */
function killBackend() {
  if (!backendProc || backendProc.killed) return;
  const pid = backendProc.pid;
  try {
    if (process.platform === "win32" && pid) {
      execFile("taskkill", ["/pid", String(pid), "/T", "/F"]);
    } else {
      backendProc.kill("SIGTERM");
    }
  } catch (_) {
    /* 忽略清理异常 */
  }
  backendProc = null;
}

async function createWindow() {
  Menu.setApplicationMenu(null); // 去掉默认菜单栏

  win = new BrowserWindow({
    useContentSize: true, // width/height 指网页内容区尺寸，与 CSS 宽度对齐
    width: DEFAULT_WIDTH,
    height: DEFAULT_HEIGHT,
    minWidth: MIN_WIDTH,
    minHeight: MIN_HEIGHT,
    backgroundColor: "#ECE6D6", // 护眼米黄，规避启动白屏
    show: true,
    autoHideMenuBar: true,
    icon: path.join(__dirname, "app-icon.ico"),
    title: "AI原生学习",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  // 外部链接用系统浏览器打开，不在应用内导航
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  await win.loadFile(path.join(__dirname, "loading.html"));

  try {
    backendPort = await getFreePort();
    backendProc = startBackend(backendPort);

    // 收集 stderr，以便后端提前崩溃时能给出有用的报错信息
    let stderrTail = "";
    const appendStderr = (d) => {
      stderrTail = (stderrTail + String(d)).slice(-2000);
      process.stderr.write(`[backend] ${d}`);
    };
    if (backendProc.stdout) backendProc.stdout.on("data", (d) => process.stdout.write(`[backend] ${d}`));
    if (backendProc.stderr) backendProc.stderr.on("data", appendStderr);

    // 后端进程提前退出时立刻终止等待，弹出带 stderr 的错误
    const earlyExit = new Promise((_, reject) => {
      backendProc.on("exit", (code) => {
        console.log(`[backend] exited code=${code}`);
        if (code !== 0 && code !== null) {
          const hint = stderrTail.trim() || "（无输出）";
          reject(new Error(`后端进程异常退出 (code=${code}):\n\n${hint}`));
        }
      });
    });

    await Promise.race([waitForBackend(backendPort), earlyExit]);
    await win.loadURL(`http://127.0.0.1:${backendPort}/`);
  } catch (err) {
    dialog.showErrorBox("启动失败", String(err && err.message ? err.message : err));
    app.quit();
  }
}

// 单实例：再次启动则聚焦已有窗口
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });

  app.whenReady().then(createWindow);

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
}

app.on("window-all-closed", () => app.quit());
app.on("before-quit", killBackend);
app.on("quit", killBackend);
process.on("exit", killBackend);

// ---------- IPC：原生视频/音频文件选择对话框 ----------
ipcMain.handle("pick-video", async () => {
  const r = await dialog.showOpenDialog(win, {
    title: "选择视频/音频文件",
    filters: [
      {
        name: "视频/音频文件",
        extensions: [
          "mp4", "mkv", "mov", "webm", "avi", "flv", "ts", "m4v",
          "mp3", "m4a", "wav", "aac", "flac", "ogg",
        ],
      },
      { name: "所有文件", extensions: ["*"] },
    ],
    properties: ["openFile"],
  });
  return r.canceled || !r.filePaths.length ? null : r.filePaths[0];
});
