"""【已废弃的兼容入口】控制台已并入大脑进程。

保留这个文件只是为了：旧启动脚本误点时给一句明确指引，而不是报 404。
它不再挂载任何业务路由，也不再写数据库 —— 所有控制面都在 :8000 的 /api/admin/*。
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="companion console (deprecated)")


@app.get("/{path:path}")
def _moved(path: str = ""):
    return HTMLResponse(
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>控制台已合并</title></head>"
        "<body style='font-family:sans-serif;padding:40px'>"
        "<h2>控制台已经并进大脑了</h2>"
        "<p>不需要再单独启动这个进程。打开 "
        "<a href='http://127.0.0.1:8000/admin.html'>http://127.0.0.1:8000/admin.html</a> "
        "即可。</p></body></html>",
        status_code=200,
    )
