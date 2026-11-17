"""阶段2 RED/GREEN。跑法：.venv\\Scripts\\python.exe tools\\test_v25_phase2.py"""
from __future__ import annotations
import os, sys, tempfile, shutil
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix="v25-p2-"); _DB = os.path.join(_TMP, "t.db")
os.environ["DB_PATH"] = _DB
from brain import db  # noqa
db.init_db()
P = F = 0
def ok(c, l, d=""):
    global P, F
    if c: P += 1; print("PASS ", l)
    else: F += 1; print("FAIL ", l, "  ", d)

def run():
    uid = "u-k"
    # 来源登记
    sid = db.add_knowledge_source(user_id=uid, uri="user://said", kind="user_said", trust=0.9)
    ok(sid > 0, "登记来源返回 id", str(sid))
    # 写信念
    b1 = db.add_belief(user_id=uid, subject="我", predicate="不吃", object="香菜",
                       confidence=0.8, source_id=sid)
    ok(b1 > 0, "写信念返回 id", str(b1))
    # 新证据替代旧信念：superseded_by 指向新信念，两者都在
    b2 = db.add_belief(user_id=uid, subject="我", predicate="不吃", object="香菜",
                       confidence=0.95, source_id=sid)
    db.supersede_belief(user_id=uid, old_id=b1, new_id=b2)
    rows = db.list_beliefs(uid)
    old = next(r for r in rows if r["id"] == b1)
    ok(old["superseded_by"] == b2 and any(r["id"] == b2 for r in rows),
       "旧信念被替代但保留", str(rows))
    # 来源 kind 枚举校验
    ok(db.add_knowledge_source(user_id=uid, uri="x", kind="bogus") is None,
       "非法 kind 被拒")

    # ===== 阶段2 T27-T33 回归断言（真实接口签名，temp 库 + 独立测试 user_id）=====
    from brain.memory import working as _t27, episodic as _t28, semantic as _t29
    from brain.memory import procedural as _t30, consolidation as _t31
    from brain.tools import local as _t32
    from brain.cognitive import verifier as _t33

    def _boom(*a, **k):
        raise RuntimeError("forced failure")

    # --- T27 working.snapshot(signals, text, limit=12) ---
    _t27_text = "我今天很烦，想用 python 写点东西"
    s = _t27.snapshot({"tech_hits": ["python"], "emotion_words": ["烦"]},
                      _t27_text, limit=50)
    ok(s == {"length": len(_t27_text), "head": _t27_text,
             "intent_hits": ["python"], "emotion_words": ["烦"]},
       "T27 快照完整字段", str(s))
    s0 = _t27.snapshot({}, "", limit=5)
    ok(s0["length"] == 0 and s0["head"] == "" and
       s0["intent_hits"] == [] and s0["emotion_words"] == [],
       "T27 缺字段/空文本默认值", str(s0))
    sn = _t27.snapshot(None, None, limit=5)
    ok(sn["length"] == 0 and sn["head"] == "" and
       sn["intent_hits"] == [] and sn["emotion_words"] == [],
       "T27 text=None 降级为空快照", str(sn))
    sc = _t27.snapshot({}, "abcdefghij", limit=4)
    ok(sc["head"] == "abcd" and sc["length"] == 10,
       "T27 head 按 limit 截断", str(sc))

    # --- T28 episodic.recent(user_id,days) / digest(user_id,limit) ---
    u28 = "u-t28"
    ok(_t28.recent(u28) == [], "T28 空数据 recent 返回 []")
    ok(_t28.digest(u28) == "", "T28 空数据 digest 返回空串")
    db.upsert_episode(u28, "2026-11-16", "昨天聊了搬家和猫", turns=4)
    db.upsert_episode(u28, "2026-11-17", "今天聊了香菜和忌口", turns=6)
    r28 = _t28.recent(u28, days=2)
    ok(len(r28) == 2 and r28[0]["day"] == "2026-11-17",
       "T28 有数据 recent 按日倒序", str(r28))
    d28 = _t28.digest(u28, limit=1)
    ok("香菜" in d28 and "；" not in d28, "T28 digest 取近 N 条拼接", d28)
    _orig_recent28 = db.get_recent_episodes
    try:
        db.get_recent_episodes = _boom
        ok(_t28.recent(u28) == [], "T28 recent 异常降级为 []")
        ok(_t28.digest(u28) == "", "T28 digest 异常降级为空串")
    finally:
        db.get_recent_episodes = _orig_recent28

    # --- T29 semantic.recall(user_id,query,top_k) ---
    u29a, u29b = "u-t29-bigram", "u-t29-belief"
    db.add_fact(fact_id="f-t29-1", user_id=u29a, session_id="t",
                text="他忌口不吃香菜和芹菜", metadata={"source_kind": "local_doc"},
                importance=0.9)
    r29a = _t29.recall(u29a, "香菜", top_k=3)
    ok(any(x["text"] == "他忌口不吃香菜和芹菜" and x["source_kind"] == "local_doc"
           for x in r29a), "T29 bigram 命中事实", str(r29a))
    sid29 = db.add_knowledge_source(user_id=u29b, uri="user://said",
                                    kind="user_said", trust=0.9)
    bid29 = db.add_belief(user_id=u29b, subject="我", predicate="叫",
                          object="小明", confidence=0.8, source_id=sid29)
    r29b = _t29.recall(u29b, "我", top_k=5)
    ok(any(x["id"] == f"belief:{bid29}" and x["source_kind"] == "user_said"
           and x["text"] == "我叫小明" for x in r29b),
       "T29 信念命中带真实 source_kind=user_said", str(r29b))
    ok(_t29.recall(u29a, "", top_k=3) == [], "T29 空查询返回 []")
    _orig_sf29, _orig_lb29 = db.search_facts, db.list_beliefs
    try:
        db.search_facts = _boom
        db.list_beliefs = _boom
        ok(_t29.recall(u29a, "香菜") == [], "T29 双路径异常降级为 []")
    finally:
        db.search_facts, db.list_beliefs = _orig_sf29, _orig_lb29

    # --- T30 procedural.current(user_id) / lessons(user_id,limit) ---
    u30 = "u-t30"
    ok(_t30.current(u30) == [], "T30 无 active 策略返回 []")
    ok(_t30.lessons(u30) == [], "T30 无 lessons 返回 []")
    db.upsert_strategy(user_id=u30, name="短句回应", text="多用短句")
    db.upsert_strategy(user_id=u30, name="短句回应", text="多用短句")
    ok(db.activate_strategy(user_id=u30, name="短句回应") is not None,
       "T30 策略启用前置")
    cur30 = _t30.current(u30)
    ok(len(cur30) == 1 and cur30[0]["name"] == "短句回应"
       and cur30[0]["status"] == "active", "T30 有 active 策略", str(cur30))
    db.add_lesson(user_id=u30, request_id="r1", kind="tone",
                  note="别太啰嗦", weight=0.9)
    ls30 = _t30.lessons(u30, limit=3)
    ok(len(ls30) == 1 and ls30[0]["note"] == "别太啰嗦",
       "T30 有 lessons 返回", str(ls30))
    _orig_as30, _orig_lfp30 = db.active_strategy, db.lessons_for_prompt
    try:
        db.active_strategy = _boom
        db.lessons_for_prompt = _boom
        ok(_t30.current(u30) == [], "T30 current 异常降级为 []")
        ok(_t30.lessons(u30) == [], "T30 lessons 异常降级为 []")
    finally:
        db.active_strategy, db.lessons_for_prompt = _orig_as30, _orig_lfp30

    # --- T31 consolidation.consolidate(user_id,text,*,source_kind) ---
    u31 = "u-t31"
    r31 = _t31.consolidate(u31, "我不吃香菜", source_kind="user_said")
    ok(r31["stored"] and isinstance(r31["belief_id"], int)
       and r31["source_kind"] == "user_said", "T31 普通巩固 stored", str(r31))
    bs31 = db.list_beliefs(u31)
    srcs31 = db.list_knowledge_sources(u31, kind="user_said")
    ok(len(bs31) == 1 and len(srcs31) == 1
       and bs31[0]["source_id"] == srcs31[0]["id"]
       and srcs31[0]["kind"] == "user_said",
       "T31 信念带 user_said 来源", str((bs31, srcs31)))
    # 冲突 supersede：两条文本前 12 字相同（predicate 相同），旧行保留且 superseded_by 指向新
    p31 = "我不吃香菜但是后来发现其实"
    r31a = _t31.consolidate(u31, p31 + "没那么严重", source_kind="user_said")
    r31b = _t31.consolidate(u31, p31 + "还是得少吃", source_kind="user_said")
    ok(r31a["stored"] and r31b["stored"], "T31 两次同主题巩固成功", str((r31a, r31b)))
    allb31 = db.list_beliefs(u31, limit=50)
    old31 = next(b for b in allb31 if b["id"] == r31a["belief_id"])
    new31 = next(b for b in allb31 if b["id"] == r31b["belief_id"])
    ok(old31["superseded_by"] == r31b["belief_id"]
       and new31["superseded_by"] is None,
       "T31 冲突 supersede 旧行保留且指向新", str((old31, new31)))
    # E04 事务原子性：第2步 execute 抛错 → 整批 rollback，不留孤儿 source/belief
    u31x = "u-t31-rollback"
    before_src = len(db.list_knowledge_sources(u31x))
    before_bel = len(db.list_beliefs(u31x))
    real_connect = db.connect

    class _BoomConn:
        def __init__(self, inner):
            self._inner, self._n = inner, 0
            self.committed = self.rolled = False

        def execute(self, *a, **k):
            self._n += 1
            if self._n == 2:
                raise RuntimeError("forced step-2 failure")
            return self._inner.execute(*a, **k)

        def commit(self):
            self.committed = True
            self._inner.commit()

        def rollback(self):
            self.rolled = True
            self._inner.rollback()

        def close(self):
            self._inner.close()

    holder31 = {}

    def _boom_connect():
        c = _BoomConn(real_connect())
        holder31["c"] = c
        return c

    try:
        db.connect = _boom_connect
        rr31 = _t31.consolidate(u31x, "我不吃香菜，测试回滚", source_kind="user_said")
        ok(rr31["stored"] is False and rr31["belief_id"] is None,
           "T31 第2步失败不报 stored", str(rr31))
        c31 = holder31["c"]
        ok(c31.rolled and not c31.committed, "T31 异常触发 rollback 且未 commit",
           str((c31.rolled, c31.committed)))
    finally:
        db.connect = real_connect
    ok(len(db.list_knowledge_sources(u31x)) == before_src
       and len(db.list_beliefs(u31x)) == before_bel,
       "T31 E04 原子性：无孤儿 source/belief",
       str((before_src, before_bel,
            db.list_knowledge_sources(u31x), db.list_beliefs(u31x))))

    # --- T32 local.read_text(rel_path, *, base) ---
    d32 = tempfile.mkdtemp(prefix="v25-p2-docs-")
    try:
        doc32 = os.path.join(d32, "hello.txt")
        with open(doc32, "w", encoding="utf-8") as fh:
            fh.write("这是一份本地文档")
        ok(_t32.read_text("hello.txt", base=d32) == "这是一份本地文档",
           "T32 合法相对路径读取")
        ok(_t32.read_text("../outside.txt", base=d32) == "",
           "T32 ../ 越界拒绝")
        ok(_t32.read_text(_DB, base=d32) == "",
           "T32 绝对路径越界拒绝")
        ok(_t32.read_text("missing.txt", base=d32) == "",
           "T32 不存在文件返回空串")
    finally:
        shutil.rmtree(d32, ignore_errors=True)

    # --- T33 verifier.verify_source(src) / SOURCE_KINDS ---
    ok("web" in _t33.SOURCE_KINDS and "user_said" in _t33.SOURCE_KINDS,
       "T33 SOURCE_KINDS 白名单")
    v33 = _t33.verify_source({"kind": "web", "uri": "https://example.com/a", "trust": 0.5})
    ok(v33.verified and v33.reason == "ok" and v33.kind == "web"
       and abs(v33.trust - 0.5) < 1e-9, "T33 合法 kind 通过",
       str((v33.verified, v33.reason, v33.source)))
    vbad33 = _t33.verify_source({"kind": "bogus", "uri": "x"})
    ok((not vbad33.verified) and vbad33.reason == "invalid_kind"
       and vbad33.kind == "unknown", "T33 非法 kind 拒绝并归 unknown",
       str((vbad33.verified, vbad33.reason, vbad33.source)))
    vhi33 = _t33.verify_source({"kind": "web", "uri": "x", "trust": 5})
    vlo33 = _t33.verify_source({"kind": "web", "uri": "x", "trust": -3})
    vnan33 = _t33.verify_source({"kind": "web", "uri": "x", "trust": float("nan")})
    ok(vhi33.trust == 1.0 and vlo33.trust == 0.0
       and abs(vnan33.trust - 0.3) < 1e-9,
       "T33 trust 钳制到 [0,1] 且 NaN 回退",
       str((vhi33.trust, vlo33.trust, vnan33.trust)))
    vnone33 = _t33.verify_source(None)
    ok(vnone33.kind == "unknown" and abs(vnone33.trust - 0.3) < 1e-9,
       "T33 无来源标 unknown 低信任",
       str((vnone33.verified, vnone33.reason, vnone33.source)))

    print(f"\nP={P} F={F}")
    return 0 if F == 0 else 1
if __name__ == "__main__":
    sys.exit(run())
