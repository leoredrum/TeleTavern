# ST Prompt 对照校准 — SillyTavern vs Telegram Tavern

> Phase 1 (已完成) — 提炼两边 Prompt 组装结构、找出差异、记录已修 vs 未修。
>
> 数据源：
> - ST 1.18.0: `~/Documents/SillyTavern/SillyTavern/public/scripts/PromptManager.js` (chatCompletionDefaultPrompts + promptManagerDefaultPromptOrder)
> - TT 1.x: `~/Documents/telegramtavern/pipeline.py` + `docs/prompt_compare_<bot>.md`
>
> 测试样本：`docs/prompt_compare_penelope.md` / `_june.md` / `_aqua.md`（同 4-turn history 跑出来的 /debug 级别报告）

## 1. ST 1.18.0 Prompt 顺序（Chat Completion 模式，main_api=openai / textgen=ollama）

| Pos | Identifier           | 来源                       | Role   | 说明 |
|----:|----------------------|----------------------------|--------|------|
| 0   | main                 | settings.main_prompt       | system | "Write {{char}}'s next reply in a fictional chat…" |
| 1   | worldInfoBefore      | character_book             | system | 上文触发（位置：角色卡之前） |
| 2   | personaDescription   | power_user.persona_description | system | 用户人设 |
| 3   | charDescription      | character_card.description  | system | 角色外观/背景 |
| 4   | charPersonality      | character_card.personality  | system | 性格 |
| 5   | scenario             | character_card.scenario    | system | 当前情境 |
| 6   | enhanceDefinitions   | 默认禁用                   | system | 让模型扩展设定（默认 off） |
| 7   | nsfw                 | settings.nsfw_prompt       | system | NSFW 开关 |
| 8   | worldInfoAfter       | character_book             | system | 下文触发（位置：角色卡之后、示例之前） |
| 9   | dialogueExamples     | character_card.mes_example | system | 示例对话 |
| 10  | chatHistory          | SessionStore                | user/assistant | 真实聊天历史 |
| 11  | jailbreak            | character_card.post_history_instructions | system | 角色卡 PHI（最高优先级） |

Instruct sequence（model=ChatML）：
```
system_sequence:  <|im_start|>system
input_sequence:   <|im_start|>user
output_sequence:  <|im_start|>assistant
stop_sequence:    <|im_end|>
```

Plain-for-Ollama context（TT 用）：把 messages 直接 JSON 序列化（无模板包装），由 Ollama 在 server 端应用 chat template。

## 2. TT Phase 1 Prompt 顺序（pipeline.py）

| Pos | Identifier           | Role   | 来源                          | 说明 |
|----:|----------------------|--------|-------------------------------|------|
| 0   | system_anchor        | system | pipeline._build_system_anchor | "You are X, role-playing…"（替代 ST 的 main） |
| 1   | language_override    | system | pipeline._build_language_override | 强制中文（ST 没有，TT 独有） |
| 2   | director             | system | director.build_director_item  | RP 规则（无循环、推进剧情、repeat-ban 短语…） |
| 3   | lore_before          | —      | stub (Phase 2)                | 未实现 |
| 4   | char_definitions     | system | pipeline._build_character_defs | **合并**：description + personality + scenario + creator_notes 全部打成一个 item |
| 5   | lore_after           | —      | stub (Phase 2)                | 未实现 |
| 6   | persona              | system | pipeline._build_persona       | user persona（仅当 persona.description 非空） |
| 7   | lore_examples        | —      | stub (Phase 2)                | 未实现 |
| 8   | example_dialogues    | system | pipeline._build_examples      | mes_example |
| 9   | summary              | —      | stub (Phase 3)                | 未实现 |
| 10  | chat_history         | u/a    | pipeline._build_chat_history  | 真实聊天历史 |
| 11  | depth_injections     | u/a    | card.get_depth_prompt_items   | 仅当 character_book.depth_prompts 非空 |
| 12  | post_history         | system | card.get_post_history_item    | 角色卡 PHI |

## 3. 逐项差异对照

