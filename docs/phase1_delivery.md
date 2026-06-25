# Phase 1 交付文档 — Telegram 酒馆

## 文件变更清单

### 新增文件

| 文件 | 说明 |
|------|------|
| `prompt_item.py` | PromptItem dataclass — 模块化 Prompt 片段的原子单元 |
| `director.py` | DirectorConfig + Director Prompt 生成 — 所有 RP 规则集中配置 |
| `pipeline.py` | PromptPipeline — 核心编排器，组装所有 PromptItem |
| `docs/architecture.md` | 完整架构设计文档（Phase 1–5 路线图） |

### 修改文件

| 文件 | 改动 |
|------|------|
| `character_card.py` | 新增 DepthPrompt、ExtendedCharacterCard；`load_character()` 返回 ExtendedCharacterCard |
| `bot.py` | 移除 `RPBuilder`；接入 `PromptPipeline`；新增 `/debug` 命令；`DEBUG_PROMPT` 环境变量 |
| `ollama_client.py` | 采样参数从写死改为可配置（temperature/top_p/repeat_penalty）|

### 同步到三个 Bot

```
sillytavern-telegram-bot/  (Penelope) ← 主开发目录
june-telegram-bot/          (June)
aqua-telegram-bot/           (Aqua)
```

所有新增和修改的文件已同步到三个 bot。

---

## 核心架构说明

### PromptItem — 模块化原子单元

```python
@dataclass
class PromptItem:
    id: str                       # 唯一标识
    role: str                     # "system" | "user" | "assistant"
    content: str                  # 内容文本
    enabled: bool = True          # 可独立开关
    position: PromptPosition      # 在 Pipeline 中的位置
    priority: int = 0             # 同位置时的排序
    depth: int = 0               # IN_CHAT 时：0=最后，1=倒数第2，...
    token_budget: float = 0.0    # Token 预算（Phase 2 启用）
    source: str = ""              # 来源：pipeline / director / character_card / ...
    metadata: dict = {}           # 元数据（调试用）
```

### PromptPosition — Pipeline 顺序

```
[0]  SYSTEM_ANCHOR         — "You are Penelope, role-playing..."
[1]  LANGUAGE_OVERRIDE    — "Always reply in Chinese..."
[2]  DIRECTOR             — RP 规则（禁止循环、推进剧情...）
[3]  LORE_BEFORE          — Lorebook（Phase 2）
[4]  CHARACTER_DEFS       — Description / Personality / Scenario
[5]  LORE_AFTER           — Lorebook（Phase 2）
[6]  PERSONA              — 用户 Persona
[7]  LORE_EXAMPLES        — Lorebook（Phase 2）
[8]  EXAMPLES             — Example Dialogues
[9]  SUMMARY              — Memory Summary（Phase 3）
[10] CHAT_HISTORY         — 真实聊天历史
[11] DEPTH_INJECTIONS     — 深度注入（在历史特定位置）
[12] POST_HISTORY         — Post-History Instructions（最后，最高优先级）
```

### DirectorConfig — 集中配置 RP 规则

```python
DirectorConfig(
    enabled=True,
    no_option_listing=True,      # 不要列出选项，直接演
    no_meta_commentary=True,     # 不做元评论
    no_prompt_explanation=True,  # 不解释自己在做什么
    no_fourth_wall=True,        # 不跳出角色
    no_repeat_micro_expressions=True,  # 同一微表情只出现1次
    no_emotion_loops=True,      # 禁止情绪循环
    no_user_repeat=True,        # 不复述用户的话
    advance_narrative=True,     # 必须推进剧情
    proactive_role=True,        # 角色必须主动
    avoid_purple_prose=True,    # 避免 Purple Prose
    reply_length_min=80,        # 最短 80 字
    reply_length_max=280,       # 最长 280 字
)
```

### DepthPrompt — 深度注入

```python
@dataclass
class DepthPrompt:
    depth: int = 0        # 注入深度：0=最后消息后，1=倒数第2条消息前
    role: str = "system" # 角色（system/user/assistant）
    content: str = ""     # 注入内容
    enabled: bool = True
```

角色卡的 `depth_prompts` 字段（V3）自动解析为 DepthPrompt 并注入到 PromptPipeline。

---

## 测试命令

### 1. 启动 Bot

```bash
# Penelope（sillytavern-telegram-bot）
cd ~/Projects/sillytavern-telegram-bot
./venv/bin/python bot.py

# June
cd ~/Projects/june-telegram-bot
./venv/bin/python bot.py

# Aqua
cd ~/Projects/aqua-telegram-bot
./venv/bin/python bot.py

# 后台运行
cd ~/Projects/sillytavern-telegram-bot && nohup ./venv/bin/python bot.py > bot.log 2>&1 &
```

