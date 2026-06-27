"""Drive V2 bot's _generate_and_reply directly with a fake Telegram Update."""
import asyncio
import logging
import os
import sys
from pathlib import Path

# Set up paths
HERE = Path(__file__).resolve().parent.parent
TELEGRAM_DIR = HERE / "telegram-bot"
sys.path.insert(0, str(TELEGRAM_DIR))

# Load env
from dotenv import load_dotenv
load_dotenv(TELEGRAM_DIR / ".env")

# Patch PTB to log at DEBUG
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)-5s %(name)s %(message)s')

# Import bot module
import bot as botmod


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class FakeMessage:
    def __init__(self, text, chat_id):
        self.text = text
        self.chat = FakeChat(chat_id)
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(("reply_text", text, kw))
        print(f"  [bot → telegram] reply_text({len(text)} chars): {text[:80]!r}")
        return self  # placeholder

    async def _edit(call_id):
        pass


class FakeUpdate:
    def __init__(self, text, chat_id=12345):
        self.effective_message = FakeMessage(text, chat_id)
        self.effective_chat = FakeChat(chat_id)


async def main():
    print("=== simulating Telegram '你好' message ===")
    update = FakeUpdate("你好", chat_id=999999)
    await botmod._generate_and_reply(update, "你好")

    print(f"\n=== bot sent {len(update.effective_message.replies)} messages ===")
    for i, (op, txt, kw) in enumerate(update.effective_message.replies):
        print(f"  [{i}] {op}({len(txt)} chars): {txt[:120]!r}")


if __name__ == "__main__":
    asyncio.run(main())