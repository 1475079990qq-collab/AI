"""控制平面路由（独立进程 :8001，前缀 /api/admin）。

与实时链路只共享 data/companion.db，绝不 import / 挂载 chat router。
这里只做「配置查询、人格模板、运行时参数、情感事件人工修正、判断日志、GPU 探测」。
所有外部探测（brain /health、nvidia-smi、ollama ps）一律降级，不抛 500。
v2 增量：provider / 知识库 / 语音 / 轮次明细 / 系统信息；新接口一律异常降级，绝不 500。
"""
from __future__ import annotations

import copy
import hmac
import inspect
import math
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from brain import config_store
from brain import db
from brain.config import settings
from brain.schemas import EMOTION_LABELS

# ---- 运行时参数白名单：直接引用 config_store.RUNTIME_RULES（GET/PUT 共用唯一数据源）----
PARAM_RULES: dict[str, dict[str, Any]] = config_store.RUNTIME_RULES

# 实时链路健康检查（进程隔离，只在同机走 127.0.0.1）
_BRAIN_HEALTH_URL = f"http://127.0.0.1:{settings.BRAIN_PORT}/health"
_GPU_QUERY = "name,memory.total,memory.used,utilization.gpu"


# ---- 鉴权：整个控制平面统一校验（GET 也校验，避免只读接口泄露密钥）----
def _is_loopback(host: str) -> bool:
    h = (host or "").strip().lower()
    return h in {"::1", "localhost", "ip6-localhost"} or h.startswith("127.")


