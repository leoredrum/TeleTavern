# Python Telegram Tavern — local Qwen-powered character bots

Telegram AI 酒馆 — 一个 Telegram Bot 入口、本地 Ollama + Qwen 模型驱动的角色扮演系统。

> **当前阶段：Phase 1 — Prompt Pipeline（已完成）**
>
> 已实现：Character Card v2/v3 加载、可插拔 Prompt Pipeline、Director Prompt 配置、
> 三个角色实例（Penelope / June / Aqua）、`/debug` 命令。
>
> 详见 [[docs/architecture.md]] 与 [[docs/phase1_delivery.md]]。
>
> 路线图：[[docs/roadmap]]（Phase 2/3/4/5 待启动 — 见 Obsidian vault `Projects/telegram-tavern/`）。

## 三个 Bot 实例

| 实例 | 角色卡 | Token 注入方式 |
|------|--------|-----------------|
| `sillytavern-telegram-bot` | Penelope (`Penelope3.png`) | `.env` |
| `june-telegram-bot`        | June (`June.png`)        | `.env` |
| `aqua-telegram-bot`        | Aqua (`Aqua.png`)        | `.env` |

> 这些实例继续跑在 `~/Projects/<bot-name>/`，本仓库是它们的**规范代码源**。
> 在这里改完代码，需要 `cp` 到对应实例目录后重启。

## 目录结构

```
telegramtavern/
├── bot.py                # PTB dispatcher + streaming reply
├── character_card.py     # ST V2/V3 parser + ExtendedCharacterCard
├── prompt_item.py        # PromptItem dataclass — pipeline atomic unit
├── director.py           # DirectorConfig + Director Prompt builder
├── pipeline.py           # PromptPipeline — 13-stage assembler
├── ollama_client.py      # Async Ollama /v1/chat/completions client
├── config.py             # Config dataclass from .env
├── db.py                 # SQLite session store
├── configs/
│   ├── bots/             # Per-bot overrides (planned, not yet used)
│   ├── models/           # Model profiles (planned, not yet used)
│   └── director/         # Director prompt presets (planned, not yet used)
├── characters/
│   ├── penelope/         # Drop Penelope3.png here
│   ├── june/             # Drop June.png here
│   └── aqua/             # Drop Aqua.png here
├── docs/                 # architecture.md + phase1_delivery.md (kept from old bot dir)
├── scripts/              # start.sh / stop.sh / restart.sh / healthcheck.sh
├── logs/                 # Runtime logs (gitignored)
├── data/                 # Runtime SQLite (gitignored)
├── .env.example          # Config template
├── .gitignore
└── requirements.txt
```

## 快速开始（新机器从零部署）

```bash
# 1. 克隆代码
git clone <this-repo> ~/Documents/telegramtavern
cd ~/Documents/telegramtavern

# 2. 创建 venv + 装依赖
python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

# 3. 配 .env
cp .env.example .env
# 编辑 .env：填 TELEGRAM_BOT_TOKEN、OLLAMA_MODEL、CHARACTER_PATH 等

# 4. 准备角色卡
# 把角色 PNG（带 chara tEXt chunk）放到 ./characters/<name>/
# 或用 V2/V3 JSON 文件

# 5. 拉模型
ollama pull <model-from-env>

# 6. 启动
./scripts/start.sh
# 或后台： ./scripts/start.sh --bg
```

## Prompt Pipeline 顺序

```
[0]  System Anchor           — "You are <char>, role-playing..."
[1]  Language Override       — "Always reply in <REPLY_LANGUAGE>..."
[2]  Director                — RP rules (no loop, advance narrative, ...)
[3]  Lore Before             — [Phase 2 - stub]
[4]  Character Defs          — Description / Personality / Scenario
[5]  Lore After              — [Phase 2 - stub]
[6]  Persona                 — User persona
[7]  Lore Examples           — [Phase 2 - stub]
[8]  Examples                — Example dialogues
[9]  Summary                 — [Phase 3 - stub]
[10] Chat History            — Real conversation
[11] Depth Injections        — In-chat depth prompts
[12] Post-History Instr.     — Last + highest priority
```

Phase 1 实际激活：0, 1, 2, 4, 6, 8, 10, 11, 12（其余位置预留）。

## 调试

```bash
# Telegram 内 /debug — 当前 user / chat 的 PromptItem 列表（含 token 估算）
/debug

# 环境变量 — 全局打开 DEBUG 级日志 + 每次组装后输出完整报告
DEBUG_PROMPT=1 ./scripts/start.sh
```

## 文档

- 架构设计：`docs/architecture.md`
- Phase 1 交付清单：`docs/phase1_delivery.md`
- Obsidian 项目 vault：`~/Library/Mobile Documents/iCloud~md~obsidian/Documents/CodingMarkdown/Projects/telegram-tavern/`

## License

Private / TBD