### 2. 查看 Prompt Debug

在 Telegram 里发送：
```
/debug
```

输出示例：
```
=== PROMPT DEBUG REPORT ===
char=Penelope  user=Friend
Pos  ID                              Role      Depth  TokEst   Ena  Source
----------------------------------------------------------------------------------------------------
  0  system_anchor                   system        0     150  True  pipeline
  1  language_override               system        0      94  True  pipeline
  2  director                        system        0     451  True  director
  4  char_definitions               system        0    1793  True  character_card
  8  example_dialogues             system        0     645  True  character_card
 10  history_0                     assistant      0       9  True  chat_history
 10  history_1                     user          0       5  True  chat_history
----------------------------------------------------------------------------------------------------
                                                TOTAL TOKENS (est)    3147
=== END REPORT ===
```

### 3. 开启 Verbose Debug 日志

```bash
# 在启动前设置环境变量
DEBUG_PROMPT=1 ./venv/bin/python bot.py
```

这会在每次生成时打印所有 PromptItem 的调试信息。

### 4. 验证 Phase 1 生效

**测试 1：旧角色卡仍可聊天**
```
发送任意消息 → 角色应正常回复（Pipeline 使用 ExtendedCharacterCard）
```

**测试 2：新 ExtendedCharacterCard 可聊天**
```
角色卡已自动升级为 ExtendedCharacterCard
→ /character 应显示 depth_prompts=0（没有深度注入的角色卡）
```

**测试 3：Director Prompt 被正确注入**
```
/debug 输出中 Position 2 = director
→ 确认 RP 规则被注入到 Prompt
```

**测试 4：Post-History Instructions 在最后生效**
```
如果角色卡有 post_history_instructions 字段：
→ /debug 输出中 Position 12 = post_history_instructions
```

**测试 5：Depth Injection 可以插入**
```
在角色卡的 depth_prompts 字段添加：
{
  "depth": 0,
  "role": "system",
  "content": "你是一个非常害羞的角色。"
}
→ /debug 应显示 Position 11 = DEPTH_INJECTIONS，depth=0
```

**测试 6：Prompt Debug 显示结构**
```
/debug
→ 应看到所有 7+ 个 PromptItem，位置正确，token 估算合理（总 ~3000-3500）
```

### 5. 回滚

```bash
# 方法 1：恢复旧文件
cd ~/Projects/sillytavern-telegram-bot
git checkout -- bot.py character_card.py ollama_client.py

# 方法 2：重新启动旧 bot 进程
# 旧 bot 进程 ID（在启动 Phase 1 之前记录）
kill <旧PID>
cd ~/Projects/sillytavern-telegram-bot && nohup ./venv/bin/python bot.py > bot.log 2>&1 &

# 方法 3：如果 git 没有旧版本，从备份恢复
# 备份位置：~/Backups/...
```

---

## 最小测试用例

```python
# test_pipeline.py — 放在 bot 同目录下运行
cd ~/Projects/sillytavern-telegram-bot && ./venv/bin/python -c "
from pipeline import PromptPipeline, Persona
from director import DirectorConfig
from character_card import load_character
from config import config

card = load_character(config.character_path)
cfg = DirectorConfig()
pipeline = PromptPipeline(
    card=card,
    director_cfg=cfg,
    persona=None,
    char_label=config.char_label,
    user_label=config.user_label,
    reply_language='Chinese',
)

history = [
    {'role': 'assistant', 'content': 'Hello! How are you?'},
    {'role': 'user', 'content': 'I am fine.'},
]

items = pipeline.build_items(history, 'Tell me a story.')
print(f'Total items: {len(items)}')
print(pipeline.debug_report(history, 'Tell me a story.'))

# 验证关键 item 存在
ids = [i.id for i in items]
assert 'system_anchor' in ids, 'Missing system_anchor'
assert 'language_override' in ids, 'Missing language_override'
assert 'director' in ids, 'Missing director'
assert 'char_definitions' in ids, 'Missing char_definitions'
assert 'history_0' in ids, 'Missing history'
print('All assertions PASSED')
"
```

---

## 下一步建议

### Phase 2：Lorebook 引擎
- 实现关键词激活的 Lore Entry
- 支持 token budget 裁剪
- Sticky / Cooldown / Delay 定时效果
- 与角色卡 `character_book` 字段集成

### Phase 3：Memory / Summary
- 实现 ChatSummary 作为消息锚点
- 自动摘要触发（每 N 条消息）
- 情节摘要替代简单截断

### Phase 4：Chat Template + Sampling
- Qwen / Llama 等模型模板适配
- SamplingConfig + 模型预设系统

### Phase 5：多角色 / 群聊
- ChatParticipant + GroupChatState 架构预留
