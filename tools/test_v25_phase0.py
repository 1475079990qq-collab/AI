# tools/test_v25_phase0.py
"""块①必修 bug 的失败测试（先 RED，修复后全 GREEN）。

跑法（仓库根）：
    .venv\\Scripts\\python.exe tools\\test_v25_phase0.py
只用副本库，不碰 data/companion.db；退出码：全绿 0，否则 1。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_SRC = os.path.join(ROOT, "data", "companion.db")
_TMP = tempfile.mkdtemp(prefix="v25-phase0-")
_DB = os.path.join(_TMP, "test.db")
if os.path.exists(_SRC):
    shutil.copyfile(_SRC, _DB)
os.environ["DB_PATH"] = _DB

from brain import db  # noqa: E402

# 测试基础设施：副本库缺少 V2 基础表（synapses/neurons 等由 init_db 建），
# 先初始化 schema 才能探测真正的 bug；V2.5 六表仍未纳入 _SCHEMA，保持 RED。
db.init_db()

PASS = 0
FAIL = 0


def ok(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS ", label)
    else:
        FAIL += 1
        print("FAIL ", label, "  ", detail)


def run():
    uid = "u-phase0"
    # 1) synapse_upsert 重复建边应强化（默认 delta 不该是 0）
    db.synapse_upsert(user_id=uid, src="a", dst="b", relation="semantic", weight=0.5)
    db.synapse_upsert(user_id=uid, src="a", dst="b", relation="semantic", weight=0.5)
    rows = db.list_synapses(uid, min_weight=0.0)
    w = next((r["weight"] for r in rows if r["src"] == "a" and r["dst"] == "b"), 0.5)
    ok(w > 0.5, "重复建边会强化（delta 默认>0）", f"weight={w}")

    # 2) reinforce 应覆盖三类关系，不只是 about
    from brain.neural import plasticity
    nid1, nid2 = "n-a", "n-b"
    plasticity.reinforce(user_id="u-rf", request_id="r", seed_ids=[nid1], spread_ids=[nid2])
    rels = {r["relation"] for r in db.list_synapses("u-rf")}
    ok({"semantic", "cooccur", "lesson"}.issubset(rels),
       "reinforce 覆盖 semantic/cooccur/lesson", str(rels))

    # 3) backfill 游标推进：第二次调用不应重复处理最早那批
    # 用 try/except 包裹：当前 backfill 尚不支持 after_id，会抛 TypeError，
    # 这里记为 FAIL 而非中断，保证后续断言全部执行（与第 9 项同款处理）。
    try:
        from brain.neural import growth
        for i in range(5):
            db.add_fact(fact_id=f"bf{i}", user_id="u-bf", session_id="s", text=f"事实{i}")
        r1 = growth.backfill("u-bf", limit=2)
        r2 = growth.backfill("u-bf", limit=2, after_id=r1.get("last_id", 0))
        ok(r1.get("grown", 0) == 2 and r2.get("grown", 0) == 2 and r2.get("last_id", 0) > 0,
           "backfill 游标推进而非反复读最早", f"r1={r1} r2={r2}")
    except Exception as exc:
        ok(False, "backfill 游标推进而非反复读最早", f"{type(exc).__name__}: {exc}")

    # 4) att_k 不得覆盖运行时 memory_top_k（纯函数级检查）
    import inspect
    import brain.services.pipeline as pl
    src = inspect.getsource(pl.handle)
    ok("top_k = max(1, min(6, att_k))" not in src,
       "att_k 不再覆盖 memory_top_k", "仍存在覆盖语句")

    # 5) emotion_label 进 metadata：store_fact 应接受 emotion_label 参数
    import inspect as _i
    from brain.services import memory as mem
    ok("emotion_label" in _i.signature(mem.store_fact).parameters,
       "store_fact 接受 emotion_label", str(_i.signature(mem.store_fact)))

    # 6) 六张新表都已建立
    cols = {r["name"] for r in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in ("agent_state", "goals", "knowledge_sources", "beliefs",
              "cognitive_trace", "evaluation_results"):
        ok(t in cols, f"新表存在: {t}")

    # 7) SSE 生成器不再用 wait_for 取消 __anext__
    import brain.routers.admin as adm
    asrc = inspect.getsource(adm.live_stream)
    ok("__anext__()" not in asrc, "SSE 不用 wait_for 取消生成器")

    # 8) 训练路由已挂鉴权依赖
    import brain.routers.train as tr
    deps = getattr(tr.router, "dependencies", [])
    ok(len(deps) > 0, "train router 挂了鉴权依赖", str(deps))

    # 9) trace 模块存在且能写能查
    try:
        import time
        from brain.cognitive import trace as tmod
        tid = tmod.new_trace_id()
        tmod.write(kind="trace", user_id=uid, trace_id=tid, payload={"channel": "fast"})
        # trace.write 走单线程池异步落库，固定 sleep 在负载下不够稳；
        # 改为轮询至可见（上限 2 秒，每 10ms 查一次），查到或超时才判定。
        deadline = time.monotonic() + 2.0
        items = []
        while time.monotonic() < deadline:
            items = tmod.query(user_id=uid, trace_id=tid)
            if len(items) == 1:
                break
            time.sleep(0.01)
        ok(len(items) == 1, "trace 写入后可查", str(items))
    except Exception as exc:
        ok(False, "trace 模块可写可查", f"{type(exc).__name__}: {exc}")

    print(f"\nPASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
