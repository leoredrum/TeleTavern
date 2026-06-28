#!/usr/bin/env python3
"""P0 测试：mock Telegram API，验证真实 DM Bot 最终发送函数 send_long_message
对 12000 字长文本：分段 >1、每段 <= SAFE_LIMIT、无截断、唯一尾标在最后一段、
message 模式与 bot+chat_id 模式均顺序送达无静默停止。

不依赖 telegram 库（纯 mock），可用系统 python3 运行。
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import telegram_splitter as TS  # noqa: E402


def build_text(approx=12000):
    """构造约 approx 字的人工文本（含唯一尾标）。"""
    lines = [
        "BEGIN_LONG_MESSAGE_TEST",
        "（mock 压力测试：验证 send_long_message 真实发送路径无截断）",
        "",
    ]
    i = 0
    while len("\n".join(lines)) < approx - 200:
        i += 1
        lines.append(
            "LINE %04d  影魔嘶吼挥出利爪，剑光与暗影碰撞，埃德蒙咬牙格挡，鲜血飞溅，"
            "你举起圣剑劈下，黑暗退散，走廊深处传来低沉回响，符文泛起冷光。" % i
        )
    lines += [
        "",
        "【战利品】",
        "- 影魔之心（传说）",
        "- 金币 +1200",
        "",
        "【状态栏】",
        "HP 62/80   AC 16   等级 7   经验 12400/16000",
        "",
        "END_LONG_MESSAGE_TEST_UNIQUE_TAIL_9f3c2a",
    ]
    return "\n".join(lines)


class FakePlaceholder:
    """流式占位 Message：edit_text 记录每次编辑。"""
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, **kw):
        self.edits.append(text)
        return self


class FakeMessage:
    """update.message：reply_text 顺序记录发送的每一段。"""
    def __init__(self):
        self.replies = []
        self._fail_indices = set()  # 模拟某段发送失败的索引（测试回退/继续）

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return self


class FakeBot:
    """telegram.Bot：bot+chat_id 模式下 send_message 顺序记录。"""
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id=None, text=None, **kw):
        self.sent.append(text)
        return self


class TestRealSendPathLongMessage(unittest.TestCase):

    def setUp(self):
        self.text = build_text(12000)
        self.assertGreater(len(self.text), 8000, "测试文本必须够长（>8000）")

    def test_message_mode_no_truncation(self):
        """message 模式：第一段复用 placeholder（edit_text），后续段顺序 reply_text。"""
        import asyncio
        ph = FakePlaceholder()
        msg = FakeMessage()
        diag = asyncio.run(TS.send_long_message(message=msg, text=self.text, first_message=ph))

        # 多段
        self.assertGreater(diag["segments"], 1, "12000 字必须分多段")
        # 每段 <= SAFE_LIMIT
        for length in diag["lengths"]:
            self.assertLessEqual(length, TS.SAFE_LIMIT, "每段必须 <= SAFE_LIMIT")
        # 全部送达
        self.assertTrue(all(r == "ok" for r in diag["results"]), "results 全 ok: %s" % diag["results"])
        # 无截断：第一段 == placeholder 最后一次 edit_text
        self.assertGreater(len(ph.edits), 0, "placeholder 必须被 edit_text")
        self.assertEqual(ph.edits[-1], TS.split_text(self.text)[0], "placeholder edit 为第一段")
        # 后续段顺序 == reply_text 调用
        self.assertEqual(msg.replies, TS.split_text(self.text)[1:], "后续段顺序发送")
        # 尾标在最后一段（= 最后一条 reply 或 placeholder edit）
        tail = "END_LONG_MESSAGE_TEST_UNIQUE_TAIL_9f3c2a"
        last_segment = msg.replies[-1] if msg.replies else ph.edits[-1]
        self.assertIn(tail, last_segment, "唯一尾标必须在最后一段")

    def test_bot_chat_id_mode_no_truncation(self):
        """bot+chat_id 模式（probe / 命令路径）：全部走 bot.send_message，无截断。"""
        bot = FakeBot()
        import asyncio
        diag = asyncio.run(TS.send_long_message(bot=bot, chat_id=650773030, text=self.text))

        self.assertGreater(diag["segments"], 1)
        for length in diag["lengths"]:
            self.assertLessEqual(length, TS.SAFE_LIMIT)
        self.assertTrue(all(r == "ok" for r in diag["results"]), diag["results"])
        self.assertEqual(bot.sent, TS.split_text(self.text), "bot 模式 send_message == 全部分段")
        tail = "END_LONG_MESSAGE_TEST_UNIQUE_TAIL_9f3c2a"
        self.assertIn(tail, bot.sent[-1], "唯一尾标在最后一段")

    def test_no_final_text_truncation(self):
        """禁止截断 final_text：拼接所有发出段，必须包含唯一尾标（证明内容未被丢弃）。"""
        ph = FakePlaceholder()
        msg = FakeMessage()
        import asyncio
        asyncio.run(TS.send_long_message(message=msg, text=self.text, first_message=ph))

        assembled = ph.edits[-1] + "\n" + "\n".join(msg.replies)
        tail = "END_LONG_MESSAGE_TEST_UNIQUE_TAIL_9f3c2a"
        self.assertIn(tail, assembled, "拼接发出段必须含尾标 → 无截断")
        self.assertIn("BEGIN_LONG_MESSAGE_TEST", assembled, "拼接必须含开头")

    def test_short_message_single_segment(self):
        """短消息：单段直发，placeholder edit_text。"""
        ph = FakePlaceholder()
        msg = FakeMessage()
        import asyncio
        diag = asyncio.run(TS.send_long_message(message=msg, text="短消息测试", first_message=ph))
        self.assertEqual(diag["segments"], 1)
        self.assertEqual(len(ph.edits), 1)
        self.assertEqual(msg.replies, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
