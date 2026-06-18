# AI原生学习（ai-native-learning）

[English](README.md)

输入一个视频 URL（YouTube / B站 等），在网页上**播放视频**，同时后台**提取音频 -> 转录文字 -> 翻译中文**，以**双语字幕**形式显示在视频下方。视频下载/转码完成后即可播放，字幕**边播边出**。

## 核心复用策略

本子项目大量复用仓库内既有能力，几乎不重写核心逻辑：

| 能力 | 复用模块 |
|---|---|
| 视频下载 + 转 Windows 可播放 mp4 | `scripts/lib/video_download.py::download_and_transcode` |
| VAD 分段 + 云端 STT 流式转录 | `scripts/lib/transcript_common.py` + `scripts/audio_to_transcript.py` |
| LLM 批量翻译（流式 SSE + 缓存） | `subtitle_player/backend/translator.py` |
| 统一模型配置（面板 JSON） | `ai_native_learning/backend/model_config.py` |
| 字幕数据模型 / 源语言判定 | `subtitle_player/backend/models.py`、`subtitle_player/backend/parser.py` |

## 工作原理

```text
URL -> yt-dlp 下载 + ffmpeg 转码 -> 本地 playable.mp4 -> 立即 <video> 播放
                    |
                    -> ffmpeg VAD 分段 -> 云端 STT（流式） -> 源语言字幕 ----+
                                                                             +-> SSE 推前端
                                LLM 批量翻译（流式） -> 目标语言字幕 ---------+
前端：以 <video>.currentTime 驱动命中段落并渲染字幕。
```

- 视频就绪即可播放。
- 转录与翻译并行推进，字幕分段流式出现。
- 同一来源重复提交会复用缓存（`<id>.vtjob.json`、`<video>.bilingual.json`）。

## 快速开始

```powershell
# 在仓库根目录执行（依赖在根 pyproject.toml）
uv run python ai_native_learning/run.py
```

浏览器打开 `http://127.0.0.1:8810`。

输入方式：
- 在线视频链接（YouTube/B站等）
- 本地媒体文件路径
- UI 中直接上传本地文件

支持格式：
- 视频：`mp4,mkv,mov,webm,avi,flv,m4v,ts,wmv`
- 音频：`mp3,m4a,wav,flac,ogg,aac,opus`

启动参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 绑定地址 |
| `--port` | `8810` | 端口 |
| `--no-open` | 关闭 | 不自动打开浏览器 |

## 配置

**仅通过设置面板（⚙）配置**，不读 `.env`、无兜底；缺配置时对应功能直接报错。

配置保存在：`%APPDATA%/AiNativeLearning/models_config.json`（新安装默认为空 `{}`）。

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

媒体与字幕缓存目录：`%APPDATA%/AiNativeLearning/media/`。

### 备用下载代理

设置面板 **「备用下载代理」** Tab：启用后，在线视频**下载**与**元信息获取**（标题/ID）在直连失败/超时时，yt-dlp 会自动尝试配置的代理（如 `socks5h://127.0.0.1:12080`）。未启用则仅直连，不再使用代码内硬编码代理。

系统依赖（PATH）：`yt-dlp`、`ffmpeg`、`ffprobe`。

## API 概览

| 接口 | 说明 |
|---|---|
| `POST /api/job` | 提交 URL 或本地路径（`{source}`） |
| `POST /api/job/upload` | 上传本地媒体 |
| `GET /api/job/{id}` | 查询任务快照 |
| `GET /api/job/{id}/events` | SSE 事件流（状态/进度/字幕/完成） |
| `GET /api/video/{id}` | 支持 Range 的本地 mp4 服务 |
| `GET/PUT /api/models-config` | 读取/保存模型配置 |
| `POST /api/models-config/reset` | 清空配置 |
| `POST /api/models-config/test/text` | 测试文本模型（`{ task }`） |
| `POST /api/models-config/test/stt` | 测试转录模型（可选 `{ backend }`） |

## 已知限制

- 新视频首次处理耗时较长（下载 + 转码 + 初次 STT）。
- 时间轴是段级（VAD），不是词级强对齐。
- 需要可访问视频平台的网络（支持 `yt-dlp` SOCKS5 回退）。
