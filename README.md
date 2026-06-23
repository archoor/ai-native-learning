# ai-native-learning (AI Native Learning)

[中文](README.zh-CN.md)

`ai-native-learning` is a learning workstation for **video** and **text** sources. Load a YouTube/Bilibili link, local media, a web article, or a `txt` / `md` / `pdf` file, then study with bilingual captions or a reader view plus an AI learning panel (outline, highlights, quizzes, Feynman practice, reports).

## Features

| Area | What you get |
|---|---|
| **Video** | Download/transcode → play in-browser → streaming transcribe + translate → bilingual subtitles synced to playback |
| **Text** | Extract article body → segment → translate → reader view with learning panel |
| **Learning panel** | Outline, information map, goal-based highlights, self-test, Feynman chat, session report |
| **History** | Last 10 opened sources, persisted under `%APPDATA%/AiNativeLearning/` |
| **Resume playback** | Video position saved every ~5s; restored on reopen (≥5s threshold) |
| **Desktop (Windows)** | Electron shell + bundled Python backend (`release/win-unpacked/`) |
| **Settings** | Unified LLM / STT / download-proxy configuration via UI (no `.env` fallback) |

## How It Works

### Video pipeline

```text
URL/path -> yt-dlp + ffmpeg -> local mp4 -> <video> playback
                |
                -> VAD + cloud STT (streaming) -> source segments --+
                                                                    +-> SSE -> frontend
                       LLM batch translate -> target segments -------+
Frontend: <video>.currentTime drives subtitle rendering.
```

### Text pipeline

```text
URL or txt/md/pdf -> extract body (trafilatura / PyMuPDF) -> segment
                -> translate segments (LLM) -> reader view + learning panel
```

- Video is playable as soon as media preparation finishes; subtitles stream in while you watch.
- Re-submitting the same source reuses cached artifacts (`<id>.vtjob.json`, incremental `segments.jsonl`).
- Same file opened from different paths shares one job via content fingerprint.

## Quick Start

### Option A — Windows desktop app (recommended)

1. Build or use a packaged directory build:
   - `desktop/release/win-unpacked/AiNativeLearning.exe`
2. Keep the **entire `win-unpacked` folder** (backend lives in `resources/backend/`).
3. Optional: run `desktop/create_desktop_shortcut.ps1` for a desktop shortcut.

### Option B — Development (monorepo)

From the parent knowledge-base repository root:

```powershell
uv run python ai_native_learning/run.py
```

Open `http://127.0.0.1:8810`.

> This standalone GitHub export mirrors `ai_native_learning/` from the monorepo. Full Python dependencies are defined in the parent `pyproject.toml`; use the monorepo for day-to-day backend development.

### Input options

| Type | Examples |
|---|---|
| Online video | YouTube, Bilibili, … |
| Local media path | `D:\videos\lesson.mp4` |
| File upload | Video/audio via UI |
| Web article | Any `http(s)` page (non-video URLs) |
| Local document | `.txt`, `.md`, `.pdf` |

**Media formats:** `mp4,mkv,mov,webm,avi,flv,m4v,ts,wmv` and `mp3,m4a,wav,flac,ogg,aac,opus`

**CLI flags:**

| Flag | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8810` | HTTP port |
| `--no-open` | off | Do not auto-open browser |

## User Data (Windows)

| Path | Purpose |
|---|---|
| `%APPDATA%/AiNativeLearning/models_config.json` | LLM / STT / proxy settings |
| `%APPDATA%/AiNativeLearning/media/` | Downloaded/transcoded video cache |
| `%APPDATA%/AiNativeLearning/recent_videos.json` | History + `position_sec` playback bookmarks |
| Browser `localStorage` key `anl_playback_pos` | Playback fallback if close interrupts API save |

## Build Desktop Release

From monorepo root:

```powershell
# 1) Bundle Python backend (PyInstaller onedir)
uv run pyinstaller --noconfirm ai_native_learning/packaging/backend.spec

# 2) Electron directory build
cd ai_native_learning/desktop
npm install
npm run dist

# 3) Optional desktop shortcut
powershell -ExecutionPolicy Bypass -File create_desktop_shortcut.ps1
```

Output: `ai_native_learning/desktop/release/win-unpacked/`

## Configuration

**Configure only via the settings panel (⚙)** — no `.env` fallback; missing config causes the related feature to fail with an error.

### Text models (LLM)

- Global: `base_url`, `api_key`, `temperature`, `max_tokens`
- Per-task models: `translate`, `outline`, `highlight`, `quiz`, `grade`, `feynman`, `report`

### Transcription (STT)

Three backends can be saved; only the **active** one is used at runtime (no cross-backend fallback):

| Backend | Description |
|---|---|
| `whisper` | Local Whisper service URL |
| `cloud` | Cloud STT (OpenAI-compatible) |
| `funasr` | Fun-ASR + OSS credentials |

### Fallback download proxy

In settings, **Download proxy** tab: when enabled, yt-dlp retries via the configured proxy after a direct failure/timeout for both **video download** and **metadata fetch**.

Required tools in PATH (dev / non-bundled runs): `yt-dlp`, `ffmpeg`, `ffprobe`.

## API Overview

| Endpoint | Description |
|---|---|
| `POST /api/job` | Submit URL or local path (`{source}`) |
| `POST /api/job/upload` | Upload local media or document |
| `GET /api/job/{id}` | Get current job snapshot |
| `GET /api/job/{id}/events` | SSE (status / progress / segments / translations / done) |
| `GET /api/video/{id}` | Range-enabled local mp4 |
| `GET /api/history` | Recent sources (max 10) |
| `POST /api/history` | Push / refresh history entry |
| `POST /api/history/position` | Update `position_sec` for a source |
| `POST /api/history/remove` | Remove one history entry |
| `GET/PUT /api/models-config` | Read/write model configuration |
| `POST /api/models-config/reset` | Clear configuration |
| `POST /api/models-config/test/text` | Test text model (`{ task }`) |
| `POST /api/models-config/test/stt` | Test STT backend (optional `{ backend }`) |
| `POST /api/models-config/test/proxy` | Test download proxy reachability |

Learning-panel APIs live under `/api/learn/{job_id}/…` (outline, highlights, quiz, Feynman, report).

## Core Reuse (monorepo)

| Capability | Reused module |
|---|---|
| Video download + mp4 conversion | `scripts/lib/video_download.py` |
| VAD + cloud STT streaming | `scripts/lib/transcript_common.py` |
| LLM batch translation | `subtitle_player/backend/translator.py` |
| Unified model config | `backend/model_config.py` |
| Source language detection | `subtitle_player/backend/parser.py` |
| Web / document extraction | `backend/content/extractor.py` |

## Known Limitations

- First run for a new video may take time (download + transcode + initial STT).
- Subtitle timing is segment-level (VAD), not word-level forced alignment.
- Online video requires network access (optional SOCKS5 fallback for yt-dlp).
- Playback resume keys on `source` string; the same file opened via two different paths may store progress under separate history entries.
- Packaged desktop builds must rebuild **both** PyInstaller backend and Electron shell after frontend/backend changes.
