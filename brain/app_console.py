"""控制平面独立进程 (:8001)。

与实时链路 (:8000, brain.app_brain) 进程级隔离，只共享 data/companion.db，
不 import / 不挂载 brain.routers.chat，两个 app 不合并。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from brain import db
from brain.config import settings
from brain.routers import admin


@asynccontextmanager
async def _lifespan(app: FastAPI):
    db.init_db()
    from brain import config_store
    config_store.init_store()
    yield


app = FastAPI(title="companion console", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],      # 允许写操作 + OPTIONS 预检
    allow_headers=["*"],      # 放行自定义头 X-Admin-Token
)

# 控制平面 API
app.include_router(admin.router)


# 存活检查
@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "console"}


# 根路径指向 admin.html；web/ 由 C 负责，目录当前可能为空，重定向不报 500
@app.get("/")
def root():
    index = settings.WEB_DIR / "admin.html"
    if index.exists():
        return FileResponse(index)
    return RedirectResponse(url="/admin.html")


# 静态目录最后挂载，避免遮蔽上面的具体路由；check_dir=False 容忍空目录
app.mount(
    "/",
    StaticFiles(directory=str(settings.WEB_DIR), html=True, check_dir=False),
    name="web",
)
