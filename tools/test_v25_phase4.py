"""阶段4 T38 RED/GREEN：evaluation_results DAO + apply_evaluation 单事务原子性。

跑法：.venv\\Scripts\\python.exe tools\\test_v25_phase4.py

覆盖 T38 职责：
  1) add_evaluation_result / list_evaluation_results（带 user_id，E09）
  2) 非法 decision 归一化为 keep
  3) apply_evaluation 正常路径：写结果 + 策略状态随 decision 变更（同事务）
  4) apply_evaluation keep 分支：success +1 同事务
  5) apply_evaluation 原子性：注入第2步失败 → 整批 rollback，
     无孤儿 evaluation 行、策略状态不变（E04 硬约束）

T39/T40/T41 的 evaluation/reflection/learner 模块由后续任务接线，
不在本文件断言，避免跨任务依赖导致本任务无法独立 GREEN。
"""
from __future__ import annotations

import datetime
import os
import sys
import tempfile
import unittest.mock as mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix="v25-p4-")
os.environ["DB_PATH"] = os.path.join(_TMP, "t.db")

from brain import db  # noqa: E402

db.init_db()
P = F = 0


def ok(cond, label, detail=""):
    global P, F
    if cond:
        P += 1
        print("PASS ", label)
    else:
        F += 1
        print("FAIL ", label, "  ", detail)


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _seed_strategy(uid, name, version, status):
    """直接造一条策略行，绕过 upsert 的候选/证据逻辑。"""
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO strategies"
            "(user_id,name,text,evidence,success,failure,status,version,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (uid, name, "seed", 2, 0, 0, status, version, _now(), _now()))
        conn.commit()
    finally:
        conn.close()


def _strategy_states(uid, name):
    conn = db.connect()
    try:
        return [(int(r["version"]), r["status"]) for r in conn.execute(
            "SELECT version,status FROM strategies WHERE user_id=? AND name=? ORDER BY version",
            (uid, name)).fetchall()]
    finally:
        conn.close()


def _set_fail_trigger(on):
    """注入/移除第2步失败：BEFORE UPDATE strategies → RAISE(ABORT)。"""
    conn = db.connect()
    try:
        if on:
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS fail_strategy_update BEFORE UPDATE ON strategies "
                "BEGIN SELECT RAISE(ABORT, 'injected-failure'); END;")
        else:
            conn.execute("DROP TRIGGER IF EXISTS fail_strategy_update")
        conn.commit()
    finally:
        conn.close()


def _flush_trace():
    """单 worker 线程池屏障：等已提交的 trace 写全部落库，消除异步竞态。"""
    from brain.cognitive import trace as tmod
    tmod._EXEC.submit(lambda: None).result()


def _seed_lesson(uid, request_id, *, note="反馈用教训", weight=0.5):
    """造一条 lesson，供 learner / feedback 读写权重。"""
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO lessons(created_at,user_id,request_id,kind,note,verdict,"
            "weight,success,failure) VALUES(?,?,?,?,?,?,?,?,?)",
            (_now(), uid, request_id, "general", note, "", float(weight), 0, 0))
        conn.commit()
    finally:
        conn.close()


def _lesson_state(uid, request_id):
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT weight,success,failure FROM lessons WHERE user_id=? AND request_id=? "
            "ORDER BY id DESC LIMIT 1", (uid, request_id)).fetchone()
        return None if row is None else (float(row["weight"]),
                                         int(row["success"]), int(row["failure"]))
    finally:
        conn.close()


def _seed_feedback_ctx(tag, *, name="lesson_technical"):
    """一个反馈用例造齐 turn_meta / lesson / active+retired 两版 strategy。"""
    from brain import config_store
    config_store.init_store()
    uid = "u-fb-" + tag
    request_id = "req-fb-" + tag
    config_store.open_turn(request_id=request_id, user_id=uid,
                           session_id="s-fb", input_text="反馈回归测试")
    _seed_lesson(uid, request_id)
    _seed_strategy(uid, name, 1, "retired")
    _seed_strategy(uid, name, 2, "active")
    return uid, request_id, name


def _has_cn(text):
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


