# Telegram Tavern V3 — 独立引擎 + Mac App 计划

> 决策日期：2026-10-05。Leo 确认：**若独立 Python 引擎无功能损耗，则放弃 SillyTavern 运行时**。
> 盘点结论：无损耗（见「依据」）。模型依赖外部 Ollama；只迁移角色卡与世界书，对话从零开始。

## 依据（2026-10-05 盘点四个 ST 实例）

| 项目 | 实际使用情况 | V3 处理 |
|---|---|---|
| Chat Completion 预设 | 四实例均为 ST 自带 Default，无自定义 prompt 块 | Pipeline 13 阶段已对齐 ST 默认顺序 |
| 扩展 / 正则 / 摘要 / 向量 | 全部未启用 | 无需移植 |
| 世界书 | 无全局挂载；全部来自角色卡内嵌 Character Book（DM 68 / 无职 46 / 生命教 57 条）。高级字段仅 `constant`，其余默认 | 新写 `tavern/worldinfo.py`，ST 字段级兼容 |
| 角色卡 | V2/V3 PNG，含 PHI、多开场白 | `character_card.py` 已支持 |
| 上下文 | 16k token 裁剪 | 新增 token 预算裁剪 |
| bot 自有逻辑 | Director、翻译表、世界状态、RPG 规则、存档命令，全是 Python | 直接移植为引擎模块 |
| **损失** | ST WebUI 不再是运行时的一部分 | ST 保留为**编辑工具**，PNG / JSON 格式兼容，改完直接放入引擎目录 |

## 目标形态

```
TelegramTavern.app  (菜单栏常驻)
 ├─ 启停全部 bot / 单个 bot
 ├─ 设置窗口：Telegram token、选角色卡、选世界书、选模型（一键 ollama pull）
 ├─ Ollama 检测：未安装 → 引导安装；未运行 → 自动拉起
 └─ 日志查看
数据目录（~/Library/Application Support/TelegramTavern/ 或 App 同级）
 ├─ bots/*.yaml        一个文件 = 一个 bot
 ├─ characters/*.png   ST 兼容角色卡（内嵌世界书自动识别）
 ├─ worlds/*.json      ST 兼容世界书（可多本叠加到任一 bot）
 ├─ data/<bot>/        SQLite：会话、世界状态、绑定
 └─ logs/
```

`bots/<name>.yaml` 示例：

```yaml
name: dungeon-master
token_env: TG_TOKEN_DM            # 真实 token 放 .env / Keychain，不进 yaml
mode: rpg                          # dialogue | rpg
characters: [DungeonMaster12.png]  # dialogue 模式可列多张，/character 切换
worlds: [Eldoria.json]             # 叠加在角色卡内嵌书之上
model: fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest
extract_model: qwen3:14b           # rpg 模式的状态抽取小模型
reply_language: zh-CN
language_override: |               # 可选，替代原各 bot 硬编码的 LANGUAGE_OVERRIDE
  ...
translation_table: {}              # 可选，SAENGMYEONG 的罗马音→中文表
director_enabled: false            # Penelope 专用 Director 块
```

## 分阶段

### Phase 1 — 引擎核心（仓库 `telegramtavern/`，新包 `tavern/`）
1. `tavern/worldinfo.py`：ST 兼容世界书引擎。字段：key/keysecondary、selectiveLogic（AND_ANY/NOT_ALL/NOT_ANY/AND_ALL）、constant、order、position（before/after char、AN top/bottom、EM top/bottom、@depth+role）、scanDepth、caseSensitive、matchWholeWords、正则键、probability、递归（含 exclude/prevent）、token 预算。同时读取角色卡内嵌 book 与 ST 世界 JSON。**测试用现有三本真实书。**
2. Pipeline 接入：Lore Before/After/Examples/Depth；token 预算裁剪历史；`/start` 支持多开场白。
3. `tavern/manager.py`：读取 `bots/*.yaml`，一个进程跑 N 个 PTB Application；模式：
   - `dialogue`：移植 SINGLE 的 `/character /chars /use /where /reset /newgame`，按 Telegram chat 绑定角色（已有 chat_bindings 思路）。
   - `rpg`：移植 DM 的 `game_state.py`、`state_extractor.py`、`rpg_engine.py`、`director_engine.py`，命令 `/newgame /continue /session(s) /save /load /export_* /endgame`。
4. 移植 MUSHOKU / SAENGMYEONG 的语言覆盖、first_mes 预翻译、`translate_to_chinese` 表为 yaml 配置。
5. 冒烟：离线 assemble 三张卡 + 真实 Telegram 单 bot 跑通（启动前先停 connector 里对应旧 bot，避免 getUpdates 冲突）。

### Phase 2 — App 外壳与打包
- `app/`：菜单栏（rumps）+ 设置窗口（pywebview 或原生对话框）；Ollama 检测 / 拉起 / `ollama pull` 进度；日志窗口。
- PyInstaller spec → `TelegramTavern.app`；首次启动创建数据目录并复制示例 yaml。
- `.claude/launch.json` 供本机预览。

### Phase 3 — 切换
- 四个 token 迁入新引擎；逐个停旧 bot、起新 bot、实测。
- connector 栈归档；Obsidian `PROJECT_CONTEXT.md` 的「V2 以 SillyTavern 为中心」决策标记为被 V3 取代。

## 移植清单（来源：`~/Documents/SillyTavern/connector/`）

| 来源 | 行数 | 去向 |
|---|---|---|
| dungeon-master-bot/game_state.py（含 2026-10-05 aliases/纠正行） | ~600 | tavern/rpg/game_state.py |
| dungeon-master-bot/state_extractor.py | ~170 | tavern/rpg/state_extractor.py |
| dungeon-master-bot/rpg_engine.py | 1084 | tavern/rpg/rules.py |
| dungeon-master-bot/director_engine.py（DEFAULT_SCENES 内置） | 612 | tavern/rpg/director.py，scenes 外置为 yaml |
| telegram-bot/bot.py 的角色绑定与命令 | 692 | tavern/modes/dialogue.py |
| saengmyeong-bot/ollama_fallback.py `translate_to_chinese` + 16 项表 | 183 | yaml `translation_table` + tavern/postprocess.py |
| 各 bot `LANGUAGE_OVERRIDE` 常量 | — | yaml `language_override` |
| ChatBridge 的 Penelope Director 块 | — | director_enabled 时由 pipeline 注入 |

## 不做的事
- 不打包 Node / Chromium / SillyTavern。
- 不内置 llama.cpp（Leo 选择依赖外部 Ollama）。
- 不迁移 ST 聊天历史。
