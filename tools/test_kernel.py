"""内核自测：验的是「程序思考、大模型表达」这条原则是否真的立住。

跑法（在 companion 目录下）：
    .venv\\Scripts\\python.exe tools\\test_kernel.py

每条断言对应一条原则，不是凑数的：
  - 身份类问题必须判成身份，且绝不联网；
  - 知道姓名就答姓名，不知道就只答角色，不许拿自己的名字顶；
  - 画像里「改名叫小智」不算他的名字；
  - 问句不许当事实；
  - 表达越界必须被拦下并回退。
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brain.kernel import WorldSnapshot, run_turn  # noqa: E402
from brain.kernel.check import check  # noqa: E402

BASE = WorldSnapshot(assistant_name="小智", user_name="", profile_text="", facts=())


class TestPerception(unittest.TestCase):
    def test_self_ask_is_identity_not_chat(self):
        out = run_turn("我是谁？", BASE)
        self.assertEqual(out.plan.speech_act, "identity_user")

    def test_assistant_ask_is_identity_self(self):
        out = run_turn("你叫什么？", BASE)
        self.assertEqual(out.plan.speech_act, "identity_self")
        self.assertIn("小智", out.final_text)

    def test_identity_never_triggers_action(self):
        """「我是谁」带动手词也不许动手——这是历史翻车点。"""
        out = run_turn("我是谁？帮我查一下", BASE)
        self.assertFalse(out.awaiting_action)
        self.assertFalse(any(a.kind != "none" for a in out.plan.actions))


class TestIdentityAnswer(unittest.TestCase):
    def test_unknown_name_answers_role_only(self):
        out = run_turn("我是谁？", BASE)
        self.assertIn("用户", out.final_text)
        # 关键：不许把自己的名字当成他的名字
        self.assertNotIn("小智", out.final_text)
        self.assertIn("小智", out.plan.must_not)
        self.assertEqual(out.plan.uncertainty.value, "unknown")

    def test_known_name_answers_name(self):
        snap = WorldSnapshot(assistant_name="小智", user_name="阿伟", facts=())
        out = run_turn("我是谁？", snap)
        self.assertIn("阿伟", out.final_text)
        self.assertEqual(out.plan.uncertainty.value, "certain")

    def test_profile_rename_trap_not_taken_as_user_name(self):
        """「称呼：改名叫小志或小智」是我改名的记录，不是他叫小志。"""
        snap = WorldSnapshot(assistant_name="小智", user_name="",
                             profile_text="称呼：改名叫小志或小智\n生活：有点累")
        out = run_turn("我是谁？", snap)
        self.assertNotIn("小志", out.final_text)
        self.assertNotIn("小智", out.final_text)

    def test_profile_explicit_name_is_used_but_marked_inferred(self):
        snap = WorldSnapshot(assistant_name="小智", user_name="",
                             profile_text="他叫阿伟，做运维")
        out = run_turn("我是谁？", snap)
        self.assertIn("阿伟", out.final_text)
        self.assertEqual(out.plan.uncertainty.value, "inferred")


class TestMemoryShape(unittest.TestCase):
    def test_question_facts_are_dropped(self):
        snap = WorldSnapshot(
            assistant_name="小智", user_name="",
            facts=({"id": "f1", "text": "我问的是我是谁？"},
                   {"id": "f2", "text": "我在问我是谁"},
                   {"id": "f3", "text": "他有一张 2080Ti 显卡，之前跑过量化"}),)
        out = run_turn("你还记得我上次说的显卡吗？", snap)
        self.assertEqual(out.plan.speech_act, "recall")
        self.assertIn("显卡", out.final_text)

    def test_recall_miss_says_unknown(self):
        out = run_turn("你还记得我上次说的显卡吗？", BASE)
        self.assertEqual(out.plan.speech_act, "refuse_unknown")
        self.assertIn("没", out.final_text)


class TestActionPermission(unittest.TestCase):
    def test_world_task_issues_granted_action(self):
        out = run_turn("帮我查下广元天气", BASE)
        self.assertTrue(out.awaiting_action)
        act = [a for a in out.plan.actions if a.allowed]
        self.assertTrue(act)
        self.assertEqual(act[0].kind, "web_search")
        self.assertEqual(out.final_text, "")


class TestExpressionCheck(unittest.TestCase):
    def test_self_name_in_user_identity_is_blocked(self):
        out = run_turn("我是谁？", BASE)
        got = check(out.plan, "我叫小智。")
        self.assertFalse(got.ok)
        self.assertTrue(any("forbidden" in f for f in got.failures))

    def test_invented_name_is_blocked(self):
        out = run_turn("我是谁？", BASE)
        got = check(out.plan, "你叫王小明。")
        self.assertFalse(got.ok)
        self.assertTrue(any("invented_name" in f for f in got.failures))

    def test_template_output_passes(self):
        out = run_turn("我是谁？", BASE)
        self.assertTrue(check(out.plan, out.final_text).ok)

    def test_bad_model_output_falls_back_to_template(self):
        """模型胡说 → 校验不过 → 回退模板，最终仍然答对。"""
        snap = BaseWithHistory()
        bad = lambda prompt: "你叫王小明，我记得很清楚。"  # noqa: E731
        out = run_turn("我是谁？", snap, model_fn=bad)
        self.assertNotIn("王小明", out.final_text)
        self.assertIn("用户", out.final_text)


def BaseWithHistory() -> WorldSnapshot:
    return WorldSnapshot(assistant_name="小智", user_name="", facts=(), has_history=True)


class TestRobustness(unittest.TestCase):
    def test_empty_snapshot_never_crashes(self):
        out = run_turn("", WorldSnapshot())
        self.assertTrue(out.final_text)

    def test_every_step_has_event_and_reason(self):
        out = run_turn("我是谁？", BASE)
        systems = [e["system"] for e in out.events]
        self.assertIn("perception", systems)
        self.assertIn("memory", systems)
        self.assertIn("judge", systems)
        self.assertIn("express", systems)
        # 每个事件都要有 because：说不清凭什么，就不该亮
        for e in out.events:
            self.assertTrue(e["because"], f"事件缺 because: {e}")

    def test_identity_event_has_real_sources(self):
        out = run_turn("我是谁？", BASE)
        ident = [e for e in out.events if e["action"] == "identity_lookup"]
        self.assertTrue(ident)
        self.assertTrue(ident[0]["sources"])


class TestModelOnlyWhenNeeded(unittest.TestCase):
    def test_identity_does_not_call_model(self):
        calls = {"n": 0}

        def spy(prompt: str) -> str:
            calls["n"] += 1
            return "随便说一句"

        out = run_turn("我是谁？", BASE, model_fn=spy)
        self.assertEqual(calls["n"], 0)          # 身份类根本不需要模型
        self.assertEqual(out.render.by, "template")

    def test_chat_uses_model(self):
        calls = {"n": 0}

        def spy(prompt: str) -> str:
            calls["n"] += 1
            self.assertIn("必须表达", prompt)     # 给模型的是表达任务，不是自由作答
            return "嗯，听着挺好的。"

        out = run_turn("今天过得怎么样", BASE, model_fn=spy)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(out.render.by, "model")


if __name__ == "__main__":
    unittest.main(verbosity=2)
