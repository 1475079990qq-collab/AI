"""App 更新分发：GET /app/version、GET /app/download。

发布新包时只需把 APK 放进 companion/update/，并改同目录 version.json：

    {"version_code": 4, "version_name": "0.4.0",
     "notes": "这次改了什么", "apk": "companion.apk"}

手机端启动 / 每次回到前台拉 /app/version，version_code 比本地大就提示更新。
刻意不做强更：拿不到 manifest 或文件缺失时一律安静失败，不影响聊天。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter()

# brain/routers/update.py -> companion/update
_UPDATE_DIR = Path(__file__).resolve().parents[2] / "update"


def _manifest() -> dict:
    try:
        raw = json.loads((_UPDATE_DIR / "version.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


@router.get("/app/version")
def app_version() -> JSONResponse:
    manifest = _manifest()
    if not manifest:
        return JSONResponse({"version_code": 0, "version_name": "", "notes": "",
                             "download_path": "", "size": 0})
    apk_name = str(manifest.get("apk") or "companion.apk")
    apk = _UPDATE_DIR / apk_name
    try:
        size = apk.stat().st_size
    except OSError:
        size = 0
    return JSONResponse({
        "version_code": int(manifest.get("version_code") or 0),
        "version_name": str(manifest.get("version_name") or ""),
        "notes": str(manifest.get("notes") or ""),
        "download_path": "/app/download" if size else "",
        "size": size,
    })


@router.get("/app/download")
def app_download():
    manifest = _manifest()
    apk = _UPDATE_DIR / str(manifest.get("apk") or "companion.apk")
    if not apk.exists():
        return JSONResponse(status_code=404, content={"detail": "APK_NOT_FOUND"})
    # filename 会带 Content-Disposition: attachment，浏览器直接下载。
    return FileResponse(
        path=str(apk),
        media_type="application/vnd.android.package-archive",
        filename=apk.name,
    )
