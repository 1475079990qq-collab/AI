"""实时链路 + 控制面，同一个进程 (:8000)。

合并理由：控制台只是同一套配置的读写壳，情绪判断是同进程内 70ms 的 ONNX，
为它们各起一个进程，换来的是更多端口、更多启动项、更多「到底哪边生效」的疑问。
现在：一个进程、一个端口、一个 CORS 策略、一次启动。

/admin.html 是控制台页面，/api/admin/* 是控制面接口，/chat /tts /asr 是实时链路。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from brain import config_store, db
from brain.config import settings
from brain.routers import admin, chat, train, update

# 主动开口的节流间隔：每 10 分钟看一次，该说才说（服务内部还有 3 小时/每天两次的限制）
_PROACTIVE_INTERVAL_S = 600


async def _proactive_loop() -> None:
    """后台定时器：没人说话时，它自己开一次口。

    放在大脑进程里而不是再起一个线程/进程：这条链路本来就是「一个进程一件事」，
    主动开口也是同一件事的一部分——它只是「说」的另一个触发来源。
    """
    from brain.services import proactive

    while True:
        try:
            await asyncio.to_thread(proactive.ticket)
        except Exception:
            pass
        await asyncio.sleep(_PROACTIVE_INTERVAL_S)

# 只允许本机与内网来源，不再用 "*"。
# 为什么：浏览器里任何一个网页都能以 127.0.0.1 身份发起请求，
# 而管理接口对回环地址是放行的 —— 等于任何网站都能改配置。
# 指定来源后，手机/电脑通过局域网 IP 访问仍然可用，公网来源一律被挡。
_LAN_ORIGIN_RE = (
    r"^https?://("
    r"127\.0\.0\.1|localhost|\[::1\]"
    r"|10\.\d+\.\d+\.\d+"
    r"|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+"
    r"|192\.168\.\d+\.\d+"
    r")(:\d+)?$"
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    db.init_db()
    config_store.init_store()
    # 脑图事件总线绑定当前事件循环：worker 线程（情感判断/记忆/GPT-SoVITS）也要能投递。
    try:
        from brain.services import live

        live.bind_loop(asyncio.get_running_loop())
    except Exception:
        pass
    # 模型服务的体检放在启动器横幅里（brain.launch），这里不重复打印。
    task = asyncio.create_task(_proactive_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="companion", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=_LAN_ORIGIN_RE,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 实时链路（/chat /tts /asr /health）与 App 更新分发
app.include_router(chat.router)
app.include_router(update.router)
# 控制面（/api/admin/*）—— 已并入本进程，不再单独起 :8001
app.include_router(admin.router)
# 实时脑图 SSE（/api/admin/live/*）：与上面同前缀，但口令允许走查询参数
app.include_router(admin.live_router)
# 训练中心（/api/train/* + /train.html）—— 装什么、训什么由页面上自己点
app.include_router(train.router)


@app.get("/")
def root():
    index = settings.WEB_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return HTMLResponse(
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>companion</title></head><body>"
        "<h1>companion</h1>"
        "<p>实时链路已启动：<code>/health</code> <code>/chat</code>；"
        "控制台：<code>/admin.html</code></p>"
        "</body></html>"
    )


# 静态目录最后挂载，避免遮蔽上面的具体路由；check_dir=False 容忍空目录
app.mount(
    "/",
    StaticFiles(directory=str(settings.WEB_DIR), html=True, check_dir=False),
    name="web",
)
