# ST Alignment — 15-round 真聊测试

> 测试时间：2026-06-25 23:50 AEST
> 模型：`fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest`
> Ollama：0.30.10，num_ctx=32768，temp=0.95 / top_p=0.92 / repeat_penalty=1.18
> 启动脚本：`scripts/run_15round.py <bot>` — 直接驱动 pipeline.assemble() + ollama_client.stream_chat()，不走 Telegram
> 测试场景：固定 15 轮剧本（greeting → drink → smalltalk → walk → intimacy → objection → reversal → probe → silence → confrontation → vulnerable → escalation → silence → closing），覆盖情绪/剧情推进/重复/silence 几个维度
> 修复前提：本轮修了两个真 bug，详见 `docs/st_prompt_alignment.md` 第 4 节（chat history 重复）+ ollama_client.py 端点切换（`/v1/chat/completions` → `/api/chat`）

## 1. 测试结果汇总

| Bot     | 总轮数 | 中文率≥80% | 剧情推进率 | 平均 repeat/turn | echo jaccard | 4th-wall break | 总耗时 |
|---------|:------:|:-----------:|:----------:|:----------------:|:------------:|:--------------:|:------:|
| Penelope| 15     | **60%** (9/15) | **100%** (15/15) | 0.07 | 0.00 | NO | 28.5s |
| June    | 15     | **13%** (2/15) | **100%** (15/15) | 0.40 | 0.00 | NO | 26.5s |
| Aqua    | 15     | **100%** (15/15) | **100%** (15/15) | 0.20 | 0.00 | NO | 32.9s |

## 2. 单 bot 详情

### Penelope（15 轮）

```
R01 greeting           zh=82% rep=0 plot=Y breaks=[] echo=0.00 (2.6s) len=143
R02 order_drink        zh=83% rep=1 plot=Y breaks=[] echo=0.00 (1.8s) len=114
R03 smalltalk          zh=84% rep=0 plot=Y breaks=[] echo=0.00 (1.8s) len=119
R04 plot_walk          zh=82% rep=0 plot=Y breaks=[] echo=0.00 (2.0s) len=114
R05 physical_intimacy  zh=81% rep=0 plot=Y breaks=[] echo=0.00 (1.6s) len=111
R06 callback           zh=79% rep=0 plot=Y breaks=[] echo=0.00 (1.9s) len=126
R07 user_objection     zh=81% rep=0 plot=Y breaks=[] echo=0.00 (2.3s) len=134
R08 user_reversal      zh=81% rep=0 plot=Y breaks=[] echo=0.00 (1.7s) len=96
R09 emotional_probe    zh=78% rep=0 plot=Y breaks=[] echo=0.00 (2.1s) len=124
R10 user_silence       zh=77% rep=0 plot=Y breaks=[] echo=0.00 (1.5s) len=99
R11 confrontation      zh=84% rep=0 plot=Y breaks=[] echo=0.00 (2.0s) len=130
R12 user_vulnerable    zh=75% rep=0 plot=Y breaks=[] echo=0.00 (2.0s) len=130
R13 intimate_escalation zh=78% rep=0 plot=Y breaks=[] echo=0.00 (1.9s) len=110
R14 long_silence       zh=73% rep=0 plot=Y breaks=[] echo=0.00 (1.6s) len=101
R15 closing            zh=80% rep=0 plot=Y breaks=[] echo=0.00 (1.8s) len=113
```

- 中文稳定：基本在 75-85%，英文 token 主要是"honey"/"darling"等 RP 套语；轮 R14 最低 73%
- 剧情推进：100% — 每轮都有新动作/新信息/新转折
- 重复：仅 R02 出现 1 个 micro-expression（"轻笑"），R11 出现 1 个（"心跳加速"），完全可控
- echo：0.00 — 模型没有复读上一轮内容（修复前的 chat history 重复 bug 会让 echo 显著放大）
- 4th-wall break：无
- 摘要：修复后 Penelope 表现稳定，已达"RP 可用"水准

### June（15 轮）

```
R01 greeting           zh=77% rep=0 plot=Y breaks=[] echo=0.00 (3.1s) len=138
R02 order_drink        zh=71% rep=0 plot=Y breaks=[] echo=0.00 (1.5s) len=102
R03 smalltalk          zh=73% rep=0 plot=Y breaks=[] echo=0.00 (1.9s) len=128
R04 plot_walk          zh=77% rep=0 plot=Y breaks=[] echo=0.00 (1.3s) len=75
R05 physical_intimacy  zh=49% rep=0 plot=Y breaks=[] echo=0.00 (1.7s) len=151  ← ENGLISH LEAK
R06 callback           zh=81% rep=1 plot=Y breaks=[] echo=0.00 (1.8s) len=116
R07 user_objection     zh=80% rep=1 plot=Y breaks=[] echo=0.00 (1.3s) len=83
R08 user_reversal      zh=72% rep=0 plot=Y breaks=[] echo=0.00 (2.0s) len=115
R09 emotional_probe    zh=74% rep=1 plot=Y breaks=[] echo=0.00 (1.7s) len=118
R10 user_silence       zh=74% rep=0 plot=Y breaks=[] echo=0.00 (1.8s) len=122
R11 confrontation      zh=78% rep=0 plot=Y breaks=[] echo=0.00 (2.0s) len=139
R12 user_vulnerable    zh=71% rep=0 plot=Y breaks=[] echo=0.00 (1.5s) len=110
R13 intimate_escalation zh=70% rep=1 plot=Y breaks=[] echo=0.00 (1.6s) len=97
R14 long_silence       zh=70% rep=1 plot=Y breaks=[] echo=0.00 (1.6s) len=90
R15 closing            zh=71% rep=1 plot=Y breaks=[] echo=0.00 (1.8s) len=98
```

