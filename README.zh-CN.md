# AI原生学习（ai-native-learning）

[English](README.md)

`ai-native-learning` 是一个 **视频 + 文本** 双模式学习工作站：支持 YouTube/B站 链接、本地音视频、网页文章，以及 `txt` / `md` / `pdf` 文件；视频模式有双语字幕，文本模式有阅读器，右侧统一挂载 AI 学习面板（骨架、地图、高亮、自测、费曼、报告）。

## 功能概览

| 模块 | 说明 |
|---|---|
| **视频学习** | 下载/转码 → 浏览器播放 → 流式转录 + 翻译 → 字幕随播放进度切换 |
| **文本学习** | 抽取正文 → 分段 → 翻译 → 阅读器精读 + 学习面板 |
| **学习面板** | 骨架、信息地图、目标高亮、自测答题、费曼讲解、学习报告 |
| **历史记录** | 最近 10 条来源，持久化到 `%APPDATA%/AiNativeLearning/` |
| **续播** | 播放进度约每 5 秒保存一次，再次打开同一来源时恢复（≥5 秒才记） |
| **桌面端（Windows）** | Electron 外壳 + 内置 Python 后端（`release/win-unpacked/`） |
| **设置面板** | LLM / 转录 / 下载代理统一配置（不读 `.env`，无兜底） |

## 工作原理

### 视频流程

```text
链接/路径 -> yt-dlp + ffmpeg -> 本地 mp4 -> <video> 播放
                |
                -> VAD + 云端 STT（流式） -> 源文字幕 ----+
                                                      +-> SSE 推前端
                       LLM 批量翻译 -> 译文字幕 ---------+
前端：以 <video>.currentTime 驱动字幕命中与渲染。
```

### 文本流程

```text
链接或 txt/md/pdf -> 正文抽取（trafilatura / PyMuPDF）-> 分段
                -> LLM 翻译 -> 阅读器 + 学习面板
```

- 视频就绪即可播放，字幕边播边出。
- 同一来源重复提交复用缓存（`<id>.vtjob.json`、增量 `segments.jsonl`）。
- 本地文件按内容指纹归并任务，不同路径打开同一文件可复用结果。

## 快速开始

### 方式 A — Windows 桌面版（推荐）

1. 使用打包目录版：`desktop/release/win-unpacked/AiNativeLearning.exe`
2. **必须保留整个 `win-unpacked` 目录**（后端在 `resources/backend/`）。
3. 可选：执行 `desktop/create_desktop_shortcut.ps1` 创建桌面快捷方式「AI原生学习」。

### 方式 B — 源码开发（monorepo）

在知识库 monorepo 根目录执行：

```powershell
uv run python ai_native_learning/run.py
```

浏览器打开 `http://127.0.0.1:8810`。

> 本 GitHub 仓库是 monorepo 中 `ai_native_learning/` 的导出镜像；完整 Python 依赖在父仓库 `pyproject.toml`，日常后端开发请在 monorepo 内进行。

### 输入方式

| 类型 | 示例 |
|---|---|
| 在线视频 | YouTube、B站等 |
| 本地媒体路径 | `D:\videos\lesson.mp4` |
| 文件上传 | UI 中选择音视频 |
| 网页文章 | 任意非视频站的 `http(s)` 链接 |
| 本地文档 | `.txt`、`.md`、`.pdf` |

**媒体格式：** `mp4,mkv,mov,webm,avi,flv,m4v,ts,wmv` 与 `mp3,m4a,wav,flac,ogg,aac,opus`

**启动参数：**

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 绑定地址 |
| `--port` | `8810` | 端口 |
| `--no-open` | 关闭 | 不自动打开浏览器 |

## 用户数据（Windows）

