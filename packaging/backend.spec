# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 配置：把 AI原生学习后端（FastAPI + uvicorn）打成独立可执行目录。

产物（onedir）：dist/AiNativeLearning-backend/AiNativeLearning-backend.exe
  - 入口 ai_native_learning/run.py，接受 --no-open --port <p>
  - frontend/ 作为数据打入，运行时由 backend/paths.py 经 _MEIPASS 定位

构建（项目根目录执行）：
  uv run pyinstaller --noconfirm ai_native_learning/packaging/backend.spec

随后 electron-builder 会把 dist/AiNativeLearning-backend 作为 extraResources 封进单 exe。
"""

import os

PROJECT_ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

datas = [
    (os.path.join(PROJECT_ROOT, "ai_native_learning", "frontend"), "frontend"),
]

hiddenimports = [
    # ai_native_learning 后端
    "ai_native_learning",
    "ai_native_learning.backend.app",
    "ai_native_learning.backend.pipeline",
    "ai_native_learning.backend.jobs",
    "ai_native_learning.backend.store",
    "ai_native_learning.backend.model_config",
    "ai_native_learning.backend.history",
    "ai_native_learning.backend.live_transcribe",
    "ai_native_learning.backend.paths",
    "ai_native_learning.backend.content",
    "ai_native_learning.backend.content.extractor",
    "ai_native_learning.backend.content.segmenter",
    "ai_native_learning.backend.learning",
    "ai_native_learning.backend.learning.routes",
    "ai_native_learning.backend.learning.session",
    "ai_native_learning.backend.learning.llm_service",
    "ai_native_learning.backend.learning.highlight_service",
    "ai_native_learning.backend.learning.voice_stt",
    "ai_native_learning.backend.learning.kb_writer",
    "ai_native_learning.backend.learning.prompts",
    # 仓库内复用模块
    "scripts.lib.video_download",
    "scripts.lib.transcript_common",
    "scripts.lib.content_common",
    "subtitle_player.backend.parser",
    "subtitle_player.backend.translator",
    # 网页/文本抽取
    "trafilatura",
    "trafilatura.settings",
    "trafilatura.core",
    "trafilatura.metadata",
    "trafilatura.xml",
    "trafilatura.utils",
    "trafilatura.htmlprocessing",
    "trafilatura.main_extractor",
    "fitz",
    # 动态导入
    "dotenv",
    "openai",
    "httpx",
    "multipart",
    # uvicorn 运行时子模块
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
]

a = Analysis(
    [os.path.join(PROJECT_ROOT, "ai_native_learning", "run.py")],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AiNativeLearning-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="AiNativeLearning-backend",
)
