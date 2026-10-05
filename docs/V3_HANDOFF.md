# V3 Handoff — 2026-10-05

给下一个会话的接力说明。先读 [V3_PLAN.md](V3_PLAN.md)，再读本文件。

## 已完成

### A. DM bot 跑偏修复（旧栈，已上线运行）
位置 `~/Documents/SillyTavern/connector/`（未提交）。根因：ST 安装的 ChatBridge 扩展忽略 OpenAI `system` 消息，DM 的 CURRENT WORLD STATE 从未到达模型；规则版状态提取实战几乎不触发。
- ChatBridge：新增 opt-in `st_extension_prompt {text, depth}`，IN_CHAT 注入。文件 `SillyTavern/public/scripts/extensions/third-party/SillyTavern-Extension-ChatBridge/index.js`，备份 `index.js.bak-20261005-ext-prompt`。
- `dungeon-master-bot/state_extractor.py`（新）、`bot.py`、`game_state.py`（aliases + 纠正行）、`tests/dm_state_extract_test.py`、`tests/dm_st_runner.py`（console 过滤）、`.env.example`、`README.md`。
- 验证：离线 PASS；端到端暗号复述 PASS。DM 全栈（ST :8010 / bridge :8013,:8011 / runner / ollama proxy :11435 / dm-bot）当前**运行中**。
- 待 Leo 真机实测一场战斗，看 `logs/dm-bot.log` 的 `turn=N narrative= llm= conflicts=`。

### B. V3 独立引擎 Phase 1a：世界书引擎
位置本仓库 `telegramtavern/`。
- `tavern/worldinfo.py`：ST 兼容 World Info。读 ST 世界 JSON 与角色卡内嵌 book；支持 constant、primary/secondary + 4 种 selectiveLogic、正则键、caseSensitive/matchWholeWords、scanDepth、probability（可注入 rng）、递归（exclude/prevent）、token 预算、7 种 position 含 @depth+role。
- `tests/test_worldinfo.py`：29 项 PASS（含三本真实书 + DM 卡 68 条）。
  ```bash
  cd ~/Documents/telegramtavern && penelope/venv/bin/python tests/test_worldinfo.py
  ```

## 下一步（按序）

### Phase 1b — Pipeline 接入
- `pipeline.py` 第 187 行附近 `_build_lorebook` stub：用 `WorldInfo.activate(history, user_message)` 的结果生成 PromptItem：`before_char`→位置 LORE_BEFORE，`after_char`→LORE_AFTER，`em_top/em_bottom`→examples 前后，`an_*`→Author's Note 位置（若无则作为 depth 4 的 in-chat system），`depth_entries`→IN_CHAT depth/role。
- `PromptPipeline.__init__` 增加 `worldinfo: WorldInfo | None`；`PipelineContext` 不变。
- token 预算裁剪：`assemble()` 前按 `estimate_tokens` 从最旧历史起裁到 `max_context - reserved`（默认 16384 - 4096）。
- `/start` 多开场白：`alternate_greetings` 随机或 `/start 3` 指定。
- 测试：三张卡离线 assemble，断言世界书内容出现在正确位置。

### Phase 1c — 多 bot 管理器
- `tavern/config.py`：读 `bots/*.yaml`（结构见 V3_PLAN.md），token 从 `.env`（`token_env`）读取。
- `tavern/manager.py`：一个进程 N 个 `python-telegram-bot` Application（`penelope/venv` 里是 PTB 20.7；connector venv 是 22.8，统一到 22.x）。
- `tavern/modes/dialogue.py`：移植 `connector/telegram-bot/bot.py` 的 `/character /chars /use /where /reset /newgame /ping`，per-chat 绑定（`chat_bindings.json` 思路→SQLite）。
- `tavern/modes/rpg.py` + `tavern/rpg/`：移植 `connector/dungeon-master-bot/{game_state,state_extractor,rpg_engine,director_engine}.py`。`director_engine.DEFAULT_SCENES` 外置为 `worlds/<name>.scenes.yaml`。命令 `/newgame /continue /session /sessions /save /load /export_raw /export_script /export_notes /endgame /help`。
- 语言覆盖与翻译：`connector/saengmyeong-bot/ollama_fallback.py` 的 `translate_to_chinese` 16 项表与 `*_LANGUAGE_OVERRIDE` 常量 → yaml 字段。
- `ollama_client.py` 保留（已处理 think:false 与 reasoning 字段）。

### Phase 2 — App
- `app/`：rumps 菜单栏 + 设置窗口；Ollama 检测/拉起/`pull` 进度；PyInstaller → `TelegramTavern.app`。

### Phase 3 — 切换
- 起新 bot 前必须停 connector 里同 token 的旧 bot（`connector/logs/*.pid`），否则 Telegram `getUpdates Conflict`。
- 四个 token 在 `connector/*/.env`，不要复制进任何文档。

## 注意事项
- 本机：M1 Ultra 64 GB。模型只有一个 RP 特调（Qwen3.6-35B-A3B HauhauCS Aggressive）+ qwen3:14b / ministral-3:8b / qwen3.5 coding。模型替换调研尚未开始。
- workflow-orchestrator 的 PostToolUse hook 会对 `random`、`print`、以及临时环境里解析不到的 import 误报「CRITICAL」；写入其实已成功，用 venv 实际 compile/运行为准。
- 自动模式下 `/workflow-orchestrator:delegate` 曾因分类器不可用失败，直接用 Bash/Read 即可。
- 本仓库其它未提交改动（aqua/june 归档、AB director 结果）早于本次工作，一并待 Leo 决定提交。
- Obsidian 记录：`Projects/telegram-tavern/CURRENT_STATE.md`（2026-10-05 节）、`ROADMAP.md`（V3 节）、根 `SESSION_LOG.md`。
