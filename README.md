# AI原生学习（ai-native-learning）

输入一个视频 URL（YouTube / B站 等），在网页上**播放视频**，同时后台**提取音频 → 转录文字 → 翻译中文**，以**双语字幕**形式显示在视频下方。视频下载/转码完成后即可播放，字幕**边播边出**。

本子项目大量复用仓库内既有能力，几乎不重写核心逻辑：

| 能力 | 复用模块 |
|------|---------|
| 视频下载 + 转 Windows 可播放 mp4 | `scripts/lib/video_download.py::download_and_transcode` |
| VAD 分段 + 云端 STT 流式转录 | `scripts/lib/transcript_common.py` + `scripts/audio_to_transcript.py` |
| LLM 批量翻译（流式 SSE + 缓存） | `subtitle_player/backend/translator.py` |
| 翻译参数配置（面板 + .env） | `subtitle_player/backend/translate_config.py` |
| 字幕数据模型 / 源语言判定 | `subtitle_player/backend/models.py` · `parser.py` |

## 工作原理

```
URL ─► yt-dlp 下载+ffmpeg 转码 ─► 本地 playable.mp4 ─► <video> 立即播放
              │
              └─► ffmpeg 抽段(VAD) ─► 云端 STT(流式) ─► 源语言字幕 ─┐
                                                                    ├─► SSE 推前端
                                       LLM 批量翻译(流式) ─► 中文 ──┘
前端：<video>.currentTime 驱动 ─► 命中当前段 ─► 视频下方双语字幕条
```

- **为什么要先下载**：YouTube 等官方 iframe 播放器是黑盒，拿不到音频流；要同时「界面播放」+「转录」，必须由后端用 yt-dlp 取到本地媒体。下载完成的 mp4 已开启 `+faststart`，可立即播放并支持拖动 seek。
- **"边播边放"**：视频就绪即可播放；转录与翻译在后台并发进行，字幕通过 SSE 逐段推送，随播放进度浮现。云端 STT 通常快于实时，观感即"边播边出字幕"。
- **复用与缓存**：同一 URL 再次提交直接复用已下载视频与字幕结果（`<id>.vtjob.json`、`<video>.bilingual.json`），免重复处理。

## 快速开始

```powershell
# 仓库根目录执行（依赖在根 pyproject.toml）
uv run python ai_native_learning/run.py
```

浏览器打开 `http://127.0.0.1:8810`，有两种输入方式：

- **在线视频**：粘贴视频链接（或本地文件绝对路径）→ 点「加载」。
- **本地文件**：点「选择文件」上传本地视频/音频，或直接在输入框填本地路径。

支持的本地格式：`mp4/mkv/mov/webm/avi/flv/m4v/ts/wmv` 及 `mp3/m4a/wav/flac/ogg/aac/opus`。

处理过程的状态条会分阶段显示**操作名 + 进度条**：下载 % → 转码 % → 转录段数 → 翻译进度。

| 参数 | 默认 | 说明 |
|------|------|------|
| `--host` | `127.0.0.1` | 绑定地址 |
| `--port` | `8810` | 端口 |
| `--no-open` | 关 | 不自动打开浏览器 |

## 配置

需要配置**云端 STT** 与**翻译 LLM**（均为 OpenAI 兼容接口）：

- **STT**：`.env` 中 `STT_BASE_URL` / `STT_API_KEY`（缺省回退 `BASE_URL` / `API_KEY`），模型 `STT_MODEL`（默认 `FunAudioLLM/SenseVoiceSmall`）。
- **翻译**：⚙ 设置面板填写 Base URL / API Key / 模型（本地保存，立即生效），或在 `.env` 中配置 `TRANSLATE_*` / `DASHSCOPE_*`。默认百炼 `qwen3.6-flash`。

> 与 `subtitle_player` 共用翻译配置（保存在 `%APPDATA%/SubtitlePlayer/translate_config.json`）。
> 下载的视频与字幕缓存保存在 `%APPDATA%/AiNativeLearning/media/`。

依赖：`yt-dlp`、`ffmpeg`/`ffprobe` 需在 PATH 中（ffmpeg 用于转码、抽段、VAD）。

## 接口

| 接口 | 说明 |
|------|------|
| `POST /api/job` | 提交视频 URL 或本地文件路径（`{source}`），返回 job 快照 |
| `POST /api/job/upload` | 上传本地视频/音频文件（multipart），按本地流程处理 |
| `GET /api/job/{id}` | 读取 job 当前状态与已得字幕 |
| `GET /api/job/{id}/events` | SSE：阶段 / 进度(download·transcode·transcribe·translate) / 视频就绪 / 转录段 / 译文 / 完成 |
| `GET /api/video/{id}` | 以 HTTP Range 服务本地 mp4 |
| `GET/PUT /api/translate-config` | 翻译参数读写 |

## 已知限制

- 首次处理需等待下载+转码（长视频较久）；之后同一链接秒开。
- 字幕时间轴由 RMS VAD 分段得到，为段级（非词级强制对齐）。
- 需要能访问视频站点的网络（含 yt-dlp 的 SOCKS5 代理回退）。
