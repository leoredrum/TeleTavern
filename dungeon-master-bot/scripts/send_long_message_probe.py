#!/usr/bin/env python3
"""P0 probe：构造 12000 字人工文本，走真实 DM Bot 同一个最终发送函数 send_long_message
发到真实 Telegram，验证发送层是否完整送达（唯一尾标必须出现在 Telegram 最后一段）。

不含 token / chat_id 硬编码：
  - token 从 connector/.env 的 TELEGRAM_BOT_TOKEN 读取（绝不打印）。
  - chat_id 从 argv[1] 或环境变量 DM_BOT_CHAT_ID 读取。

用法（venv python）：
  venv/bin/python dungeon-master-bot/scripts/send_long_message_probe.py <chat_id>
"""
import os
import sys
import asyncio
from pathlib import Path

# telegram_splitter 与 bot.py 同目录（dungeon-master-bot/）；probe 在其下 scripts/
_DM_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_DM_DIR))
import telegram_splitter as TS  # noqa: E402


def load_token():
    """读 TELEGRAM_BOT_TOKEN（先 dungeon-master-bot/.env，再 connector/.env）；绝不打印或外泄。"""
    for env in (_DM_DIR / ".env", _DM_DIR.parent / ".env"):
        if env.exists():
            for line in env.read_text().splitlines():
                line = line.strip()
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def build_test_text():
    """构造约 12000 字人工文本（BEGIN / LINE 编号 / Boss 战 / 战利品 / Inventory / 状态栏 / 尾标）。"""
    lines = [
        "BEGIN_LONG_MESSAGE_TEST",
        "（影魔战斗结算·压力测试：验证真实 Telegram 发送层是否完整送达 12000 字）",
        "",
    ]
    # Boss 战正文：约 100 行，每行约 90 字
    for i in range(1, 101):
        lines.append(
            "LINE %04d  影魔嘶吼着挥出利爪，剑光与暗影碰撞，埃德蒙咬牙格挡，鲜血飞溅。"
            "你举起圣剑劈下，黑暗退散，走廊深处传来回响。" % i
        )
    lines += [
        "",
        "【战利品】",
        "- 影魔之心（传说级消耗材料）",
        "- 暗影碎片 x5",
        "- 金币 +1200",
        "- 经验 +4500",
        "",
        "【背包 / Inventory】",
        "- 长剑 +2",
        "- 皮甲 +1",
        "- 治疗药水 x3",
        "- 圣银匕首",
        "- 影魔之心 x1",
        "",
        "【状态栏】",
        "姓名：埃德蒙   职业：圣骑士   等级：7",
        "HP 62/80   MP 18/40   AC 16",
        "经验 12400/16000   金币 2350",
        "",
    ]
    # 补足到约 11500+ 字
    pad = 0
    while len("\n".join(lines)) < 11500:
        pad += 1
        lines.append(
            "LINE %04d  走廊尽头火光摇曳，远处传来低沉吟唱，墙壁上的符文泛起冷光，"
            "一条隐秘线索浮现：通往王座密室的阶梯在第七根石柱之后。" % (100 + pad)
        )
    lines += ["", "END_LONG_MESSAGE_TEST_UNIQUE_TAIL_9f3c2a"]
    return "\n".join(lines)


async def main():
    token = load_token()
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN 未找到（connector/.env 或环境变量）")
        sys.exit(2)
    chat_id = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DM_BOT_CHAT_ID", "")
    if not chat_id:
        print("ERROR: chat_id 未提供（argv[1] 或 DM_BOT_CHAT_ID 环境变量）")
        sys.exit(2)

    import telegram  # 仅在真实发送时才 import（依赖 venv）
    bot = telegram.Bot(token=token)

    text = build_test_text()
    print("PROBE text_len=%d chat_id=%s" % (len(text), chat_id))
    # 关键：走真实 send_long_message（bot + chat_id 模式，与 bot.py 命令路径同一函数）
    diag = await TS.send_long_message(bot=bot, chat_id=chat_id, text=text)
    print("PROBE DIAG input_len=%d segments=%d lengths=%s results=%s exceptions=%s"
          % (diag["input_len"], diag["segments"], diag["lengths"], diag["results"],
             diag["exceptions"][:4]))

    # 尾标必须出现在 split_text 的最后一段（= 真实 Telegram 最后收到的那条消息）
    chunks = TS.split_text(text)
    tail_ok = "END_LONG_MESSAGE_TEST_UNIQUE_TAIL_9f3c2a" in chunks[-1]
    all_ok = all(r == "ok" for r in diag["results"])
    print("TAIL_IN_LAST_SEGMENT=%s ALL_SEGMENTS_OK=%s" % (tail_ok, all_ok))
    print("VERDICT %s" % ("PASS" if (diag["segments"] > 1 and tail_ok and all_ok) else "FAIL"))
    print("请到 Telegram 确认最后一条消息包含 END_LONG_MESSAGE_TEST_UNIQUE_TAIL_9f3c2a")


if __name__ == "__main__":
    asyncio.run(main())
