"""阶段1 RED/GREEN 测试。跑法：.venv\\Scripts\\python.exe tools\\test_v25_phase1.py"""
from __future__ import annotations
import os, sys, tempfile, shutil
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
_SRC = os.path.join(ROOT, "data", "companion.db")
_TMP = tempfile.mkdtemp(prefix="v25-p1-"); _DB = os.path.join(_TMP, "t.db")
if os.path.exists(_SRC): shutil.copyfile(_SRC, _DB)
os.environ["DB_PATH"] = _DB
from brain import db  # noqa
db.init_db()

P = F = 0
def ok(c, l, d=""):
    global P, F
    if c: P += 1; print("PASS ", l)
    else: F += 1; print("FAIL ", l, "  ", d)

def run():
    from brain.cognitive import channel
    rt = {"channel_enabled": "1", "channel_high_intensity": "0.75",
          "channel_risk_words": "钱,健康,删除",
          "channel_deep_phrases": "想清楚,权衡"}
    # 快速：语气词，无风险词，情绪低
    r1 = channel.classify_channel(text="嗯", signals={"is_filler": True}, intent="filler",
                                  emotion_label="neutral", emotion_intensity=0.1, runtime=rt)
    ok(r1["channel"] == "fast", "语气词走快速", str(r1))
    # 深度：decision 意图
    r2 = channel.classify_channel(text="选哪个", signals={}, intent="decision",
                                  emotion_label="neutral", emotion_intensity=0.2, runtime=rt)
    ok(r2["channel"] == "deep", "决策走深度", str(r2))
    # 深度：风险词
    r3 = channel.classify_channel(text="这笔钱要不要投", signals={}, intent="chat",
                                  emotion_label="neutral", emotion_intensity=0.1, runtime=rt)
    ok(r3["channel"] == "deep", "风险词走深度", str(r3))
    # 深度：高情绪
    r4 = channel.classify_channel(text="我太难过了", signals={}, intent="emotional",
                                  emotion_label="sad", emotion_intensity=0.9, runtime=rt)
    ok(r4["channel"] == "deep", "高情绪走深度", str(r4))
    # 标准：技术闲聊
    r5 = channel.classify_channel(text="显卡怎么装", signals={}, intent="technical",
                                  emotion_label="neutral", emotion_intensity=0.2, runtime=rt)
    ok(r5["channel"] == "standard", "技术走标准", str(r5))
    # 开关关闭 → 全 standard
    rt2 = dict(rt); rt2["channel_enabled"] = "0"
    r6 = channel.classify_channel(text="选哪个", signals={}, intent="decision",
                                  emotion_label="neutral", emotion_intensity=0.2, runtime=rt2)
    ok(r6["channel"] == "standard", "关闭开关回标准", str(r6))

    # ---- T15: agent_state 状态模块 + DAO ----
    from brain.cognitive import agent_state
    # 新用户初始默认
    s0 = agent_state.get("u-t15-cont")
    ok(abs(s0.get("valence", 9)) < 1e-9 and abs(s0.get("energy", 9) - 0.5) < 1e-9
       and s0.get("stance") == "steady", "agent_state 初始默认", str(s0))
    # 连续两轮：第一轮写入，第二轮读到第一轮的值（不是初始默认）
    agent_state.upsert("u-t15-cont", valence=0.4, arousal=-0.3, energy=0.7, stance="warm")
    s1 = agent_state.get("u-t15-cont")
    ok(abs(s1.get("valence", 9) - 0.4) < 1e-9
       and abs(s1.get("arousal", 9) + 0.3) < 1e-9
       and abs(s1.get("energy", 9) - 0.7) < 1e-9
       and s1.get("stance") == "warm", "agent_state 连续两轮状态连续", str(s1))
    # 非法值钳制：v=5→1.0, arousal=-5→-1.0, energy=3→1.0, 非法 stance→steady
    r7 = agent_state.upsert("u-t15-clamp", valence=5, arousal=-5, energy=3, stance="???")
    s2 = agent_state.get("u-t15-clamp")
    ok(abs(r7["valence"] - 1.0) < 1e-9 and abs(r7["arousal"] + 1.0) < 1e-9
       and abs(r7["energy"] - 1.0) < 1e-9 and r7["stance"] == "steady"
       and abs(s2["valence"] - 1.0) < 1e-9 and s2["stance"] == "steady",
       "agent_state 非法值被钳制", str(r7) + " | " + str(s2))
    # ---- T16: goal_manager 目标模块（规则最小）----
    from brain.cognitive import goal_manager as gm
    # 设计文档块②：用户明确表达 → active；推断 → paused 待确认
    g1 = gm.detect_from_text("u-t16-goal", "下周要交方案")
    g1_id = g1["id"] if g1 else -1
    ok(bool(g1) and g1.get("status") == "active" and g1.get("source") == "user_said",
       "T16 用户明说落 active", str(g1))
    ok(any(x["id"] == g1_id for x in gm.active("u-t16-goal")) if g1 else False,
       "T16 active 列出用户明说目标", str(gm.active("u-t16-goal")))
    # 去重：同 user 同 title 且仍 active/paused 时不重复建
    g1b = gm.add("u-t16-goal", "下周要交方案", source="user_said")
    ok(bool(g1b) and g1b.get("exists") is True and g1b.get("id") == g1_id,
       "T16 同目标去重", str(g1b))
    ok(len([x for x in gm.active("u-t16-goal") if x["title"] == "下周要交方案"]) == 1,
       "T16 active 无重复行", str(gm.active("u-t16-goal")))
    # 改成"不做了" → abandoned → active 里没有
    ok(gm.update("u-t16-goal", g1_id, status="abandoned") is True,
       "T16 update status=abandoned", str(g1))
    ok(not any(x["id"] == g1_id for x in gm.active("u-t16-goal")),
       "T16 abandoned 后不在 active", str(gm.active("u-t16-goal")))
    # inferred 来源默认 paused
    gi = gm.add("u-t16-goal", "推测要准备材料", source="inferred")
    gi_id = gi["id"] if gi else -1
    ok(bool(gi) and gi.get("status") == "paused" and gi.get("source") == "inferred",
       "T16 inferred 默认 paused", str(gi))
    # 枚举校验：非法 source/status 被拒绝（只记日志）
    ok(gm.add("u-t16-goal", "越界来源", source="bogus") is None, "T16 非法 source 拒绝")
    ok(gm.update("u-t16-goal", gi_id, status="bogus") is False, "T16 非法 status 拒绝")
    # 随口一句不误判为目标
    ok(gm.detect_from_text("u-t16-goal", "嗯") is None, "T16 随口一句不误判")
    # 归属隔离（E09）
    ok(not any(x["title"] == "下周要交方案" for x in gm.active("u-t16-other")),
       "T16 user_id 归属隔离", str(gm.active("u-t16-other")))
    # DB 异常降级：只记日志不冒泡
    _orig_connect = db.connect
    db.connect = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        ok(gm.add("u-t16-goal", "异常测试", source="user_said") is None, "T16 add DB 异常降级")
        ok(gm.update("u-t16-goal", gi_id, status="done") is False, "T16 update DB 异常降级")
        ok(gm.active("u-t16-goal") == [], "T16 active DB 异常降级")
    finally:
        db.connect = _orig_connect

    # ---- T17: appraisal 评估模块（规则最小）----
    from brain.schemas import EmotionResult
    from brain.cognitive import appraisal as ap
    # 先为该用户建一个活跃目标，供 goal_hit 命中
    gm.detect_from_text("u-t17-appr", "下周要交方案")
    # 正向情绪 → v>0
    ar1 = ap.evaluate(user_id="u-t17-appr", emotion=EmotionResult(label="happy", intensity=0.9),
                      text="太好了", trace_id="tr_t17_1")
    ok(ar1["v"] > 0 and ar1["source"] == "rule", "T17 正向情绪 v>0", str(ar1))
    # 高强度 → |a| 大
    ok(abs(ar1["a"]) >= 0.8, "T17 高强度 |a| 大", str(ar1))
    # 命中目标文本 → goal_hit 非 null
    ar2 = ap.evaluate(user_id="u-t17-appr", emotion=EmotionResult(label="happy", intensity=0.8),
                      text="太好了方案交了", trace_id="tr_t17_2")
    ok(ar2["goal_hit"] is not None, "T17 命中目标 goal_hit 非 null", str(ar2))
    # 负向情绪 → v<0
    ar3 = ap.evaluate(user_id="u-t17-neg", emotion=EmotionResult(label="sad", intensity=0.9),
                      text="真糟糕", trace_id="tr_t17_3")
    ok(ar3["v"] < 0, "T17 负向情绪 v<0", str(ar3))
    # 文本直接命中目标短语 → goal_hit 非 null（复用 detect_from_text）
    ar3b = ap.evaluate(user_id="u-t17-detect", emotion=EmotionResult(label="happy", intensity=0.8),
                       text="下周要交方案", trace_id="tr_t17_3b")
    ok(ar3b["goal_hit"] is not None, "T17 文本命中目标短语 goal_hit 非 null", str(ar3b))
    # 异常输入（非法 intensity）→ degraded 中性值，不抛
    class _BadEmotion:
        label = "happy"
        intensity = "not-a-number"
    ar4 = ap.evaluate(user_id="u-t17-deg", emotion=_BadEmotion(), text="hi", trace_id="tr_t17_4")
    ok(ar4["v"] == 0 and ar4["a"] == 0 and ar4["goal_hit"] is None and ar4["source"] == "degraded",
       "T17 非法输入降级中性值", str(ar4))
    # 任一步失败（依赖抛异常）→ degraded 中性值，不冒泡
    _orig_get = agent_state.get
    agent_state.get = lambda uid: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        ar5 = ap.evaluate(user_id="u-t17-deg2", emotion=EmotionResult(label="happy", intensity=0.8),
                          text="hi", trace_id="tr_t17_5")
        ok(ar5["v"] == 0 and ar5["a"] == 0 and ar5["goal_hit"] is None
           and ar5["source"] == "degraded", "T17 任一步异常降级中性值", str(ar5))
    finally:
        agent_state.get = _orig_get

    # ---- T18: motivation 动机模块（规则最小）----
    from brain.cognitive import motivation as mot
    # 计划验证样例：valence=-0.6 → repair，priority=high
    m1 = mot.drive({"valence": -0.6, "energy": 0.3}, [])
    ok(m1["drive"] == "repair" and m1["priority"] == "high",
       "T18 负向情绪→repair/high", str(m1))
    # 有活跃目标且情绪平稳 → advance
    m2 = mot.drive({"valence": 0.2, "energy": 0.6}, [{"id": 1, "status": "active"}])
    ok(m2["drive"] == "advance" and m2["priority"] == "normal",
       "T18 有活跃目标→advance", str(m2))
    # 无目标且情绪平稳 → connect
    m3 = mot.drive({"valence": 0.2, "energy": 0.8}, [])
    ok(m3["drive"] == "connect" and m3["priority"] == "normal",
       "T18 无目标→connect", str(m3))
    # 低能量 → priority 提升为 high（优先降载）
    m4 = mot.drive({"valence": 0.1, "energy": 0.1}, [])
    ok(m4["priority"] == "high", "T18 低能量→priority high", str(m4))
    # 异常输入（非法 valence）→ 中性降级，不抛
    m5 = mot.drive({"valence": "bad", "energy": 0.5}, [])
    ok(m5["drive"] == "" and m5["priority"] == 0,
       "T18 异常输入降级中性", str(m5))

    # ---- T19: workspace 工作区模块（真做，不落库）----
    from brain.cognitive import workspace as wsmod
    # 聚合字段正确
    ws = wsmod.assemble(
        trace_id="tr_w19", user_id="u-t19",
        signals={"intent": "decision", "is_filler": False},
        appraisal={"v": 0.5, "a": 0.3, "goal_hit": None},
        motivation={"drive": "advance", "priority": "normal"},
        recalls=[{"id": "r1"}], goals=[{"id": 7, "title": "交方案"}],
        candidates=[{"id": "c1"}, {"id": "c2"}], chosen={"chosen_id": "c1"},
        channel="deep",
    )
    ok(ws.trace_id == "tr_w19" and ws.user_id == "u-t19"
       and ws.signals.get("intent") == "decision"
       and ws.appraisal.get("v") == 0.5
       and ws.motivation.get("drive") == "advance"
       and [r["id"] for r in ws.recalls] == ["r1"]
       and [g["id"] for g in ws.goals] == [7]
       and [c["id"] for c in ws.candidates] == ["c1", "c2"]
       and ws.chosen.get("chosen_id") == "c1" and ws.channel == "deep",
       "T19 assemble 聚合字段正确", str(ws))
    # as_payload 最小快照（脱敏：不含原文）
    pl = ws.as_payload()
    ok(pl["channel"] == "deep" and pl["intent"] == "decision"
       and pl["recall_count"] == 1 and pl["goal_count"] == 1
       and pl["candidate_ids"] == ["c1", "c2"] and pl["chosen_id"] == "c1",
       "T19 as_payload 最小快照", str(pl))
    # 缺字段默认值：assemble() 全空 + 直接构造
    we = wsmod.assemble()
    ok(we.signals == {} and we.appraisal == {} and we.motivation == {}
       and we.recalls == [] and we.goals == [] and we.candidates == []
       and we.chosen is None and we.channel == "standard",
       "T19 缺字段取默认值", str(we))
    wd = wsmod.Workspace("tr_d", "u_d")
    ok(wd.signals == {} and wd.recalls == [] and wd.chosen is None,
       "T19 Workspace 构造默认值", str(wd))
    # 不落库：assemble 前后临时库的表结构与行数完全一致
    from brain.cognitive import trace as _t
    def _drain():
        # trace.write 走单 worker 的 ThreadPoolExecutor(FIFO)；提交空任务并等其完成，
        # 作为屏障排空此前已提交的异步写入，避免快照比较期间混入别人的写入（测试竞态）。
        _t._EXEC.submit(lambda: None).result()
    def _snap():
        conn = db.connect()
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                    for t in tables}
        finally:
            conn.close()
    _drain()
    _before = _snap()
    wsmod.assemble(trace_id="tr_nodb", user_id="u-t19-nodb",
                   signals={"intent": "chat"}, recalls=[{"id": "x"}])
    _drain()
    _after = _snap()
    ok(_before == _after, "T19 assemble 不落库（表/行数不变）",
       str(_before) + " | " + str(_after))

    # ---- T20: decision 决策骨架模块（真做，D06）----
    from brain.cognitive import decision as dec

    # (1) 有目标 → 2 个 option、chosen_id 合法、chosen_by=rule、verified=True
    wg = wsmod.assemble(trace_id="tr_t20_goals", user_id="u-t20",
                        signals={"intent": "decision"},
                        appraisal={"v": 0.3, "a": 0.2, "goal_hit": None},
                        motivation={"drive": "advance", "priority": "normal"},
                        goals=[{"id": 7, "title": "交方案"}],
                        channel="deep")
    dg = dec.build_skeleton(wg, trace_id="tr_t20_goals")
    ok(list(dg.keys()) == ["trace_id", "options", "chosen_id", "chosen_by", "verified", "skip"],
       "T20 骨架字段顺序稳定", str(list(dg.keys())))
    ok(dg["trace_id"] == "tr_t20_goals" and dg["chosen_by"] == "rule"
       and dg["verified"] is True and dg["skip"] is False,
       "T20 骨架基础字段齐全", str(dg))
    ok(len(dg["options"]) == 2 and [o["id"] for o in dg["options"]] == ["o1", "o2"]
       and all(set(o) == {"id", "summary", "cost", "source"} and o["source"] == "rule"
               for o in dg["options"]),
       "T20 options 枚举化（id/summary/cost/source，source=rule）", str(dg["options"]))
    ok(dg["chosen_id"] in [o["id"] for o in dg["options"]],
       "T20 chosen_id 合法（命中某个 option id）", str(dg["chosen_id"]))

    # (2) chosen_id 由规则基于 priority/appraisal 选定
    wneg = wsmod.assemble(trace_id="tr_t20_neg", user_id="u-t20",
                          appraisal={"v": -0.6, "a": 0.5},
                          motivation={"priority": "high"},
                          goals=[{"id": 8, "title": "做决定"}], channel="deep")
    dn = dec.build_skeleton(wneg, trace_id="tr_t20_neg")
    ok(dn["chosen_id"] == "o2" and dn["chosen_by"] == "rule",
       "T20 chosen_id 规则选定（负向高优先→o2）", str(dn))

    # (3) 无候选 → options=[]、chosen_id=None（§1.2 失败语义）
    wempty = wsmod.Workspace("tr_t20_empty", "u-t20")  # 全空，channel 默认 standard
    de = dec.build_skeleton(wempty, trace_id="tr_t20_empty")
    ok(de["options"] == [] and de["chosen_id"] is None
       and de["chosen_by"] == "rule" and de["verified"] is True and de["skip"] is False,
       "T20 无候选空结构", str(de))

    # (4) fast 通道 → skip=True，options 空、chosen_id=None
    wf = wsmod.assemble(trace_id="tr_t20_fast", user_id="u-t20",
                        signals={"is_filler": True}, channel="fast")
    df = dec.build_skeleton(wf, trace_id="tr_t20_fast")
    ok(df["skip"] is True and df["options"] == [] and df["chosen_id"] is None,
       "T20 fast 通道跳过骨架", str(df))

    # (5) fill_with_model：只改 summary/cost 措辞，不改 id/source/chosen_id
    def _model(sk):
        return {"options": [
            {"id": "o9", "summary": "  模型润色推进  ", "cost": "  模型成本  ", "source": "model"},
            {"id": "o8", "summary": "", "cost": ""},
        ], "chosen_id": "o9", "chosen_by": "model"}
    filled = dec.fill_with_model(dg, model_fn=_model)
    ok(filled["options"][0]["id"] == "o1" and filled["options"][0]["source"] == "rule"
       and filled["options"][0]["summary"] == "模型润色推进"
       and filled["options"][0]["cost"] == "模型成本"
       and filled["options"][1]["summary"] != ""  # 空串不覆盖，保留骨架措辞
       and filled["chosen_id"] == dg["chosen_id"] and filled["chosen_by"] == "model",
       "T20 fill_with_model 只改措辞", str(filled))
    def _bad(sk):
        raise RuntimeError("boom")
    ok(dec.fill_with_model(dg, model_fn=_bad) == dg,
       "T20 fill_with_model 异常回退骨架")

    # ---- T21: verifier 校验模块（模型返回是不可信边界，§6.2）----
    from brain.cognitive import verifier as ver
    # (1) 只改 summary/cost → verified=true；id/source/chosen_id 不变
    v_good = ver.verify_decision(dg, {"options": [
        {"id": "o1", "summary": "  模型润色1  ", "cost": "  模型成本1  ", "source": "rule"},
        {"id": "o2", "summary": "模型润色2", "cost": "模型成本2", "source": "rule"}],
        "chosen_id": dg["chosen_id"]})
    ok(v_good["verified"] is True
       and [o["id"] for o in v_good["decision"]["options"]] == ["o1", "o2"]
       and all(o["source"] == "rule" for o in v_good["decision"]["options"])
       and v_good["decision"]["options"][0]["summary"] == "模型润色1"
       and v_good["decision"]["options"][0]["cost"] == "模型成本1"
       and v_good["decision"]["chosen_id"] == dg["chosen_id"],
       "T21 只改 summary/cost → verified=true", str(v_good["decision"]))
    # (2) 改 chosen_id → verified=false 且回退骨架
    v_chosen = ver.verify_decision(dg, {"options": [
        {"id": "o1", "source": "rule"}, {"id": "o2", "source": "rule"}],
        "chosen_id": "o2"})
    ok(v_chosen["verified"] is False
       and v_chosen["decision"]["chosen_id"] == dg["chosen_id"]
       and v_chosen["decision"]["chosen_by"] == "rule"
       and v_chosen["decision"]["options"] == dg["options"],
       "T21 改 chosen_id → verified=false 回退骨架", str(v_chosen["decision"]))
    # (3) 多加一个 option → verified=false 且回退骨架
    v_extra = ver.verify_decision(dg, {"options": [
        {"id": "o1", "source": "rule"}, {"id": "o2", "source": "rule"},
        {"id": "o3", "source": "rule"}], "chosen_id": dg["chosen_id"]})
    ok(v_extra["verified"] is False
       and len(v_extra["decision"]["options"]) == len(dg["options"])
       and v_extra["decision"]["options"] == dg["options"],
       "T21 多加 option → verified=false 回退骨架", str(v_extra["decision"]))
    # (4) 非法 JSON → verified=false 且不抛异常
    try:
        v_json = ver.verify_decision(dg, "{not json")
        ok(v_json["verified"] is False and v_json["decision"]["options"] == dg["options"],
           "T21 非法 JSON → verified=false 不抛", str(v_json["decision"]))
    except Exception as exc:
        ok(False, "T21 非法 JSON → verified=false 不抛", "抛异常: " + repr(exc))

    # ---- T22: deliberation 推演模块（接口+触发点）----
    from brain.cognitive import deliberation as dlb
    # (1) 有目标 → 候选非空且每项含 id/summary/cost/source
    w_t22 = wsmod.assemble(trace_id="tr_t22_goals", user_id="u-t22",
                           goals=[{"id": 7, "title": "交方案"}], channel="deep")
    c22 = dlb.deliberate(w_t22)
    ok(isinstance(c22, list) and len(c22) >= 1 and c22[0]["id"] == "g0"
       and all(set(c) == {"id", "summary", "cost", "source"} and c["source"] == "rule"
               for c in c22),
       "T22 有目标 → 候选非空且字段齐全", str(c22))
    # (2) 无信息 → []
    c22e = dlb.deliberate(wsmod.Workspace("tr_t22_empty", "u-t22-empty"))
    ok(c22e == [], "T22 无信息 → []", str(c22e))
    # (3) 异常输入 → [] 不抛
    class _Boom:
        @property
        def goals(self):
            raise RuntimeError("boom")
    try:
        c22b = dlb.deliberate(_Boom())
        ok(c22b == [], "T22 异常输入 → [] 不抛", str(c22b))
    except Exception as exc:
        ok(False, "T22 异常输入 → [] 不抛", "抛异常: " + repr(exc))

    print(f"\nP={P} F={F}")
    return 0 if F == 0 else 1
if __name__ == "__main__":
    sys.exit(run())
