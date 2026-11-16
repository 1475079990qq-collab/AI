"""训练中心接口：/api/train/*

页面在 /train.html。这里只做三件事：报事实、起任务、停任务。
装什么、训哪个底座、训几轮，全部由用户点选，服务端不猜。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from brain.services import trainer

router = APIRouter(prefix="/api/train", tags=["train"])
log = logging.getLogger("brain.train")


class DatasetBody(BaseModel):
    with_persona: bool = True


class TrainBody(BaseModel):
    base_model: str = ""
    epochs: float = 3.0
    rank: int = 16
    qlora: bool = False
    merge: bool = True


class InstallBody(BaseModel):
    target: str = ""


@router.get("/status")
def get_status() -> JSONResponse:
    try:
        return JSONResponse(content=trainer.status())
    except Exception:
        log.exception("训练体检失败")
        return JSONResponse(status_code=500, content={"ok": False, "error": "STATUS_FAILED"})


@router.get("/models")
def get_models() -> JSONResponse:
    return JSONResponse(content=trainer.list_models())


@router.post("/dataset")
def post_dataset(body: DatasetBody) -> JSONResponse:
    return JSONResponse(content=trainer.start_dataset(with_persona=body.with_persona))


@router.post("/train")
def post_train(body: TrainBody) -> JSONResponse:
    return JSONResponse(content=trainer.start_train(
        base_model=body.base_model, epochs=body.epochs, rank=body.rank,
        qlora=body.qlora, merge=body.merge,
    ))


@router.post("/install")
def post_install(body: InstallBody) -> JSONResponse:
    return JSONResponse(content=trainer.install(body.target))


@router.get("/job")
def get_job() -> JSONResponse:
    return JSONResponse(content=trainer.job())


@router.post("/stop")
def post_stop() -> JSONResponse:
    return JSONResponse(content=trainer.stop())
