"""Provider 读取与连接探测（S1）。

实时链路只经这里读 provider 配置，不直接碰存储层。
所有探测绝不抛异常，异常一律降级，避免控制平面 500。
"""
from __future__ import annotations

import time

import httpx

from brain import config_store, db


def get(code: str) -> dict:
    """薄封装 config_store.get_provider(code)。"""
    return config_store.get_provider(code)


def resolve(code: str, key: str, default=None):
    """取单个配置值；读不到返回 default。"""
    try:
        return (get(code).get("config") or {}).get(key, default)
    except Exception:
        return default


def snapshot() -> dict:
    try:
        return config_store.provider_snapshot()
    except Exception:
        return {}


def _result(ok: bool, mode: str, detail: str, started: float) -> dict:
    return {
        "ok": bool(ok),
        "mode": str(mode),
        "detail": str(detail),
        "latency_ms": round((time.perf_counter() - started) * 1000.0, 2),
    }


def test(code: str) -> dict:
    """真实探测，但任何异常一律降级为 ok=False，绝不抛、绝不 500。"""
    started = time.perf_counter()
    try:
        p = get(code)
        mode = str(p.get("mode") or "off")
        cfg = p.get("config") or {}
    except Exception as exc:
        return _result(False, "off", f"读取 provider 配置失败：{exc}", started)

    # 停用 / 关闭：如实说明「没开」，不冒充连通成功
    if int(p.get("enabled", 1)) == 0 or mode.lower() == "off":
        return _result(False, mode, "该服务已在控制台停用（运行方式=已关闭）", started)

    # TTS 进程内引擎：查本进程引擎状态，不再去探独立 :8102。
    if code == "tts" and mode.lower() == "inproc":
        try:
            from brain.services import tts as tts_svc

            info = tts_svc.status(force=True)
            return _result(bool(info.get("up")), "inproc",
                            str(info.get("detail") or info.get("endpoint") or "进程内引擎"), started)
        except Exception as exc:
            return _result(False, "inproc", f"进程内引擎不可用：{type(exc).__name__}: {exc}", started)

    try:
        if code in ("llm", "llm_alt", "embedding", "tts", "asr"):
            url = str(cfg.get("base_url") or "")
            if not url:
                return _result(False, mode, "未配置 base_url，无法探测", started)
            headers = {}
            key = str(cfg.get("api_key") or "")
            if key:
                headers["Authorization"] = f"Bearer {key}"
            # tts / asr 探 /health 而不是根路径：我们的服务根路径是 404，
            # 只看「连上了」会把「服务在但模型没装好」也算成成功。
            probe_health = code in ("tts", "asr")
            target = url.rstrip("/") + "/health" if probe_health else url
            resp = httpx.get(target, headers=headers, timeout=3.0, trust_env=False)
            if probe_health:
                payload = {}
                try:
                    payload = resp.json()
                except Exception:
                    payload = {}
                ready = bool(payload.get("ready", True))
                detail = payload.get("detail") or payload.get("backend") or ""
                return _result(
                    resp.status_code < 500,
                    "http" if ready else "http(未就绪)",
                    f"HTTP {resp.status_code} 就绪={ready} {detail}（{target}）",
                    started,
                )
            return _result(True, mode, f"连通成功 HTTP {resp.status_code}（{url}）", started)

        if code == "judge":
            from brain.services.judge import engine_status, judge as judge_fn  # 局部 import，避免循环依赖
            r = judge_fn("最近工作压力有点大，想歇一歇", [])
            label = getattr(r, "label", "") or (r.get("label", "") if isinstance(r, dict) else "")
            src = getattr(r, "source", "") or (r.get("source", "") if isinstance(r, dict) else "")
            st = engine_status(force=True)
            model = st.get("model") or {}
            remote = st.get("remote") or {}
            if st.get("effective") == "http":
                detail = (f"判断服务生效 label={label}（{src}，{remote.get('latency_ms', 0)}ms）"
                          f"｜{remote.get('url') or st.get('endpoint')}")
            elif st.get("effective") == "onnx":
                detail = (f"本机模型生效 label={label}（{src}）"
                          f"｜{model.get('size_mb', 0)}MB 输入 {model.get('inputs')}")
            else:
                detail = (f"当前走关键词规则 label={label}"
                          f"（判断服务 {st.get('endpoint') or '未配置'} 未启动，"
                          f"本机模型 {st.get('model_path') or '未配置'}）")
            # 模式回报实际生效的引擎，而不是表里那个静态值，
            # 否则会出现「detail 说模型生效、mode 写 rule」的自相矛盾。
            return _result(True, st.get("effective") or mode, detail, started)

        if code == "knowledge":
            from brain.services import knowledge  # 局部 import，避免循环依赖
            r = knowledge.list_bases()
            degraded = bool(r.get("degraded"))
            n = len(r.get("bases") or [])
            return _result(not degraded, mode,
                           f"知识库列库：{n} 个库，degraded={degraded}", started)

        return _result(False, mode, f"未知 provider：{code}", started)
    except Exception as exc:
        return _result(False, mode, f"探测失败已降级：{exc}", started)