| 路径 | 用途 |
|---|---|
| `%APPDATA%/AiNativeLearning/models_config.json` | 模型与代理配置 |
| `%APPDATA%/AiNativeLearning/media/` | 下载/转码视频缓存 |
| `%APPDATA%/AiNativeLearning/recent_videos.json` | 历史记录 + `position_sec` 续播进度 |
| 浏览器 `localStorage` 键 `anl_playback_pos` | 关闭时 API 未写完时的进度兜底 |

## 打包桌面 exe

在 monorepo 根目录：

```powershell
# 1) PyInstaller 打包 Python 后端（onedir）
uv run pyinstaller --noconfirm ai_native_learning/packaging/backend.spec

# 2) Electron 目录版
cd ai_native_learning/desktop
npm install
npm run dist

# 3) 可选：更新桌面快捷方式
powershell -ExecutionPolicy Bypass -File create_desktop_shortcut.ps1
```

产物：`ai_native_learning/desktop/release/win-unpacked/`

## 配置

**仅通过设置面板（⚙）配置**，不读 `.env`、无兜底；缺配置时对应功能直接报错。

### 文本模型（LLM）

- 全局：`base_url`、`api_key`、`temperature`、`max_tokens`
- 按任务指定模型：`translate` / `outline` / `highlight` / `quiz` / `grade` / `feynman` / `report`

### 转录模型（STT）

三套方案可同时保存，运行时仅「当前使用」所选方案生效（无方案间回退）：

| 方案 | 说明 |
|---|---|
| `whisper` | 本地 Whisper 服务 URL |
| `cloud` | 云端 STT（OpenAI 兼容） |
| `funasr` | Fun-ASR + OSS 凭据 |

### 备用下载代理

设置面板 **「备用下载代理」** Tab：启用后，在线视频**下载**与**元信息获取**在直连失败/超时时，yt-dlp 会尝试配置的代理（如 `socks5h://127.0.0.1:12080`）。

系统依赖（源码运行，PATH）：`yt-dlp`、`ffmpeg`、`ffprobe`。

## API 概览

| 接口 | 说明 |
|---|---|
| `POST /api/job` | 提交 URL 或本地路径（`{source}`） |
| `POST /api/job/upload` | 上传本地媒体或文档 |
| `GET /api/job/{id}` | 查询任务快照 |
| `GET /api/job/{id}/events` | SSE 事件流 |
| `GET /api/video/{id}` | 支持 Range 的本地 mp4 |
| `GET /api/history` | 最近来源（最多 10 条） |
| `POST /api/history` | 写入/刷新历史 |
| `POST /api/history/position` | 更新某来源的 `position_sec` |
| `POST /api/history/remove` | 删除一条历史 |
| `GET/PUT /api/models-config` | 读取/保存模型配置 |
| `POST /api/models-config/reset` | 清空配置 |
| `POST /api/models-config/test/text` | 测试文本模型 |
| `POST /api/models-config/test/stt` | 测试转录模型 |
| `POST /api/models-config/test/proxy` | 测试下载代理 |

学习面板接口在 `/api/learn/{job_id}/…`（骨架、高亮、出题、费曼、报告等）。

## 核心复用（monorepo）

| 能力 | 复用模块 |
|---|---|
| 视频下载 + 转 mp4 | `scripts/lib/video_download.py` |
| VAD + 云端 STT | `scripts/lib/transcript_common.py` |
| LLM 批量翻译 | `subtitle_player/backend/translator.py` |
| 统一模型配置 | `backend/model_config.py` |
| 源语言判定 | `subtitle_player/backend/parser.py` |
| 网页/文档抽取 | `backend/content/extractor.py` |

## 已知限制

- 新视频首次处理耗时较长（下载 + 转码 + 初次 STT）。
- 时间轴是段级（VAD），不是词级强对齐。
- 在线视频需要可访问平台的网络（支持 yt-dlp 代理回退）。
- 续播按 `source` 字符串区分；同一文件从不同路径打开可能记在两条历史上。
- 桌面打包后改代码需**同时**重跑 PyInstaller 后端与 Electron，否则 exe 内仍是旧前端。
