# V3 Handoff — 2026-10-05（晚，Phase 1–3 完成）

先读 [V3_PLAN.md](V3_PLAN.md) 与根 [README.md](../README.md)。

## 当前运行状态（切换已完成）

- **正式入口**：`/Applications/TelegramTavern.app`（pywebview 桌面窗口，`app/window.py` + `app/ui.html`；菜单栏形态 `app/menubar.py` 保留为备选），数据目录
  `~/Library/Application Support/TelegramTavern/`。启动即自动拉起四个 bot：
  `single`（@leopenelop_bot，dialogue，35 张卡）、`dungeon-master`（@leodungeonmaster_bot，rpg + rules + scenes）、
  `mushoku`（@leoaqua_bot，rpg）、`saengmyeong`（@leosaengmyeong_bot，rpg + 翻译表）。
  日志：`<数据目录>/logs/engine.log`。
- **开发副本**：仓库 `data-v3/`（gitignored）内容与 App 数据目录相同（bots / characters / worlds / .env）。
  改 yaml 时两边都要改，或只改 App 目录再 `rsync` 回 `data-v3`。
- **旧栈**（`~/Documents/SillyTavern/connector/`）：四个旧 bot、dm-bridge、dm-st-runner 均已停止，不要再启动（同 token 会 getUpdates 冲突）。
  ST 本体（:8010，node）与 ollama proxy（:11435）仍在运行，只作编辑器 / 无害，可随时 kill。
- 对话历史从零开始；旧 ST 聊天记录仍在 `~/Documents/SillyTavern/*-data/`。

## 本轮完成清单

| 阶段 | 内容 | 验证 |
|---|---|---|
| 1a | `tavern/worldinfo.py` ST 兼容世界书引擎 | `tests/test_worldinfo.py` 29 项 PASS |
| 1b | `pipeline.py`：世界书接入、`extra_items`、token 预算裁剪、ST depth 语义、**PHI 改到历史之后**（V1 错放在历史之前） | `tests/test_pipeline_v3.py` 8 项 PASS |
| 1c | `tavern/config.py` yaml 配置；`tavern/engine.py` CharacterRuntime；`tavern/storage.py`；`modes/dialogue.py`（SINGLE 移植）；`modes/rpg.py` + `rpg/`（DM 四引擎原样移植 + sessions/导出）；`manager.py` 多 bot；`cli.py` | `tests/test_rpg_v3.py` 12 项 PASS；真实 Ollama 生成冒烟 9.6s / 88% 中文 / 世界书命中 |
| 2 | `app/window.py` + `app/ui.html`（pywebview 窗口：总览/Bots/角色卡/世界书/模型/日志，token 写入 .env，导入卡与世界书，拉模型）；`app/menubar.py` 备选；spec + `scripts/build_app.sh`，42 MB .app | 安装版启动，4 bot 轮询；Api 方法 headless 冒烟通过 |
| 3 | 四 token 迁入 `.env`，旧 bot 停止，App 接管 | engine.log 四条 `polling as` |
| + | 本地聊天：`tavern/local.py`（LocalUpdate 适配器，chat_id = -1，私有 asyncio 线程）+ 窗口「💬 本地聊天」页（选 bot/角色、新对话/新游戏/继续/状态/导出、流式显示） | headless：Penelope 开场+回复 13s；mushoku /newgame 25s 建局 |

## 日常操作

```bash
# 加新 bot / 角色卡 / 世界书：放进数据目录 → 菜单「重新加载配置」或重启 App
# 命令行调试
export TAVERN_DATA_DIR=$PWD/data-v3
./venv/bin/python -m tavern check
./venv/bin/python -m tavern run        # 先退出 App，否则 token 冲突
# 重新打包
./scripts/build_app.sh --install       # 之后 open -a TelegramTavern
```

## 已知缺口 / 下一步候选

1. **模型调研**（Leo 要求）：本机唯一 RP 特调模型是 Qwen3.6-35B-A3B HauhauCS Aggressive。待调研无审查 RP 候选并对比。
2. Director 场景表仍是 `tavern/rpg/director_engine.py` 内的 `DEFAULT_SCENES`（Eldoria 专用）。计划外置为 `worlds/<name>.scenes.yaml`。
3. 「冲突时重新生成」未做；现在是下一轮纠正行。
4. Penelope 的 Director 块用的是 V1 `DirectorConfig` 规则，不是 ChatBridge 里那段针对「刚洗完澡」的文案；若需要可把那段写进 `single.yaml` 的 `language_override`。
5. App 未签名（ad-hoc），分发给别人需 Developer ID + notarization；首次打开需右键「打开」。
6. 开机自启：把 App 加进「登录项」即可（未自动配置）。
7. `telegram-bot` 的 `/ping` 原本检查 bridge；现在检查 Ollama。

## 注意事项

- workflow-orchestrator 的 PostToolUse hook 对 `random`、`print`、相对导入会误报 CRITICAL；以 venv 实际运行为准。
- `tests/test_pipeline_v3.py`、`test_rpg_v3.py` 依赖 `data-v3/` 里的真实角色卡；仓库不含卡（隐私），新机器需先放卡。
- `.env`、角色卡、世界书均不进 git。
