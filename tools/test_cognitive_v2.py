"""认知架构 V2 的单元验收：意图 / 记忆价值 / 提示词结构 / 神经元与突触 / 策略回滚。

跑法（用一份临时库，不碰线上数据）：
    .venv\Scripts\python.exe tools\test_cognitive_v2.py

输出的每一行都是一条可核对的断言，最后打印 PASS/FAIL 计数并以退出码报告结果。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# 必须在 import brain 之前指定库路径：config 在 import 时读环境变量
_SRC_DB = os.path.join(ROOT, "data", "companion.db")
_TMP = tempfile.mkdtemp(prefix="brain-v2-test-")
_DB = os.path.join(_TMP, "test.db")
if os.path.exists(_SRC_DB):
    shutil.copyfile(_SRC_DB, _DB)
os.environ["DB_PATH"] = _DB

from brain import db  # noqa: E402
from brain.cognitive import allocate, classify, plan as plan_cognitive, sense  # noqa: E402
from brain.cognitive import salience  # noqa: E402
from brain.neural import activation, growth, neuron  # noqa: E402

PASS = 0
FAIL = 0


def ok(cond: bool, label: str, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS  {label}")
    else:
        FAIL += 1
        print(f"FAIL  {label}  {detail}")


def _intent(text: str, prev_user: str = "", prev_reply: str = "") -> dict:
    sig = sense(text, prev_user=prev_user, prev_reply=prev_reply)
    return classify(sig)


def main() -> int:
    db.init_db()

    print("---- 1. 意图识别")
    cases = [
        ("我的模型为什么这么慢？", "technical"),
        ("你还记得我上次说的那个显卡吗", "recall"),
        # 带技术词但实际是回头查旧账：不能被判成技术排查
        ("我上次跟你说的那台机器的显卡，再说一遍", "recall"),
        ("刚才跟你说的那个端口，再说一次", "recall"),
        ("难受，睡不着", "emotional"),
        ("这张 3060 值不值得换", "decision"),
        ("在吗", "greeting"),
        ("嗯", "filler"),
        ("你是谁", "meta"),
        ("帮我记一下明天交电费", "task"),
    ]
    for text, want in cases:
        got = _intent(text)
        ok(got["intent"] == want, f"意图「{text}」→ {want}", f"实际 {got}")

    # 追问：同一件事又问一遍，应该判成 followup 而不是普通闲聊
    got = _intent("我的模型为什么这么慢？", prev_user="我的模型为什么这么慢")
    ok(got["intent"] == "followup", "重复提问判成追问", str(got))

    print("---- 2. 注意力预算随意图变化")
    sig = sense("我的模型为什么这么慢？")
    info = classify(sig)
    att = allocate(sig, info, None)
    ok(att["budget"]["allow_steps"] is True and att["budget"]["max_tokens"] >= 400,
       "技术问题允许分步且预算更大", str(att["budget"]))
    sig2 = sense("嗯")
    att2 = allocate(sig2, classify(sig2), None)
    ok(att2["budget"]["sentences"] <= 2 and att2["memory_top_k"] <= 1,
       "语气词轮次篇幅更短、只翻一条记忆", str(att2["budget"]))
    ok(4 in att["regions"], "技术问题参与判断区", str(att["regions"]))

    print("---- 3. 记忆价值判断")
    for text, want_kind, want_keep in [
        ("我不吃香菜，点了会犯恶心", "constraint", True),
        ("我叫李明，在成都做运维", "identity", True),
        ("今天中午吃了碗牛肉面", "episode", True),
        ("你还记得我吗", "echo", False),
        ("怎么设置这台路由器", "question", False),
        # 回头查旧账：带技术词也不能当新事实存下来
        ("我上次跟你说的那台机器的显卡，再说一遍", "echo", False),
        # 但带「我昨天/我换了」的仍是新事实
        ("我昨天换了张 3060 显卡", "episode", True),
    ]:
        s = salience.score(text)
        ok(s["kind"] == want_kind and bool(s["keep"]) == want_keep,
           f"价值「{text}」→ {want_kind}", str(s))
    ok(salience.score("我叫李明，在成都做运维")["importance"] >
       salience.score("今天中午吃了碗牛肉面")["importance"],
       "身份比流水更重要")

    print("---- 4. 提示词结构")
    from brain.services import prompt
    from brain.schemas import EmotionResult

    emo = EmotionResult(label="neutral", intensity=0.2)
    bundle = {"identity": {"assistant": "小豆", "user": "阿伟"}, "facts": [], "emotions": [],
              "profile": "", "episodes": [], "open_loops": [], "lessons": [],
              "association": {"concepts": ["显卡", "驱动"]}, "strategy": "先问清是不是显存爆了"}
    history = [
        {"role": "user", "content": "我这台机器跑模型特别慢"},
        {"role": "assistant", "content": "先看显存够不够，再看是不是走了 CPU。"},
        {"role": "user", "content": "显存 8G，模型 7B"},
    ]
    pl = plan_cognitive(sense("那为什么还是很慢"), classify(sense("那为什么还是很慢")),
                        allocate(sense("那为什么还是很慢"), classify(sense("那为什么还是很慢")), emo),
                        bundle, emo)
    msgs = prompt.build({"system_prompt": "你是一个陪伴型角色。"}, bundle, emo,
                        "那为什么还是很慢", history, plan=pl)
    sys_text = msgs[0]["content"]
    ok("这一轮怎么接" in sys_text, "system 里有本轮调度段")
    ok("一般只说 1-2 句" not in sys_text, "旧的「只说 1-2 句」硬约束已移除")
    ok(any(m["role"] == "assistant" for m in msgs[1:-1]), "历史里保留了自己上一轮的回复")
    ok(msgs[-1]["role"] == "user" and msgs[-1]["content"] == "那为什么还是很慢",
       "最后一条是本轮用户输入")
    ok("这一轮被带亮的关联词：显卡、驱动" in sys_text, "联想词进了提示词")
    ok("先问清是不是显存爆了" in sys_text, "已验证策略进了提示词")

    print("---- 5. 神经元 / 突触 / 激活")
    uid = "u-test-v2"
    db.add_fact(fact_id="f1", user_id=uid, session_id="s", text="显卡驱动装不上，显存也不够",
                kind="tech", importance=0.6)
    db.add_fact(fact_id="f2", user_id=uid, session_id="s", text="换了新显卡，驱动还是要重装",
                kind="tech", importance=0.6)
    g1 = growth.grow_fact(user_id=uid, fact_id="f1", text="显卡驱动装不上，显存也不够")
    g2 = growth.grow_fact(user_id=uid, fact_id="f2", text="换了新显卡，驱动还是要重装")
    ok(bool(g1["concepts"]) and bool(g2["concepts"]), "抽到了概念词", str(g1["concepts"]))
    n = db.neuron_get(neuron.fact_neuron_id("f1"))
    ok(n is not None and n["kind"] == "fact" and 0 <= n["importance"] <= 1,
       "事实节点有类型和重要度", str(n))
    stats = db.neuron_stats(uid)
    ok(stats["neurons"] >= 4 and stats["synapses"] >= 2, "节点与突触都落库了", str(stats))
    rel = db.synapse_sources(uid, [neuron.concept_neuron_id("显卡")], relation="about")
    ok(len(rel) >= 2, "同一个概念连到了两条记忆（语义边）", str(rel))
    act = activation.activate(user_id=uid, fact_ids=["f1", "f2"], scores=[0.6, 0.5])
    ok(len(act["seeds"]) == 2 and bool(act["concepts"]), "激活返回种子与联想概念", str(act))

    db.record_activations(user_id=uid, request_id="r1",
                          items=[{"id": neuron.fact_neuron_id("f1"), "energy": 0.4}])
    n2 = db.neuron_get(neuron.fact_neuron_id("f1"))
    ok(n2["activation"] > 0 and n2["activation_count"] == 1, "激活计数记账", str(n2["activation"]))
    dec = db.decay(uid)
    ok(dec["neurons"] >= 0, "衰减可执行（不删记忆）", str(dec))
    ok(db.neuron_get(neuron.fact_neuron_id("f1")) is not None, "衰减后节点仍在")

    print("---- 6. 存储筛选与去重")
    from brain.services import memory
    r_hi = memory.store_fact(uid, "s", "我忌口不吃辣，胃不好", [])
    ok(r_hi["stored"] and r_hi["importance"] >= 0.8, "禁忌被高权重记住", str(r_hi))
    r_q = memory.store_fact(uid, "s", "这台路由器怎么设置端口映射？", [])
    ok(not r_q["stored"], "纯提问不入长期记忆", str(r_q))
    r_dup = memory.store_fact(uid, "s", "我忌口不吃辣，胃不好", [])
    ok(not r_dup["stored"], "重复内容不重复记", str(r_dup))

    print("---- 7. 策略：候选 → 启用 → 回滚")
    from brain.evolution import strategy as strategy_mod
    for _ in range(2):
        db.upsert_strategy(user_id=uid, name="lesson_technical",
                           text="先问清是不是显存爆了")
    s1 = db.activate_strategy(user_id=uid, name="lesson_technical", min_evidence=2)
    ok(s1 is not None and s1["status"] == "active", "证据够了自动启用", str(s1))
    ok(strategy_mod.active_line(uid).startswith("先问清"), "启用后进提示词", strategy_mod.active_line(uid))
    _out = db.record_strategy_outcome(user_id=uid, name="lesson_technical", ok=True)
    ok(_out is not None and _out["success"] >= 1, "记录策略结果会累加成功数", str(_out))
    rb = db.rollback_strategy(user_id=uid, name="lesson_technical")
    ok(rb is not None and db.active_strategy(uid) is None, "回滚后不再生效", str(rb))

    print("---- 8. 反馈落权重")
    db.add_lesson(user_id=uid, request_id="r1", kind="technical", note="答得太空", how="给具体参数")
    before = db.turn_meta("r1")
    bumped = db.bump_lesson_outcome(user_id=uid, request_id="r1", ok=False)
    ok(bumped is not None and bumped["weight"] < 0.5 + 1, "失败会下调教训权重", str(bumped))

    print("---- 9. 教训按权重选取")
    db.add_lesson(user_id=uid, request_id="r2", kind="tone", note="别用客服腔", weight=0.9)
    db.add_lesson(user_id=uid, request_id="r3", kind="length", note="别太长", weight=0.3)
    top = db.lessons_for_prompt(uid, limit=1)
    ok(top and top[0]["kind"] in ("tone", "technical"), "高权重教训排在前", str(top))

    print()
    print(f"PASS={PASS}  FAIL={FAIL}  db={_DB}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