def run():
    # --- 1. DAO 写入 + 查询（带 user_id，E09）---
    uid = "u-e"
    rid = db.add_evaluation_result(user_id=uid, strategy="lesson_technical", window="10",
                                   before_score=0.8, after_score=0.5, decision="rollback")
    rows = db.list_evaluation_results(uid)
    ok(int(rid) > 0 and len(rows) == 1 and rows[0]["decision"] == "rollback",
       "评估结果写入并可查", str(rows))

    # --- 2. user_id 隔离（E09）---
    ok(len(db.list_evaluation_results("someone-else")) == 0,
       "评估结果按 user_id 隔离")

    # --- 3. 非法 decision 归一化 ---
    db.add_evaluation_result(user_id=uid, strategy="s2", window="10",
                             before_score=0.5, after_score=0.7, decision="???")
    ok(any(r["strategy"] == "s2" and r["decision"] == "keep"
           for r in db.list_evaluation_results(uid)),
       "非法 decision 归一化为 keep")

    # --- 4. apply_evaluation 正常路径：rollback + 同事务改策略状态 ---
    uid_tx = "u-tx"
    name = "lesson_technical"
    _seed_strategy(uid_tx, name, 1, "retired")
    _seed_strategy(uid_tx, name, 2, "active")
    before_count = len(db.list_evaluation_results(uid_tx))
    res = db.apply_evaluation(user_id=uid_tx, strategy=name, window="10",
                              before_score=0.8, after_score=0.5, decision="rollback")
    after_rows = db.list_evaluation_results(uid_tx)
    states = _strategy_states(uid_tx, name)
    ok(res["decision"] == "rollback" and len(after_rows) == before_count + 1,
       "apply_evaluation 写结果同事务完成", str(res))
    ok((1, "active") in states and (2, "retired") in states,
       "apply_evaluation rollback 同事务改策略状态", str(states))

    # --- 5. apply_evaluation keep 分支：success +1 同事务 ---
    uid_k = "u-keep"
    _seed_strategy(uid_k, name, 1, "candidate")
    conn = db.connect()
    s0 = conn.execute("SELECT success FROM strategies WHERE user_id=? AND name=?",
                      (uid_k, name)).fetchone()["success"]
    conn.close()
    db.apply_evaluation(user_id=uid_k, strategy=name, window="10",
                        before_score=0.5, after_score=0.7, decision="keep")
    conn = db.connect()
    s1 = conn.execute("SELECT success FROM strategies WHERE user_id=? AND name=?",
                      (uid_k, name)).fetchone()["success"]
    conn.close()
    ok(int(s1) == int(s0) + 1, "apply_evaluation keep 记一次成功", f"{s0}->{s1}")

    # --- 6. 原子性：注入第2步失败 → 整批 rollback ---
    uid_a = "u-atomic"
    _seed_strategy(uid_a, name, 1, "retired")
    _seed_strategy(uid_a, name, 2, "active")
    before_rows = len(db.list_evaluation_results(uid_a))
    before_states = _strategy_states(uid_a, name)
    _set_fail_trigger(True)
    try:
        try:
            db.apply_evaluation(user_id=uid_a, strategy=name, window="10",
                                before_score=0.8, after_score=0.5, decision="rollback")
            raised = False
        except Exception:
            raised = True
        ok(raised, "apply_evaluation 第2步失败时抛异常")
    finally:
        _set_fail_trigger(False)
    after_atomic_rows = db.list_evaluation_results(uid_a)
    after_atomic_states = _strategy_states(uid_a, name)
    ok(len(after_atomic_rows) == before_rows,
       "原子性：无孤儿 evaluation 行", f"{before_rows}->{len(after_atomic_rows)}")
    ok(after_atomic_states == before_states,
       "原子性：策略状态不变", f"{before_states}->{after_atomic_states}")

    # ==================== 阶段4 T39-T43 固化断言 ====================

    from brain.evolution import evaluation, reflection, learner, feedback
    from brain.cognitive import trace as tmod

    # --- T39 evaluation.evaluate_strategy：下降→rollback 且真回滚 ---
    uid_e = "u-t39"
    _seed_strategy(uid_e, "lesson_technical", 1, "retired")
    _seed_strategy(uid_e, "lesson_technical", 2, "active")
    er = evaluation.evaluate_strategy(user_id=uid_e, strategy="lesson_technical",
                                      before=0.8, after=0.5, runtime={}, trace_id=None)
    ok(er["decision"] == "rollback" and er["persisted"] is True,
       "T39 分数下降→decision=rollback 且落库", str(er))
    states_e = _strategy_states(uid_e, "lesson_technical")
    ok((1, "active") in states_e and (2, "retired") in states_e,
       "T39 rollback 真回滚策略版本", str(states_e))
    erows = db.list_evaluation_results(uid_e)
    ok(any(r["strategy"] == "lesson_technical" and r["decision"] == "rollback"
           and abs(float(r["after_score"]) - 0.5) < 1e-9 for r in erows),
       "T39 evaluation_results 留有 rollback 记录", str(erows))

    # 不降→keep（success+1）
    uid_k2 = "u-t39-keep"
    _seed_strategy(uid_k2, "lesson_technical", 1, "candidate")
    er2 = evaluation.evaluate_strategy(user_id=uid_k2, strategy="lesson_technical",
                                       before=0.5, after=0.7, runtime={}, trace_id=None)
    conn = db.connect()
    succ2 = int(conn.execute(
        "SELECT success FROM strategies WHERE user_id=? AND name=?",
        (uid_k2, "lesson_technical")).fetchone()["success"])
    conn.close()
    ok(er2["decision"] == "keep" and succ2 == 1,
       "T39 分数不降→keep 且 success+1", f"{er2} success={succ2}")

    # 唯一落库入口：三个拆分 DAO 置哨兵，零调用才算对
    uid_s = "u-t39-sentinel"
    _seed_strategy(uid_s, "lesson_technical", 1, "candidate")
    with mock.patch.object(db, "add_evaluation_result", side_effect=AssertionError("forbidden")), \
         mock.patch.object(db, "rollback_strategy", side_effect=AssertionError("forbidden")), \
         mock.patch.object(db, "record_strategy_outcome", side_effect=AssertionError("forbidden")):
        er3 = evaluation.evaluate_strategy(user_id=uid_s, strategy="lesson_technical",
                                           before=0.8, after=0.5, runtime={}, trace_id=None)
    ok(er3["persisted"] is True,
       "T39 只调 db.apply_evaluation（拆分 DAO 零调用）", str(er3))

    # DAO 异常降级不抛
    with mock.patch.object(db, "apply_evaluation", side_effect=RuntimeError("boom")):
        try:
            er4 = evaluation.evaluate_strategy(user_id="u-t39-err", strategy="lesson_technical",
                                               before=0.8, after=0.5, runtime={}, trace_id=None)
            raised4 = False
        except Exception:
            raised4 = True
    ok(not raised4 and er4["persisted"] is False,
       "T39 落库异常不抛、persisted=false", str(er4))

    # --- T40 reflection：score / reflect ---
    ok(abs(reflection.score(0.5, signal="negative") - 0.4) < 1e-9,
       "T40 score 负向降分")
    ok(abs(reflection.score(0.5, signal="positive") - 0.6) < 1e-9,
       "T40 score 正向上分")
    rneg = reflection.reflect(signal="negative", context={"verdict": True, "severity": 0.5})
    ok(rneg["weight"] > 0.5 and rneg["verdict"] == "needs_improvement",
       "T40 reflect 负向：权重升 + needs_improvement", str(rneg))
    rpos = reflection.reflect(signal="positive", context={"verdict": True, "severity": 0})
    ok(rpos["weight"] < 0.5 and rpos["verdict"] == "ok",
       "T40 reflect 正向：权重降 + ok", str(rpos))
    rnone = reflection.reflect(signal="???", context=None)
    ok(rnone["signal"] == "neutral" and rnone["weight"] == 0.5
       and rnone["verdict"] == "neutral" and rnone["kind"] == "general",
       "T40 缺上下文安全默认", str(rnone))
    with mock.patch.object(reflection, "_severity", side_effect=RuntimeError("boom")):
        try:
            rex = reflection.reflect(signal="negative", context={"severity": 0.9})
            raisedx = False
        except Exception:
            raisedx = True
    ok(not raisedx and rex["weight"] == 0.5 and rex["verdict"] == "neutral",
       "T40 内部异常降级不抛", str(rex))

    # --- T41 learner.apply_feedback：权重方向 + 中性 + 异常降级 ---
    uid_ln, rid_ln, _ = _seed_feedback_ctx("l-neg")
    w0 = _lesson_state(uid_ln, rid_ln)[0]
    lr_neg = learner.apply_feedback(user_id=uid_ln, signal="negative", request_id=rid_ln,
                                    context={}, runtime={}, trace_id=None)
    w1 = _lesson_state(uid_ln, rid_ln)[0]
    ok(lr_neg["changed"] == 1 and w1 < w0,
       "T41 负向反馈下调 lesson 权重", f"{w0}->{w1} {lr_neg}")

    uid_lp, rid_lp, _ = _seed_feedback_ctx("l-pos")
    w0p = _lesson_state(uid_lp, rid_lp)[0]
    lr_pos = learner.apply_feedback(user_id=uid_lp, signal="positive", request_id=rid_lp,
                                    context={}, runtime={}, trace_id=None)
    w1p = _lesson_state(uid_lp, rid_lp)[0]
    ok(lr_pos["changed"] == 1 and w1p > w0p,
       "T41 正向反馈上调 lesson 权重", f"{w0p}->{w1p} {lr_pos}")

    uid_lz, rid_lz, _ = _seed_feedback_ctx("l-neu")
    w0z = _lesson_state(uid_lz, rid_lz)[0]
    lr_neu = learner.apply_feedback(user_id=uid_lz, signal="neutral", request_id=rid_lz,
                                    context={}, runtime={}, trace_id=None)
    w1z = _lesson_state(uid_lz, rid_lz)[0]
    ok(lr_neu["changed"] == 0 and abs(w1z - w0z) < 1e-9,
       "T41 中性反馈不动权重", f"{w0z}->{w1z} {lr_neu}")

    uid_le, rid_le, _ = _seed_feedback_ctx("l-err")
    with mock.patch.object(db, "bump_lesson_outcome", side_effect=RuntimeError("boom")):
        try:
            lr_err = learner.apply_feedback(user_id=uid_le, signal="negative",
                                            request_id=rid_le, context={}, runtime={}, trace_id=None)
            raisedle = False
        except Exception:
            raisedle = True
    ok(not raisedle and lr_err["changed"] == 0,
       "T41 DAO 异常降级不抛", str(lr_err))

    # --- T42 feedback.on_verdict：正负反馈 + 双 trace + 异常隔离 + 既有键 ---
    uid_fp, rid_fp, _ = _seed_feedback_ctx("v-pos")
    sum_p = feedback.on_verdict(request_id=rid_fp, ok=True)
    _flush_trace()
    ok((sum_p.get("v25_feedback") or {}).get("signal") == "positive",
       "T42 正向 summary.v25_feedback.signal=positive", str(sum_p.get("v25_feedback")))
    ok(len(tmod.query(user_id=uid_fp, kind="feedback")) >= 1,
       "T42 正向写 feedback trace")
    six = ("request_id", "ok", "user_id", "facts_adjusted", "lesson", "strategy")
    ok(all(k in sum_p for k in six),
       "T42 既有 6 键都保留", str(sorted(sum_p)))

    uid_fn, rid_fn, name_fn = _seed_feedback_ctx("v-neg")
    sum_n = feedback.on_verdict(request_id=rid_fn, ok=False)
    _flush_trace()
    ev = sum_n.get("v25_evaluation") or {}
    ok(ev.get("decision") == "rollback",
       "T42 负向触发 evaluation decision=rollback", str(ev))
    ok(len(tmod.query(user_id=uid_fn, kind="verdict")) >= 1,
       "T42 负向写 verdict trace")
    ok(any(r["decision"] == "rollback" for r in db.list_evaluation_results(uid_fn)),
       "T42 负向 evaluation_results 落 rollback")

    # 异常隔离：learner 挂→evaluation 仍执行，on_verdict 不抛
    uid_il, rid_il, _ = _seed_feedback_ctx("v-iso-l")
    with mock.patch.object(learner, "apply_feedback", side_effect=RuntimeError("boom")):
        try:
            sum_il = feedback.on_verdict(request_id=rid_il, ok=False)
            raised_il = False
        except Exception:
            raised_il = True
    ok(not raised_il and (sum_il.get("v25_evaluation") or {}).get("decision") == "rollback",
       "T42 learner 异常不阻断 evaluation", str(sum_il.get("v25_evaluation")))

    # 异常隔离：evaluation 挂→learner 仍执行，on_verdict 不抛
    uid_ie, rid_ie, _ = _seed_feedback_ctx("v-iso-e")
    with mock.patch.object(evaluation, "evaluate_strategy", side_effect=RuntimeError("boom")):
        try:
            sum_ie = feedback.on_verdict(request_id=rid_ie, ok=False)
            raised_ie = False
        except Exception:
            raised_ie = True
    ok(not raised_ie and (sum_ie.get("v25_feedback") or {}).get("signal") == "negative",
       "T42 evaluation 异常不阻断 learner", str(sum_ie.get("v25_feedback")))

    # --- T43 trainer.status()：样本阈值 + 中文提示 ---
    from brain.services import trainer
    with mock.patch.object(trainer, "_count_lines") as m_count, \
         mock.patch.object(trainer, "_nvidia", return_value=[]), \
         mock.patch.object(trainer, "_torch_state", return_value={"installed": False, "cuda": False}), \
         mock.patch.object(trainer, "list_models", return_value={"gguf": [], "hf": [], "dirs": []}), \
         mock.patch.object(trainer, "install_options", return_value=[]), \
         mock.patch.object(trainer, "job", return_value={}), \
         mock.patch.object(trainer, "_dep", return_value=False):
        for n, expect in ((0, False), (199, False), (200, True)):
            trainer._STATUS_CACHE = None  # 绕过 30s TTL，逐档验证
            m_count.return_value = n
            st = trainer.status()
            ok(st["samples_ready"] is expect and st["sample_ready"] is expect
               and st["dataset"]["ready"] is expect,
               f"T43 {n} 条样本 samples_ready={expect}", str(st.get("reason")))
            if n == 0:
                ok(_has_cn(st["reason"]), "T43 0 样本 reason 含中文", st["reason"])

    print(f"\nP={P} F={F}")
    return 0 if F == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
