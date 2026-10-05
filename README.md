# Telegram Tavern V3

本地 Ollama 驱动的 Telegram 角色扮演 / TRPG 引擎，打包为 macOS 桌面 App（主窗口：总览 / 本地聊天 / Bots / 角色卡 / 角色卡商店 / 世界书 / 模型 / 日志）。
不依赖 SillyTavern 运行时，但**角色卡（V2/V3 PNG）和世界书（World Info JSON）格式与 SillyTavern 完全兼容**，ST 可以继续当编辑器用。

## 它能做什么

- **对话模式 `dialogue`**：一个 bot 挂任意多张角色卡，`/character` 按钮切换，每个 Telegram 聊天绑定自己的角色；可选 Director 反套路规则与防卡壳剧情引擎。
- **RPG 模式 `rpg`**：程序持有的权威世界状态（地点、敌人、NPC、存活、战斗），每轮注入到最新消息前；本地小模型从叙述中抽取新实体（含别名）、死亡、地点变化；校验到「死者复活 / 场景跳变」会在下一轮强制纠正。可选 D&D 规则引擎与导演场景控制（DungeonMaster）。每局自动记录，可导出原始日志 / 剧本 / 小说素材。
- **世界书引擎**：ST 语义——常驻条目、主次关键词四种逻辑、正则键、扫描深度、概率、递归、token 预算、七种注入位置。角色卡内嵌书自动识别，`worlds/` 里的书可叠加。
- **本地聊天**：在 App 窗口里直接和任意 bot 对话（对话 bot 可选角色，RPG bot 走同一套世界状态与存档），不经过 Telegram，与 Telegram 对话互相独立。
- **角色卡商店**：App 内嵌 aicharactercards.com 浏览器窗口，登录、浏览、下载；下载到「下载」文件夹的角色卡 PNG 自动识别并导入 `characters/`。
- **多 bot 单进程**：`bots/*.yaml` 一个文件一个 bot。

## 安装使用（App）

1. 安装 [Ollama](https://ollama.com/download)，拉取模型（默认 `fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest`，RPG 抽取用 `qwen3:14b`）。App 菜单里也能检查和拉取。
2. 打开 `TelegramTavern.app`。首次启动会创建数据目录
   `~/Library/Application Support/TelegramTavern/`：
   ```
   .env            TG_TOKEN_XXX=...        ← 从 @BotFather 拿的 token
   bots/*.yaml     每个 bot 一个文件
   characters/     放角色卡 PNG
   worlds/         放世界书 JSON
   data/ logs/     运行数据
   ```
3. 在窗口里操作：「角色卡」导入 PNG、「世界书」导入 JSON、「Bots」新建并勾选角色卡 / 世界书、粘贴 Telegram token、「模型」检查或拉取 Ollama 模型，然后「总览」里启动或重启引擎。所有配置也可以直接改数据目录里的文件。

`bots/example.yaml`（最小）：

```yaml
name: my-bot
enabled: true
mode: dialogue          # 或 rpg
token_env: TG_TOKEN_MYBOT
characters: [MyCharacter.png]
worlds: []              # 可选，叠加在卡内嵌世界书之上
model: fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest
reply_language: zh-CN
```

RPG 模式常用字段：`extract_model`、`extract`、`rules`（D&D 规则）、`scenes`（导演场景）、`newgame_prompt`、`continue_prompt`、`language_override`、`translation_table`。全部字段见 `tavern/config.py` 的 `BotConfig`。

## 从源码运行 / 开发

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements-v3.txt
export TAVERN_DATA_DIR=$PWD/data-v3        # 开发用数据目录（已 gitignore）
./venv/bin/python -m tavern init            # 建目录与示例
./venv/bin/python -m tavern check           # 检查 Ollama / 模型 / 每个 bot 的配置
./venv/bin/python -m tavern run             # 前台运行全部 bot
./venv/bin/python app/window.py             # 从源码跑桌面 App（TAVERN_AUTOSTART=0 不自动启 bot）
./venv/bin/python app/menubar.py            # 备选：纯菜单栏形态
./scripts/build_app.sh --install            # 打包并安装到 /Applications
```

测试（离线，不需要 Telegram；部分需要本地 Ollama）：

```bash
./venv/bin/python tests/test_worldinfo.py
./venv/bin/python tests/test_pipeline_v3.py
./venv/bin/python tests/test_rpg_v3.py
```

## 代码结构

```
tavern/
  worldinfo.py        ST 兼容世界书引擎
  engine.py           CharacterRuntime：角色卡 + 世界书 + Pipeline + Ollama
  config.py           数据目录与 bots/*.yaml
  manager.py          多 bot 引擎；cli.py / __main__.py 命令行
  modes/dialogue.py   对话模式（角色切换、绑定）
  modes/rpg.py        RPG 模式（世界状态、抽取、规则、导演、存档导出）
  rpg/                game_state / state_extractor / rpg_engine / director_engine / sessions
tavern/local.py       本地聊天适配器（LocalUpdate 复用两种模式的处理逻辑）
app/window.py + ui.html   pywebview 桌面 App；app/menubar.py 菜单栏备选；app/TelegramTavern.spec 打包
pipeline.py character_card.py director.py story_engine.py ollama_client.py   V1 核心，V3 复用
docs/V3_PLAN.md docs/V3_HANDOFF.md   计划与交接
```

## 历史

- V1（`_archive_2026-06-30/`、`penelope/`）：单角色 Prompt Pipeline 原型。
- V2（`~/Documents/SillyTavern/connector/`）：SillyTavern + 无头浏览器桥接，已被 V3 取代。
