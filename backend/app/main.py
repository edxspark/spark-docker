"""FastAPI 应用入口。

启动：uvicorn app.main:app --reload --port 8720
若前端已构建（frontend/dist 存在），后端会直接托管静态文件，单端口访问即可。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import douyin as douyin_api
from app.api import settings as settings_api
from app.api import stats as stats_api
from app.api import tasks as tasks_api
from app.api import youtube as youtube_api
from app.core.config import settings
from app.db import dispose_db, init_db
from app.pipeline.runner import recover_interrupted_tasks
from app.services.settings_store import settings_store

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    await init_db()
    interrupted = await recover_interrupted_tasks()
    if interrupted:
        logger.warning("检测到 %s 个因重启中断的任务，已标记为失败（可重试，已完成阶段会自动跳过）", interrupted)

    # 首次启动时把默认配置写入数据库，便于前端展示与用户修改
    from app.db import SessionLocal

    async with SessionLocal() as session:
        await settings_store.load_all(session)
    logger.info("数据目录：%s", settings.data_dir)
    logger.info("服务已就绪：http://%s:%s", settings.host, settings.port)
    yield
    await dispose_db()


app = FastAPI(
    title="Spark Video Tools API",
    description="YouTube 视频搬运流水线：下载 → 翻译 → 配音 → 发布抖音",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(settings_api.router)
app.include_router(tasks_api.router)
app.include_router(douyin_api.router)
app.include_router(youtube_api.router)
app.include_router(stats_api.router)


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "version": "0.1.0"}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("未处理异常：%s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": f"服务内部错误：{exc}"})


# --------------------------------------------------------------------------------------
# 前端静态资源托管（生产模式单端口访问）
# --------------------------------------------------------------------------------------

_dist = settings.frontend_dist
if _dist.exists() and (_dist / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(_dist / "index.html")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        # 排除 API 路径，其余交给前端路由
        candidate = _dist / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_dist / "index.html")

else:

    @app.get("/", include_in_schema=False)
    async def dev_root() -> dict:
        return {
            "message": "后端已启动。前端开发模式请访问 http://localhost:5173，或先执行 npm run build 后再访问本地址。",
            "docs": "/docs",
        }
