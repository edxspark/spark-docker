"""应用配置：环境变量级配置（进程启动时确定，不可在界面修改）。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> parents: [0]=core, [1]=app, [2]=backend, [3]=仓库根目录
REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = REPO_ROOT / "backend"


class AppSettings(BaseSettings):
    """进程级配置。业务配置（密钥、音色、发布选项）存在数据库里，见 services/settings_store.py。"""

    model_config = SettingsConfigDict(
        env_prefix="SPARK_",
        env_file=(REPO_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Spark Video Tools"
    debug: bool = False

    host: str = "127.0.0.1"
    port: int = 8720

    # 数据目录
    data_dir: Path = REPO_ROOT / "data"
    database_url: str = ""

    # 前端构建产物目录（存在时由后端直接托管，单端口访问）
    frontend_dist: Path = REPO_ROOT / "frontend" / "dist"

    # 允许的跨域来源（开发模式下 Vite dev server）
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8720",
        "http://127.0.0.1:8720",
    ]

    # 密钥加密主密钥文件（首次启动自动生成，权限 600）
    secret_key_file: Path = REPO_ROOT / "data" / "secrets.key"

    # 流水线并发
    max_concurrent_items: int = 2
    max_concurrent_tts: int = 4
    # yt-dlp 同时下载数
    download_concurrency: int = 2

    # ffmpeg / ffprobe 可执行文件
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    # 浏览器登录态（抖音）与 Playwright
    browser_profile_dir: Path = REPO_ROOT / "data" / "browser"
    douyin_headless: bool = False

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def subtitles_dir(self) -> Path:
        return self.data_dir / "subtitles"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def outputs_dir(self) -> Path:
        return self.data_dir / "outputs"

    @property
    def covers_dir(self) -> Path:
        return self.data_dir / "covers"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def auth_dir(self) -> Path:
        return self.data_dir / "auth"

    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{self.data_dir / 'spark.db'}"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.downloads_dir,
            self.subtitles_dir,
            self.audio_dir,
            self.outputs_dir,
            self.covers_dir,
            self.logs_dir,
            self.auth_dir,
            self.browser_profile_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> AppSettings:
    settings = AppSettings()
    settings.ensure_dirs()
    return settings


settings = get_settings()
