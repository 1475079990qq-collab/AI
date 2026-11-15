"""情感判断模型独立服务（默认 :8101）。

为什么单独一个进程
------------------
1. 模型 1.06GB。大脑是编排层，不该背这个权重：判断服务关掉，大脑照样能跑
   （自动降级成关键词规则），这是「模型可单独启停」的前提。
2. 判断是 CPU 密集（单句 70ms 左右）。独立进程后它的线程数不会和 TTS、
   LLM 抢同一批核。
3. 换模型 / 搬到另一台机器，只改 JUDGE_BASE，大脑一行不动。

启动
----
    4-启动情感判断.bat                 （等价于 python -m brain.judge_server）

接口
----
    GET  /health   → 是否就绪、模型信息、标签表、最近耗时
    GET  /labels   → 8 类标签
    POST /judge    → {"text": "..."} → {label, confidence, intensity, source}

拿到的是**纯模型结果**（含 sentiment_model 内部的 fear 关键词修正）。
「低置信度时用关键词复核」这一步留在大脑侧（judge.py），因为那里有
对话上下文，而且服务挂了也还能兜住。
"""
from __future__ import annotations

import os
import socket
import sys
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field

from brain import log_setup
from brain.config import settings


def _fix_console() -> None:
    """bat 里用 PYTHONIOENCODING 定了编码(GBK)，这里就别再改成 UTF-8 了。"""
    if os.environ.get("PYTHONIOENCODING"):
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass


def _port_busy(port: int) -> bool:
    sock = socket.socket()
    try:
        return sock.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        sock.close()


class JudgeRequest(BaseModel):
    text: str = Field(default="", description="要判断的这句话")
    history: list[dict] = Field(default_factory=list, description="预留，当前模型不用")


_state: dict = {
    "ready": False,
    "error": "",
    "loaded_ms": 0,
    "calls": 0,
    "total_ms": 0.0,
    "last_ms": 0.0,
}


def _load_model() -> None:
    """加载权重并跑一次预热。

    这一步只做一次，放启动阶段（同步）。服务端口开放 == 模型已可用，
    不会出现「端口通了但第一句要等 4 秒」的错觉。
    """
    from brain.services import sentiment_model

    path = settings.JUDGE_MODEL_PATH
    start = time.perf_counter()
    try:
        sentiment_model.load(path)
        sentiment_model.predict(path, "预热")
        _state["ready"] = True
        _state["error"] = sentiment_model.load_error()
    except Exception as exc:
        _state["ready"] = False
        _state["error"] = f"{type(exc).__name__}: {exc}"
    _state["loaded_ms"] = round((time.perf_counter() - start) * 1000.0, 1)


def _banner() -> None:
    info = {}
    try:
        from brain.services import sentiment_model

        info = sentiment_model.info(settings.JUDGE_MODEL_PATH)
    except Exception:
        pass
    print("=" * 58)
    print("  情感判断服务  judge server")
    print("=" * 58)
    print(f"  监听地址  : http://{settings.JUDGE_HOST}:{settings.JUDGE_PORT}")
    print(f"  模型文件  : {settings.JUDGE_MODEL_PATH}")
    print(f"  体积      : {info.get('size_mb', 0)} MB   设备 : CPU（不占显存）")
    print(f"  加载耗时  : {_state['loaded_ms']} ms   "
          f"状态 : {'就绪' if _state['ready'] else '未就绪'}")
    if _state["error"]:
        print(f"  [错误] {_state['error']}")
        print("         先跑 tools\\setup_judge_model.py 下载并转换模型。")
    print("=" * 58)
    print("  这个窗口保持打开就是判断服务在跑；关掉即停（大脑会自动退回关键词规则）。")
    print()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _load_model()
    _banner()
    yield


app = FastAPI(title="companion judge", lifespan=_lifespan)


@app.get("/health")
def health() -> dict:
    try:
        from brain.services import sentiment_model

        info = sentiment_model.info(settings.JUDGE_MODEL_PATH)
    except Exception:
        info = {}
    calls = _state["calls"]
    return {
        "status": "ok",
        "service": "judge",
        "ready": bool(_state["ready"]),
        "detail": _state["error"],
        "model": info,
        "loaded_ms": _state["loaded_ms"],
        "labels": info.get("labels") or [],
        "stats": {
            "calls": calls,
            "avg_ms": round(_state["total_ms"] / calls, 1) if calls else 0,
            "last_ms": _state["last_ms"],
        },
    }


@app.get("/labels")
def labels() -> dict:
    try:
        from brain.services.sentiment_model import MODEL_ID2LABEL

        return {"labels": sorted(set(MODEL_ID2LABEL))}
    except Exception:
        return {"labels": []}


@app.post("/judge")
def judge(req: JudgeRequest):
    from fastapi.responses import JSONResponse

    if not _state["ready"]:
        return JSONResponse(
            status_code=503,
            content={"detail": f"judge model not ready: {_state['error']}"},
        )
    text = (req.text or "").strip()
    if not text:
        return JSONResponse(status_code=400, content={"detail": "text is empty"})

    started = time.perf_counter()
    try:
        from brain.services import sentiment_model

        out = sentiment_model.predict(settings.JUDGE_MODEL_PATH, text)
    except Exception as exc:
        log_setup.log().warning("判断失败  ｜%s：%s", text[:24], exc)
        return JSONResponse(status_code=503, content={"detail": f"predict failed: {exc}"})
    if not out:
        log_setup.log().warning("判断失败  ｜%s：模型返回空", text[:24])
        return JSONResponse(status_code=503, content={"detail": "predict returned None"})

    cost = (time.perf_counter() - started) * 1000.0
    _state["calls"] += 1
    _state["total_ms"] += cost
    _state["last_ms"] = round(cost, 1)
    log_setup.log().info(
        "判断  %s（%.2f / 强度 %.2f）%.0f ms  ｜%s",
        out["label"], float(out["confidence"]), float(out["intensity"]), cost, text[:24],
    )
    return {
        "label": out["label"],
        "confidence": out["confidence"],
        "intensity": out["intensity"],
        "source": "onnx",
        "latency_ms": _state["last_ms"],
        "probs": out.get("probs"),
    }


def main() -> None:
    _fix_console()
    log_setup.start("judge")
    if _port_busy(settings.JUDGE_PORT):
        print()
        print(f"[提示] 端口 {settings.JUDGE_PORT} 已经有服务在跑了，不用重复启动。")
        print(f"       浏览器打开 http://127.0.0.1:{settings.JUDGE_PORT}/health 可以确认。")
        print()
        return
    try:
        uvicorn.run(
            app,
            host=settings.JUDGE_HOST,
            port=settings.JUDGE_PORT,
            log_level="warning",
            log_config=None,
        )
    except Exception:
        import traceback
        from pathlib import Path

        log = Path(settings.ROOT) / "data" / "logs" / "judge-server.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(traceback.format_exc(), encoding="utf-8")
            print(f"[ERROR] 判断服务启动失败，详细错误：{log}")
        except Exception:
            traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