| # | Prompt 部件                | ST 有? | TT 有? | TT 当前状态                                                                          | 差异 / 需要修吗 |
|--:|----------------------------|:------:|:------:|---------------------------------------------------------------------------------------|----------------|
| 1 | Main Prompt (Write {{char}}'s next reply…) | ✓ | △ | TT 用 `system_anchor` 替代（"You are X, role-playing…"），功能等价               | 不需要修（措辞稍弱于 ST，可后续加 `Write... next reply` 让指令更明确） |
| 2 | World Info (before)        | ✓ | ✗ | Phase 2 stub — 卡片无 character_book                                                | 不在本轮修 |
| 3 | World Info (after)         | ✓ | ✗ | Phase 2 stub — 同上                                                                  | 不在本轮修 |
| 4 | Persona Description        | ✓ | ✓ | TT 有但 persona=None，所以 TT 实际从未注入                                           | 不在本轮修（Phase 2 一起） |
| 5 | Char Description           | ✓ | ✓ | TT 合并到 `char_definitions` 一个 item                                              | 不需要修（功能等价） |
| 6 | Char Personality           | ✓ | ✓ | 同上合并                                                                              | 不需要修 |
| 7 | Scenario                   | ✓ | ✓ | 同上合并                                                                              | 不需要修 |
| 8 | Enhance Definitions        | ✓ | ✗ | ST 默认禁用                                                                            | 不需要修 |
| 9 | NSFW Prompt                | ✓ | ✗ | TT 用 `.env` 开关 NSFW_ENABLED，没有专用 prompt                                      | 不需要修（卡内容本身就是 NSFW） |
| 10 | Example Dialogues          | ✓ | ✓ | Penelope / Aqua 有 mes_example，**June 卡片 mes_example=0 chars**                   | **June 卡片缺示例（已发现，不在本轮修）** |
| 11 | Chat History               | ✓ | ✓ | **修复前**：chat history 在 ollama 看到时重复了一遍（assemble() 里 absolute_items 包含 CHAT_HISTORY items，加上 raw history 又加一遍） | **已修（pipeline.py:1 行 + sync 到 3 bot 副本）** |
| 12 | Depth Injections           | ✓ | ✓ | TT 通过 `card.get_depth_prompt_items()` 实现，但 **3 张卡片的 depth_prompts 都是空**   | 卡片缺数据，pipeline 已支持 |
| 13 | Post-History Instructions  | ✓ | ✓ | TT pipeline 支持，但 **3 张卡片的 post_history_instructions 都是空字符串**           | **HIGH：PHI 是最高优先级钩子，空了等于没有任何"最终提醒"——这跟 ST 一样空，但 ST 的 main prompt 已经够了；TT 缺这个会让模型在长对话里漂移** |
| 14 | First Message（首条 assistant 消息）| ✓ | ✗ | ST 在新聊天时把 first_mes 作为第一条 assistant message 注入；TT **从不注入 first_mes**  | **MED：/reset 之后对话没有开场，模型不知道角色"想从哪里开始"。建议在 pipeline 启动时若 history 为空则注入 first_mes** |
| 15 | Character Book (lorebook)  | ✓ | ✗ | Phase 2 stub                                                                           | 不在本轮修 |
| 16 | Summary / Memory           | ✓ | ✗ | Phase 3 stub                                                                           | 不在本轮修 |
| 17 | Language Override          | ✗ | ✓ | TT 独有 — `[LANGUAGE OVERRIDE — HIGHEST PRIORITY]`                                   | 不需要修（TT 独有功能，针对 ST 默认英文卡） |
| 18 | Director Prompt            | ✗ | ✓ | TT 独有 — 620 tokens 的 RP 规则 + repeat-ban 短语                                    | **MED 风险：620 tokens 的 system 指令可能压过角色卡的 1793 tokens char_definitions。Director 的禁止项全是中文规则，跟角色卡并存时模型倾向"遵守规则"而忽略"角色"。** |
| 19 | Chat Template              | ✓ | ✓ | TT 用 Plain-for-Ollama（无模板包装，Ollama 应用 chatml）；ST 也支持 Plain-for-Ollama | 不需要修（两边一致） |
| 20 | Sampler（temp/top_p/...）  | ✓ | ✓ | TT 通过 ollama_client.py 硬编码 temp=0.95/top_p=0.92/repeat_pen=1.18  | ST 1.18 推荐 RP：temp=0.8 / top_p=0.9 / rep_pen=1.1。TT 略激进，可后续调低 |

## 4. 已修 / 未修 总结

### 已修（本轮）

| # | 问题 | 修复 | 影响 |
|--:|------|------|------|
| 1 | Chat history 在 ollama prompt 里被加了两遍（assemble() bug） | `pipeline.py`: 过滤 `is_absolute and position != CHAT_HISTORY`；同步到 root + 3 bot 副本 | 直接消除"剧情绕圈、重复 micro-expression"的一个主因。每条 chat turn 之前重复一次会让模型把它当成"事实"在第二轮接续，强化重复。 |
| 2 | `/v1/chat/completions` + Qwen3.6 + ollama 0.30：`think:false` 被忽略，模型把全部 token 烧在 reasoning 上，content 全空 | `ollama_client.py`: 切到 ollama 原生 `/api/chat` 端点；thinking 处理逻辑保留（兜底）；同步到 root + 3 bot 副本 | **修复前 production bot 给用户返回空消息**——Telegram 上收到"…" placeholder 但永远不更新。修复后 bot 真出回复。 |

### 未修（本轮，建议下轮）

| # | 问题 | 建议方案 | 优先级 |
|--:|------|----------|--------|
| 1 | PHI 在 3 张卡里全空 | 把 TT director 的 [DIRECTOR] 关键规则镜像到 PHI 位置（system 末尾），这样无论 ST 还是 instruct 模型都看到 | HIGH |
| 2 | First Message 不注入 | pipeline.build_items() 加 `if not history: inject first_mes as first assistant turn` | MED |
| 3 | June 卡片缺示例对话 | 重新制作 June 卡片或在 june/data 放一份 mes_example JSON | LOW（不影响功能，但模型"声音"会平） |
| 4 | Director prompt 620 tokens 可能压过角色卡 | 把 Director 移到 PHI 之后 / 之前的位置测试；或拆分成"硬规则"（在 PHI）和"软规则"（在 director）| MED |
| 5 | ollama_client.py sampler 偏激进 (0.95/0.92/1.18) | 调到 ST RP 推荐 (0.8/0.9/1.1) | LOW |
| 6 | TT 不支持 V3 character_book | Phase 2 范围，不在本轮 | OUT |

## 5. 跟 ST 的关键 takeaway

1. **TT 设计上比 ST 多两层**：`language_override` + `director`。这是为了在没有 ST 那种角色卡 override 控制的情况下强制中文 + 推进剧情。这两层是有意加的，不是缺陷。

2. **TT 的 chat history bug 比 ST 更严重**：ST 的 prompt 顺序是声明式的，每条规则独立；TT 的 assemble() 用 imperative 拼装，bug 风险更大。**修复后 ollama 看到的 history 不再翻倍**——这是本轮最重要的修复。

3. **TT 卡片数据稀疏**：3 张卡片的 PHI、depth prompts、character_book 都是空。ST 在没有这些字段时也工作（fall back 到 main prompt），所以 TT 现在能跑；但角色卡越丰富，TT 跟 ST 越接近。本轮不补这些数据。

4. **PHI 钩子是空的**——这是 ST/TT 共有的问题，但 TT 更依赖它（因为 ST 的 main prompt "Write {{char}}'s next reply..." 本身已经给了强指令，TT 的 system_anchor 更弱）。

## 6. 验证

- `docs/prompt_compare_penelope.md`：9 messages（修前 13）
- `docs/prompt_compare_june.md`：8 messages（修前 12）
- `docs/prompt_compare_aqua.md`：9 messages（修前 13）

（chat history 在 ollama 实际收到的消息列表里各出现 1 次）
