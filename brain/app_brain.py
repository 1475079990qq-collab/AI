"""实时链路独立进程 (:8000)。

与控制平面 (:8001) 进程级隔离，只共享 data/companion.db，
不 import / 不挂载 brain.routers.admin，两个 app 不合并。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from brain import config_store, db
from brain.config import settings
from brain.routers import chat, update


@asynccontextmanager
async def _lifespan(app: FastAPI):
    db.init_db()
    config_store.init_store()
    # 模型服务的体检放在启动器横幅里（brain.launch），这里不重复打印。
    # 关键点：整个大脑进程不 import onnxruntime / torch，也不加载任何权重。
    yield


app = FastAPI(title="companion brain", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 实时链路 API（/chat /tts /health），必须先于静态挂载
app.include_router(chat.router)
# App 更新分发（/app/version /app/download）
app.include_router(update.router)


@app.get("/")
def root():
    index = settings.WEB_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    # web/ 由前端 C 负责，目录可能暂时为空/缺文件：返回提示页，绝不 500
    return HTMLResponse(
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>companion brain</title></head><body>"
        "<h1>companion brain</h1>"
        "<p><code>web/index.html</code> 尚未就绪。实时链路已启动，"
        "可访问 <code>/health</code> 与 <code>/chat</code>。</p>"
        "</body></html>"
    )


# 静态目录最后挂载，避免遮蔽上面的具体路由；check_dir=False 容忍空目录
app.mount(
    "/",
    StaticFiles(directory=str(settings.WEB_DIR), html=True, check_dir=False),
    name="web",
)
