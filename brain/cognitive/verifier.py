"""校验：模型填充不得改 options 个数/id/chosen_id；越界则回退骨架（D06、技术规格 §6.2）。

L1 纯认知模块：只依赖标准库与同层 trace，不 import services / routers。
模型返回是不可信边界：只准改 options[].summary / options[].cost 的措辞；
options 个数 / options[].id / options[].source / chosen_id 任一被改即判不通过；
chosen_by 只接受白名单 {rule, model}（deep 通道经 fill_with_model 置 model 为合法）；
JSON 解析失败 / 超时(>800ms) / 字段缺失 → 回退骨架、chosen_by=rule、verified=false，
并记一条 kind=decision 且 verified=false 的 trace（技术规格 §6.2 / §7）。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

log = logging.getLogger("brain.verifier")

_DEFAULT_TIMEOUT_MS = 800
_CHOSEN_BY = {"rule", "model"}
_TRACE_KIND = "decision"  # trace._KINDS 白名单内


class VerifyResult(dict):
    """校验结果。

    字典视图：{"verified": bool, "reason": str, "decision": dict}（team-lead 约定）。
    同时兼容实施计划 T21 的元组返回约定：``final, ok = result`` / ``result[0]`` / ``result[1]``。
    """

    def __init__(self, decision: dict, verified: bool, reason: str) -> None:
        super().__init__(verified=bool(verified), reason=str(reason), decision=decision)
        self._decision = decision
        self._verified = bool(verified)

    def __iter__(self):
        yield self._decision
        yield self._verified

    def __getitem__(self, key):
        if key == 0:
            return self._decision
        if key == 1:
            return self._verified
        return super().__getitem__(key)


def _coerce(skeleton: dict, model_output: Any, timeout_ms: int):
    """把模型输出解析为 dict。返回 (candidate|None, reason|None)。"""
    if callable(model_output):
        t0 = time.monotonic()
        try:
            try:
                candidate = model_output(skeleton)
            except TypeError:
                candidate = model_output()
        except Exception as exc:
            return None, f"model_call_error:{type(exc).__name__}"
        elapsed = (time.monotonic() - t0) * 1000
        if elapsed > timeout_ms:
            return None, f"timeout:{elapsed:.0f}ms"
    elif isinstance(model_output, (str, bytes, bytearray)):
        try:
            candidate = json.loads(model_output)
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            return None, f"invalid_json:{type(exc).__name__}"
    elif isinstance(model_output, dict):
        candidate = model_output
    else:
        return None, f"bad_type:{type(model_output).__name__}"
    if not isinstance(candidate, dict):
        return None, "not_object"
    return candidate, None


def _fail(skeleton: dict, reason: str):
    """回退骨架 + 记日志 + 记一条 kind=decision/verified=false 的 trace。绝不冒泡。"""
    bad = dict(skeleton)
    bad["verified"] = False
    bad["chosen_by"] = "rule"
    log.warning("verifier 不通过(%s)，回退骨架", reason)
    try:
        from brain.cognitive import trace  # 延迟导入：快路径零依赖，失败才落观测
        trace.write(kind=_TRACE_KIND,
                    user_id=str(skeleton.get("user_id", "") or ""),
                    trace_id=str(skeleton.get("trace_id", "") or ""),
                    payload={"verified": False, "reason": reason},
                    node_type="structure")
    except Exception as exc:
        log.debug("verifier 降级 trace 失败: %s", type(exc).__name__)
    return bad, reason


def verify_decision(skeleton: dict, model_output: Any, *,
                    timeout_ms: int = _DEFAULT_TIMEOUT_MS) -> VerifyResult:
    """校验模型输出并返回最终结果。

    返回 VerifyResult：``result["verified"]`` / ``result["reason"]`` / ``result["decision"]``，
    亦兼容 ``final, ok = result``。
    """
    skeleton = skeleton if isinstance(skeleton, dict) else {}
    if skeleton.get("skip"):
        return VerifyResult(skeleton, True, "skip")

    candidate, reason = _coerce(skeleton, model_output, timeout_ms)
    if candidate is None:
        bad, reason = _fail(skeleton, reason or "invalid")
        return VerifyResult(bad, False, reason)

    s_opts = skeleton.get("options") or []
    if not isinstance(s_opts, list):
        s_opts = []
    c_opts = candidate.get("options")
    if not isinstance(c_opts, list) or len(c_opts) != len(s_opts):
        bad, reason = _fail(skeleton, "options_count")
        return VerifyResult(bad, False, reason)

    if candidate.get("chosen_id") != skeleton.get("chosen_id"):
        bad, reason = _fail(skeleton, "chosen_id")
        return VerifyResult(bad, False, reason)

    chosen_by = candidate.get("chosen_by", skeleton.get("chosen_by", "rule"))
    if chosen_by not in _CHOSEN_BY:
        bad, reason = _fail(skeleton, "chosen_by")
        return VerifyResult(bad, False, reason)

    for s, c in zip(s_opts, c_opts):
        if (not isinstance(c, dict)
                or c.get("id") != s.get("id")
                or c.get("source") != s.get("source")):
            bad, reason = _fail(skeleton, "option_id_or_source")
            return VerifyResult(bad, False, reason)

    # 合法：只把 summary/cost 措辞从 candidate 覆盖到骨架副本，杜绝注入额外字段。
    final = dict(skeleton)
    new_opts: list[dict[str, Any]] = []
    for s, c in zip(s_opts, c_opts):
        o = dict(s)
        if isinstance(c.get("summary"), str) and c["summary"].strip():
            o["summary"] = c["summary"].strip()[:80]
        if isinstance(c.get("cost"), str) and c["cost"].strip():
            o["cost"] = c["cost"].strip()[:60]
        new_opts.append(o)
    final["options"] = new_opts
    final["verified"] = True
    final["chosen_by"] = chosen_by
    return VerifyResult(final, True, "ok")


# 技术规格 §7 表中的 verifier.verify 命名，保留为同义入口。
verify = verify_decision


# ---------------------------------------------------------------------------
# T33 · 来源验证扩展（技术规格 §2.2 块④ / §8.3）
# 只追加，不改上面 T21 的 verify_decision / VerifyResult / verify。
# L1 纯认知模块：不 import services / routers；异常降级不抛。
# ---------------------------------------------------------------------------

# 来源 kind 白名单（与 db._SRC_KINDS / consolidation.SOURCE_KINDS 一致）
SOURCE_KINDS = {"user_said", "local_doc", "remote_kb", "web", "unknown"}

# 各来源默认信任度：user_said 最高，web 次之，unknown 最低。
_DEFAULT_TRUST = {
    "user_said": 0.9,
    "local_doc": 0.7,
    "remote_kb": 0.6,
    "web": 0.5,
    "unknown": 0.3,
}
_UNKNOWN_KIND = "unknown"


def _clamp01(value) -> float:
    """把 trust 钳制到 [0,1]；非法/非数值/NaN 回退到 unknown 默认信任度，绝不抛。"""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return _DEFAULT_TRUST[_UNKNOWN_KIND]
    if x != x:  # NaN
        return _DEFAULT_TRUST[_UNKNOWN_KIND]
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


class SourceVerification(tuple):
    """来源校验结果：兼容实施计划 T33 的 ``(ok, reason)`` 元组约定，
    同时携带规范化来源（含钳制后的 trust）。

    ``ok, reason = verify_source(src)`` 仍然可用，``print(...)`` 显示 ``(ok, reason)``；
    额外提供 ``.source`` / ``.verified`` / ``.reason`` / ``.kind`` / ``.trust``。
    """

    def __new__(cls, ok: bool, reason: str, source: dict):
        obj = super().__new__(cls, (bool(ok), str(reason)))
        obj._source = source
        return obj

    @property
    def source(self) -> dict:
        return self._source

    @property
    def verified(self) -> bool:
        return self[0]

    @property
    def reason(self) -> str:
        return self[1]

    @property
    def kind(self) -> str:
        return self._source.get("kind", _UNKNOWN_KIND)

    @property
    def trust(self) -> float:
        return self._source.get("trust", _DEFAULT_TRUST[_UNKNOWN_KIND])


def verify_source(src) -> SourceVerification:
    """来源验证（技术规格 §2.2 块④ / §8.3）。

    - kind 必须在 ``SOURCE_KINDS``；非法 kind → 拒绝(ok=False) 并归 unknown；
      缺失/无来源 → 默认 unknown（低信任，不会被当用户事实引用）。
    - ``user_said`` 必须有文本、``local_doc`` 必须有 uri，否则拒绝。
    - trust 钳制到 [0,1]；缺省按 kind 取默认信任度。
    - 返回 SourceVerification：兼容 ``ok, reason = verify_source(src)``；
      ``.source`` 为规范化 dict（kind/uri/trust/text），``.trust`` 为钳制后信任度。
    - 任何异常都降级为 unknown，绝不冒泡。
    """
    try:
        data = src if isinstance(src, dict) else {}
        raw_kind = data.get("kind")
        kind = str(raw_kind).strip() if raw_kind is not None else ""
        kind = kind or _UNKNOWN_KIND
        text = str(data.get("text") or "")
        uri = str(data.get("uri") or "")

        if kind not in SOURCE_KINDS:
            # 非法 kind：保留拒绝标记，同时把规范化来源归到 unknown。
            kind = _UNKNOWN_KIND
            reason = "invalid_kind"
        else:
            reason = "ok"

        if reason == "ok" and kind == "user_said" and not text.strip():
            reason = "empty_user_said"
        if reason == "ok" and kind == "local_doc" and not uri.strip():
            reason = "no_doc_uri"

        raw_trust = data.get("trust")
        if raw_trust is None or raw_trust == "":
            trust = _DEFAULT_TRUST.get(kind, _DEFAULT_TRUST[_UNKNOWN_KIND])
        else:
            trust = _clamp01(raw_trust)

        normalized = {
            "kind": kind,
            "uri": uri.strip(),
            "trust": trust,
            "text": text.strip(),
        }
        return SourceVerification(reason == "ok", reason, normalized)
    except Exception as exc:
        log.debug("来源校验降级: %s", type(exc).__name__)
        normalized = {
            "kind": _UNKNOWN_KIND,
            "uri": "",
            "trust": _DEFAULT_TRUST[_UNKNOWN_KIND],
            "text": "",
        }
        return SourceVerification(False, "invalid_kind", normalized)