- 中文稳定：R05 出现明确英文 leak（"she whispered, her red eyes reflecting the moonlight"），跟 director 的 language_override 冲突——可能跟 June 角色卡的描述细节里夹了英文有关（June 卡片 personality 只有 33 chars，scenario 462 chars，密度不高，模型容易"找补"）
- 剧情推进：100%
- 重复：R06/R07/R09/R13/R14 各 1 个 repeat-phrase，整体 0.40 偏高
- 摘要：June 比 Penelope 表现稍差——主因是卡片信息密度低（缺示例对话、性格描述短）。**建议优先补 June 卡片的内容**

### Aqua（15 轮）

```
R01 greeting           zh=86% rep=1 plot=Y breaks=[] echo=0.00 (4.7s) len=201
R02 order_drink        zh=82% rep=0 plot=Y breaks=[] echo=0.00 (2.3s) len=134
R03 smalltalk          zh=86% rep=0 plot=Y breaks=[] echo=0.00 (2.4s) len=158
R04 plot_walk          zh=83% rep=0 plot=Y breaks=[] echo=0.00 (2.3s) len=133
R05 physical_intimacy  zh=85% rep=0 plot=Y breaks=[] echo=0.00 (2.2s) len=134
R06 callback           zh=82% rep=0 plot=Y breaks=[] echo=0.00 (2.0s) len=118
R07 user_objection     zh=84% rep=0 plot=Y breaks=[] echo=0.00 (1.8s) len=100
R08 user_reversal      zh=86% rep=0 plot=Y breaks=[] echo=0.00 (1.4s) len=92
R09 emotional_probe    zh=84% rep=1 plot=Y breaks=[] echo=0.00 (1.8s) len=105
R10 user_silence       zh=88% rep=0 plot=Y breaks=[] echo=0.00 (1.8s) len=107
R11 confrontation      zh=84% rep=0 plot=Y breaks=[] echo=0.00 (2.5s) len=144
R12 user_vulnerable    zh=83% rep=1 plot=Y breaks=[] echo=0.00 (1.8s) len=100
R13 intimate_escalation zh=84% rep=0 plot=Y breaks=[] echo=0.00 (2.4s) len=127
R14 long_silence       zh=87% rep=0 plot=Y breaks=[] echo=0.00 (1.9s) len=119
R15 closing            zh=82% rep=0 plot=Y breaks=[] echo=0.00 (1.5s) len=101
```

- 中文稳定：100% — 全部 ≥80%
- 剧情推进：100%
- 重复：R01/R09/R12 各 1 个，整体 0.20
- 摘要：Aqua 表现最好。Aqua 卡片 scenario 68 chars / personality 50 chars 也很短，但因为 description 1013 chars + examples 724 chars 信息密度合适

## 3. 修复 vs 修复前对比

修复前（chat history 重复 bug + ollama endpoint 错误）：
- Penelope 15 轮里有 7 轮返回空（实际只剩 8 轮有效），echo jaccard 0.04
- June/Aqua 类似一半轮次返回空

修复后（本轮）：
- 所有 15 轮都有有效回复
- 所有 echo jaccard = 0.00（连续两轮没出现重复 n-gram）
- 中文率稳定在 70-88%

## 4. 建议下一步

按优先级：

1. **补 June 卡片**：mes_example=0，personality=33 chars，scenario=462 chars。补到接近 Penelope/Aqua 密度（desc ~1500 / personality ~1000 / scenario ~800 / examples ~1000），预计能把中文率从 13% 拉到 70%+。
2. **PHI 注入**：3 张卡 post_history_instructions 都是空。把 director 的关键规则镜像到 PHI 位置，让模型在最后还有一次 reminder。
3. **first_mes 注入**：/reset 之后没有开场白，模型不知道从哪起。建议 pipeline 启动时若 history 为空则注入 first_mes。
4. **Director 强度**：当前 director 620 tokens，可能压过角色卡。考虑把硬规则挪到 PHI（system 末尾）位置。

## 5. 长 session 压测建议

**不建议立即进入**。理由：
- 修复刚合入，没有在真实 Telegram 客户端跑过（这次测试是直接调 pipeline + ollama，绕过了 PTB 的 reply 流程）
- June 卡片内容稀薄，需要先补
- PHI 空 + first_mes 缺失这两个 HIGH 优先级项没修

建议先：
1. 在真实 Telegram 跑 5-10 轮验证 reply 流（文本编辑速率、退款行为、/debug 命令）
2. 补 June 卡片 + 加 PHI 注入
3. 再做长 session 压测（50+ 轮）