def _require_admin_token(
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> None:
    """按两种模式放行：

    - 配了 ADMIN_TOKEN：必须带一致的 X-Admin-Token（常量时间比较，防时序侧信道）。
    - 没配 ADMIN_TOKEN：只允许本机回环访问。局域网 / 手机访问一律 403 并给出配置指引，
      不再出现「口令留空 = 全网可读 api_key」。
    """
    expected = settings.ADMIN_TOKEN
    if expected:
        if not hmac.compare_digest(str(x_admin_token or ""), str(expected)):
            raise HTTPException(status_code=401, detail="X-Admin-Token required or invalid")
        return
    client = getattr(request, "client", None)
    host = str(getattr(client, "host", "") or "")
    if not _is_loopback(host):
        raise HTTPException(
            status_code=403,
            detail="未配置 ADMIN_TOKEN：控制台只允许本机访问。"
                   "要在局域网 / 手机上用，请在 config.yaml 填 ADMIN_TOKEN，"
                   "并在控制台右上角填同一个口令。",
        )


router = APIRouter(prefix="/api/admin", dependencies=[Depends(_require_admin_token)])


# ---- 出网脱敏：secret 字段的值一律置空（库里仍保留真值，链路照常可用）----
def _mask_provider(payload: Any) -> Any:
    """把 provider 的 secret 字段（api_key / mcp_token）抹成空串，并标注被抹的键。

    只在 HTTP 层做，db / config_store 里的真值不动 —— 否则大脑就连不上上游服务了。
    """
    if not isinstance(payload, dict):
        return payload
    fields = payload.get("fields")
    if not isinstance(fields, list) or not isinstance(payload.get("config"), dict):
        return payload
    out = copy.deepcopy(payload)
    masked: list[str] = []
    for f in fields:
        if not isinstance(f, dict) or not f.get("secret"):
            continue
        key = str(f.get("key") or "")
        if key and str(out["config"].get(key) or "").strip():
            out["config"][key] = ""
            masked.append(key)
    if masked:
        out["masked_fields"] = masked
    return out


def _mask_flat_items(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for item in items:
        if isinstance(item, dict) and item.get("secret") and str(item.get("value") or "").strip():
            item = dict(item)
            item["value"] = ""
            item["masked"] = True
        out.append(item)
    return out


# ---- 新增接口统一降级工具：任何依赖（config_store / services）失败都不抛裸异常 ----
def _degraded(exc: Exception, **payload: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    out.update(payload)
    return out


def _provider_brief() -> list[dict[str, Any]]:
    """overview / system 附带的 provider 精简列表：code/mode/enabled/version。"""
    return [
        {"code": p["code"], "mode": p["mode"], "enabled": p["enabled"], "version": p["version"]}
        for p in config_store.list_providers()
    ]


# ---- Pydantic v2 请求体 ----
class PersonaUpsert(BaseModel):
    id: int | None = None
    name: str
    system_prompt: str = Field(..., max_length=8000)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be empty")
        return v


class ParamUpdate(BaseModel):
    key: str
    # 前端可能传数字或字符串，统一在校验阶段转 float
    value: str | int | float | bool


class EmotionPatch(BaseModel):
    emotion_label: str | None = None
    intensity: float | None = Field(default=None, ge=0, le=1)
    summary: str | None = None

    @field_validator("emotion_label")
    @classmethod
    def _label_allowed(cls, v: str | None) -> str | None:
        if v is not None and v not in EMOTION_LABELS:
            raise ValueError(f"emotion_label must be one of {EMOTION_LABELS}")
        return v


# ---- v2 新增接口请求体 ----
class ProviderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")  # 顶层非法字段直接 422
    config: dict[str, Any] | None = None
    enabled: int | bool | None = None
    mode: str | None = None


class VoiceProfileUpdate(BaseModel):
    emotion: str
    reference_audio_id: str | None = None
    voice_id: str | None = None
    emotion_vector: list[Any] | str | None = None
    speed: float | None = None
    volume: float | None = None
    note: str | None = None


class ReferenceAudioUpsert(BaseModel):
    id: str
    name: str
    file_path: str = ""
    emotion: str = ""
    note: str = ""


class KnowledgeSearchBody(BaseModel):
    query: str
    top_k: int | None = None


class KnowledgeRememberBody(BaseModel):
    text: str
    title: str = ""
    tags: str = ""


class TTSPreviewBody(BaseModel):
    text: str
    emotion: str = "neutral"
    reference_audio_id: str | None = None


class FlatParamUpdate(BaseModel):
    code: str
    value: str | int | float | bool


# ---- 扁平参数表（对标小智「参数管理」：一张可搜索的表）----
@router.get("/flat-params")
def flat_params() -> dict[str, Any]:
    try:
        return {"items": _mask_flat_items(config_store.flat_params())}
    except Exception as exc:
        return {"items": [], "available": False, "reason": f"{type(exc).__name__}: {exc}"}


@router.put("/flat-params", dependencies=[Depends(_require_admin_token)])
def put_flat_param(body: FlatParamUpdate) -> dict[str, Any]:
    code = (body.code or "").strip()
    if not code:
        raise HTTPException(status_code=422, detail="code required")
    if "." in code:
        pcode, _, key = code.partition(".")
        try:
            return config_store.set_provider_field(pcode, key, body.value)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    if code not in config_store.RUNTIME_RULES:
        raise HTTPException(status_code=422, detail=f"unknown param: {code}")
    stored = _validate_param(code, body.value)
    db.set_runtime_param(code, stored)
    return {"ok": True, "code": code, "value": stored}


# ---- 存活检查 ----
@router.get("/ping")
def ping() -> dict[str, bool]:
    return {"pong": True}


# ---- 总览 ----
@router.get("/overview")
def overview() -> dict[str, Any]:
    result: dict[str, Any] = {}
    # stats 改用 extended_stats()，失败退回 db.stats()，再失败给空对象
    try:
        result["stats"] = config_store.extended_stats()
    except Exception as exc:
        try:
            result["stats"] = db.stats()
        except Exception:
            result["stats"] = {}
        result["stats_reason"] = f"{type(exc).__name__}: {exc}"
    # 附各 provider 的 code/mode/enabled/version
    try:
        result["providers"] = _provider_brief()
    except Exception as exc:
        result["providers"] = []
        result["providers_reason"] = f"{type(exc).__name__}: {exc}"
    # 短超时代理 /health，任何失败降级为 {"reachable": false}
    try:
        resp = httpx.get(_BRAIN_HEALTH_URL, timeout=2.0)
        resp.raise_for_status()
        try:
            payload: Any = resp.json()
        except ValueError:
            payload = resp.text
        result["brain_health"] = {"reachable": True, "health": payload}
    except Exception:
        result["brain_health"] = {"reachable": False}
    try:
        result["active_persona"] = db.get_active_persona()
    except Exception:
        result["active_persona"] = None
    return result


# ---- 人格模板 ----
@router.get("/personas")
def list_personas() -> list[dict]:
    return db.list_personas()


@router.post("/personas", dependencies=[Depends(_require_admin_token)])
def save_persona(body: PersonaUpsert) -> dict:
    persona_id = db.save_persona(body.id, body.name, body.system_prompt)
    # 回读完整对象，保证含 id / version / is_active / updated_at
    persona = next((p for p in db.list_personas() if p["id"] == persona_id), None)
    if persona is None:
        persona = {
            "id": persona_id,
            "name": body.name,
            "system_prompt": body.system_prompt,
            "is_active": 0,
            "version": 1,
        }
    return persona


@router.post("/personas/{persona_id}/activate", dependencies=[Depends(_require_admin_token)])
def activate_persona(persona_id: int) -> dict:
    """保存即生效不重启：直接改库，实时链路每轮从库里读 persona。"""
    try:
        return db.activate_persona(persona_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="persona not found")


# ---- 运行时参数 ----
@router.get("/params")
def get_params() -> dict[str, Any]:
    # 附带每个 key 允许范围说明（白名单即 RUNTIME_RULES，17 项）
    return {"params": db.get_runtime_params(), "constraints": PARAM_RULES}


def _validate_param(key: str, value: Any) -> str:
    """白名单 + 值域校验，返回规范化后的字符串（db 以 str 存储）。"""
    rule = PARAM_RULES.get(key)
    if rule is None:
        raise HTTPException(status_code=422, detail=f"param '{key}' not in whitelist")
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="value must be numeric")
    if not math.isfinite(num):
        raise HTTPException(status_code=422, detail="value must be finite")
    if rule["type"] == "int":
        if not num.is_integer():
            raise HTTPException(status_code=422, detail=f"{key} must be an integer")
        num = int(num)
    lo, hi = rule["min"], rule["max"]
    if num < lo or num > hi:
        raise HTTPException(status_code=422, detail=f"{key} out of range: {rule['desc']}")
    return str(num)


@router.put("/params", dependencies=[Depends(_require_admin_token)])
def put_params(body: ParamUpdate) -> dict[str, Any]:
    stored = _validate_param(body.key, body.value)
    db.set_runtime_param(body.key, stored)
    return {"key": body.key, "value": stored, "ok": True}


# ---- 情感事件 ----
@router.get("/emotions")
def list_emotions(
    label: str | None = None,
    min_intensity: float | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict]:
    return db.list_emotion_events(label=label, min_intensity=min_intensity, limit=limit)


@router.patch("/emotions/{event_id}", dependencies=[Depends(_require_admin_token)])
def patch_emotion(event_id: int, body: EmotionPatch) -> dict[str, Any]:
    if body.emotion_label is None and body.intensity is None and body.summary is None:
        raise HTTPException(status_code=422, detail="no fields to update")
    ok = db.update_emotion_event(
        event_id,
        label=body.emotion_label,
        intensity=body.intensity,
        summary=body.summary,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="emotion event not found")
    updated: dict[str, Any] = {}
    if body.emotion_label is not None:
        updated["emotion_label"] = body.emotion_label
    if body.intensity is not None:
        updated["intensity"] = body.intensity
    if body.summary is not None:
        updated["summary"] = body.summary
    return {"id": event_id, "ok": True, "updated": updated}


# ---- 判断日志 ----
@router.get("/judge-logs")
def list_judge_logs(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict]:
    return db.list_judge_logs(limit=limit)


# ---- GPU 状态 ----
def _run_cmd(cmd: list[str], timeout: float = 5.0) -> str:
    """固定参数列表（无 shell、无用户输入），失败抛异常交给上层降级。"""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:200]
        raise RuntimeError(f"exit={proc.returncode} {detail}".strip())
    return proc.stdout or ""


def _to_number(raw: str) -> Any:
    try:
        f = float(raw)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return raw


def _parse_smi(raw: str) -> list[dict[str, Any]]:
    gpus: list[dict[str, Any]] = []
    for line in raw.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        gpus.append({
            "name": parts[0],
            "memory_total_mib": _to_number(parts[1]),
            "memory_used_mib": _to_number(parts[2]),
            "utilization_gpu_pct": _to_number(parts[3]),
        })
    return gpus


def _parse_ollama_ps(raw: str) -> list[dict[str, str]]:
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return []
    header = re.split(r"\s{2,}", lines[0].strip())
    rows: list[dict[str, str]] = []
    for ln in lines[1:]:
        cells = re.split(r"\s{2,}", ln.strip())
        rows.append(dict(zip(header, cells)))
    return rows


@router.get("/gpu")
def gpu_status() -> dict[str, Any]:
    """命令不存在 / 超时 / 非 0 退出 / 解析异常一律降级，绝不 500。"""
    try:
        result: dict[str, Any] = {"gpus": [], "nvidia_smi_raw": "", "ollama_ps": {}}
        reasons: list[str] = []

        try:
            smi = _run_cmd([
                "nvidia-smi", f"--query-gpu={_GPU_QUERY}",
                "--format=csv,noheader,nounits",
            ])
            result["nvidia_smi_raw"] = smi.strip()
            result["gpus"] = _parse_smi(smi)
        except Exception as exc:
            reasons.append(f"nvidia-smi: {type(exc).__name__}: {exc}")

        try:
            ps = _run_cmd(["ollama", "ps"])
            result["ollama_ps"] = {
                "available": True,
                "raw": ps.strip(),
                "rows": _parse_ollama_ps(ps),
            }
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            result["ollama_ps"] = {"available": False, "reason": msg}
            reasons.append(f"ollama ps: {msg}")

        if reasons:
            result["available"] = False
            result["reason"] = "; ".join(reasons)
        else:
            result["available"] = True
        return result
    except Exception as exc:
        # 最外层兜底：任何意外都返回降级对象，而不是 500
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


# ---- v2 · providers ----
@router.get("/providers")
def list_providers_route() -> Any:
    try:
        return [_mask_provider(p) for p in config_store.list_providers()]
    except Exception as exc:
        return _degraded(exc, items=[])


@router.get("/providers/{code}")
def get_provider_route(code: str) -> Any:
    try:
        return _mask_provider(config_store.get_provider(code))
    except Exception as exc:
        return _degraded(exc, code=code)


@router.put("/providers/{code}", dependencies=[Depends(_require_admin_token)])
def put_provider_route(code: str, body: ProviderUpdate) -> Any:
    try:
        enabled = None if body.enabled is None else int(body.enabled)
        return config_store.update_provider(code, body.config or {}, enabled=enabled, mode=body.mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        return _degraded(exc, code=code)


@router.post("/providers/{code}/test")
def test_provider_route(code: str) -> Any:
    try:
        from brain.services import providers as providers_svc  # 局部 import，容忍 S1 未落地
        return providers_svc.test(code)
    except Exception as exc:
        return _degraded(exc, code=code, ok=False, degraded=True,
                         detail="provider test 不可用")


# ---- v2 · knowledge ----
@router.get("/knowledge/bases")
def knowledge_bases() -> dict[str, Any]:
    try:
        from brain.services import knowledge as knowledge_svc  # 局部 import
        payload = knowledge_svc.list_bases()
        if not isinstance(payload, dict):
            payload = {"bases": []}
    except Exception as exc:
        payload = _degraded(exc, bases=[])
    # 用户已实拉的 4 个库，始终作为兜底给出
    payload["fallback_bases"] = config_store.KNOWN_BASES
    return payload


@router.post("/knowledge/search")
def knowledge_search(body: KnowledgeSearchBody) -> Any:
    try:
        from brain.services import knowledge as knowledge_svc  # 局部 import
        return knowledge_svc.search(body.query, top_k=body.top_k)
    except Exception as exc:
        return _degraded(exc, results=[], mode="off", degraded=True)


@router.post("/knowledge/remember", dependencies=[Depends(_require_admin_token)])
def knowledge_remember(body: KnowledgeRememberBody) -> Any:
    try:
        from brain.services import knowledge as knowledge_svc  # 局部 import
        # 控制台手动写入 = 用户自己指定的一篇内容，按独立文档建，不进当天纪要
        return knowledge_svc.remember(body.text, title=body.title, tags=body.tags,
                                      daily=False)
    except Exception as exc:
        return _degraded(exc, ok=False, degraded=True)


# ---- v2 · voice profiles ----
@router.get("/voice-profiles")
def list_voice_profiles_route() -> Any:
    try:
        return config_store.list_voice_profiles()
    except Exception as exc:
        return _degraded(exc, items=[])


@router.put("/voice-profiles", dependencies=[Depends(_require_admin_token)])
def put_voice_profile(body: VoiceProfileUpdate) -> Any:
    try:
        patch = {k: v for k, v in body.model_dump(exclude_unset=True).items()
                 if k != "emotion" and v is not None}
        return config_store.update_voice_profile(body.emotion, patch)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        return _degraded(exc, emotion=body.emotion)


# ---- v2 · reference audios ----
@router.get("/reference-audios")
def list_reference_audios_route() -> Any:
    try:
        return config_store.list_reference_audios()
    except Exception as exc:
        return _degraded(exc, items=[])


@router.post("/reference-audios", dependencies=[Depends(_require_admin_token)])
def save_reference_audio_route(body: ReferenceAudioUpsert) -> Any:
    try:
        return config_store.save_reference_audio(
            body.id, body.name, file_path=body.file_path,
            emotion=body.emotion, note=body.note,
        )
    except Exception as exc:
        return _degraded(exc, id=body.id)


@router.delete("/reference-audios/{audio_id}", dependencies=[Depends(_require_admin_token)])
def delete_reference_audio_route(audio_id: str) -> Any:
    try:
        deleted = config_store.delete_reference_audio(audio_id)
        return {"id": audio_id, "deleted": bool(deleted), "ok": bool(deleted)}
    except Exception as exc:
        return _degraded(exc, id=audio_id, deleted=False, ok=False)


# ---- v2 · turn logs / sessions ----
@router.get("/turn-logs")
def list_turn_logs_route(
    user_id: str | None = None,
    session_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> Any:
    try:
        return config_store.list_turn_logs(user_id=user_id, session_id=session_id, limit=limit)
    except Exception as exc:
        return _degraded(exc, items=[])


@router.get("/sessions")
def list_sessions_route(limit: int = Query(default=50, ge=1, le=500)) -> Any:
    try:
        return config_store.list_sessions(limit=limit)
    except Exception as exc:
        return _degraded(exc, items=[])


# ---- v2 · facts ----
@router.get("/facts")
def list_facts_route(
    user_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> Any:
    try:
        return config_store.list_facts(user_id=user_id, limit=limit)
    except Exception as exc:
        return _degraded(exc, items=[])


@router.delete("/facts", dependencies=[Depends(_require_admin_token)])
def delete_fact_route(id: str = Query(...)) -> Any:
    try:
        deleted = config_store.delete_fact(id)
        return {"id": id, "deleted": bool(deleted), "ok": bool(deleted)}
    except Exception as exc:
        return _degraded(exc, id=id, deleted=False, ok=False)


# ---- v2 · 反馈与交付（他点过的勾 / 递出去的卡）----
@router.get("/feedback")
def list_feedback_route(limit: int = Query(default=50, ge=1, le=500)) -> Any:
    try:
        from brain import db as database

        rows = database.recent_feedback(limit=limit)
        return {"items": rows, "total": len(rows)}
    except Exception as exc:
        return _degraded(exc, items=[])


@router.get("/deliveries")
def list_deliveries_route(limit: int = Query(default=50, ge=1, le=500)) -> Any:
    try:
        from brain import db as database

        rows = database.recent_deliveries(limit=limit)
        return {"items": rows, "total": len(rows)}
    except Exception as exc:
        return _degraded(exc, items=[])


# ---- v2 · config audit ----
@router.get("/config-audit")
def list_config_audit_route(limit: int = Query(default=100, ge=1, le=1000)) -> Any:
    try:
        return config_store.list_audit(limit=limit)
    except Exception as exc:
        return _degraded(exc, items=[])


# ---- v2 · system ----
@router.get("/system")
def system_info() -> dict[str, Any]:
    result: dict[str, Any] = {"version": "v2"}
    try:
        result["stats"] = config_store.extended_stats()
    except Exception as exc:
        result["stats"] = {}
        result["stats_reason"] = f"{type(exc).__name__}: {exc}"
    try:
        result["providers"] = _provider_brief()
    except Exception as exc:
        result["providers"] = []
        result["providers_reason"] = f"{type(exc).__name__}: {exc}"
    result["runtime_rules"] = config_store.RUNTIME_RULES
    result["known_bases"] = config_store.KNOWN_BASES
    return result


# ---- v2 · tts preview ----
@router.post("/tts/preview")
async def tts_preview(body: TTSPreviewBody) -> Response:
    try:
        from brain.services import tts as tts_svc  # 局部 import，避免启动期依赖
        result = tts_svc.synthesize(body.text, body.emotion, body.reference_audio_id)
        wav = await result if inspect.isawaitable(result) else result
        return Response(content=wav, media_type="audio/wav")
    except Exception as exc:
        raise HTTPException(status_code=503,
                            detail=f"TTS preview unavailable: {type(exc).__name__}: {exc}")


# ---- v3 · 服务日志：浏览器里直接看每个 bat 的输出去哪了、在干什么 ----
# 每个 bat 起的进程都把输出同时写进 data/logs/<中文名>-YYYYMMDD.log
# （见 brain/log_setup.py）。这里只读文件，不启动、不改任何服务。

_LOG_SERVICES: list[tuple[str, str, int]] = [
    ("brain", "大脑", int(settings.BRAIN_PORT)),
    ("console", "控制台", int(settings.CONSOLE_PORT)),
    ("judge", "情感判断", int(settings.JUDGE_PORT)),
    ("asr", "语音识别", int(settings.ASR_PORT)),
    ("tts", "语音合成", int(settings.TTS_PORT)),
    ("gpt", "GPT语音模型", 9880),
    ("all", "全部", 0),
]

_LOG_TAIL_MAX = 512 * 1024   # 最多读文件尾部 512KB，避免几 MB 的 traceback 顶内存
_LOG_LINES = 200             # 一次给多少行，够看一次启动 + 几轮对话


def _log_dir() -> Path:
    return Path(settings.ROOT) / "data" / "logs"


def _log_file(name: str) -> Path | None:
    """找这个服务「最新的一份」日志：处理按天分割 + 老的固定名两种写法。"""
    log_dir = _log_dir()
    if not log_dir.is_dir():
        return None
    candidates = [p for p in log_dir.glob(f"{name}*.log") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _port_open(port: int) -> bool:
    """这个服务的端口有没有人在听——50ms 超时，探不到就当没起。"""
    if not port:
        return False
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.05):
            return True
    except Exception:
        return False


def _tail_lines(path: Any, limit: int) -> list[str]:
    """读最后 limit 行；文件大就只读尾部，第一行可能被截断，丢掉。"""
    size = path.stat().st_size
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        truncated = False
        if size > _LOG_TAIL_MAX:
            fh.seek(size - _LOG_TAIL_MAX)
            truncated = True
        text = fh.read()
    lines = text.splitlines()
    if truncated and lines:
        lines = lines[1:]
    return lines[-limit:] if limit > 0 else lines


@router.get("/logs")
def list_logs(service: str = Query(default="", description="留空=只列清单；填 brain/tts…=带内容")) -> Any:
    """列日志清单；给 service 时顺带返回该服务最后几行。"""
    key = (service or "").strip().lower()
    log_dir = _log_dir()

    items: list[dict[str, Any]] = []
    for code, name, port in _LOG_SERVICES:
        path = _log_file(name)
        info: dict[str, Any] = {
            "key": code,
            "name": name,
            "port": port,
            "running": _port_open(port),
            "file": str(path) if path else "",
            "size": path.stat().st_size if path else 0,
            "mtime": "",
            "lines": [],
        }
        if path:
            info["mtime"] = datetime.fromtimestamp(path.stat().st_mtime).strftime("%m-%d %H:%M:%S")
        items.append(info)

    result: dict[str, Any] = {"dir": str(log_dir), "exists": bool(log_dir and log_dir.is_dir()), "items": items}

    picked = next((it for it in items if it["key"] == key), None) if key else None
    if picked and picked["file"]:
        try:
            picked["lines"] = _tail_lines(_log_file(picked["name"]), _LOG_LINES)  # type: ignore[arg-type]
        except Exception as exc:
            picked["lines"] = [f"[读日志失败] {type(exc).__name__}: {exc}"]
    result["current"] = picked["key"] if picked else ""
    return result

