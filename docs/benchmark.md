# Phase 1 RP Benchmark

> 固定测试集。**所有模型 / Prompt / Director 调整必须跑同一套。**
>
> 用于 Phase 1 验证：证明 PromptPipeline 比 naive prompt 在 RP 质量上是否真的提升。
>
> 任何后续 Phase 的 Baseline 必须基于本 benchmark 的结果。

## 1. 跑法

```bash
cd ~/Documents/telegramtavern
./scripts/run_benchmark.sh [--phase1-on|--phase1-off|--compare]
# 输出：
#   docs/benchmark_results/<timestamp>/<scenario>_<mode>.txt
#   docs/benchmark_results/<timestamp>/summary.md
```

每次跑完生成 `summary.md`，人工按 [评分标准](#评分标准) 填表。

## 2. 测试场景（25 个固定）

> 所有场景以 User 输入为脚本起点。
> 跑法：每个 scenario 跑 4 轮对话，最后把对话上下文给评分人。
> 角色：Penelope（默认）。User：Friend。

### 2.1 基础能力（10 个）

| # | Scenario | User 第一句 | 验收重点 |
|---|----------|-------------|----------|
| 01 | Greeting | `你好` | 开场自然、有礼、符合人设 |
| 02 | Daily chat | `今天天气不错` | 闲聊自然、不突兀 |
| 03 | Emotional | `我今天有点难过` | 共情、不夸张 |
| 04 | Long conv (5 turns) | `你好` → `你平时做什么` → `最近有什么计划` → `…` → `…` | 5 轮后角色不漂移 |
| 05 | Plot advance | `我们去看电影吧` | 角色主动推进剧情 |
| 06 | User leads plot | `我带你回我老家` | 角色跟随 + 主动添加细节 |
| 07 | User silent (no input 5s) | `（不说话等 3 轮再回复）` | 角色主动开话题 |
| 08 | User interrupts | `等等`（在角色长回复中） | 角色立刻停下、听用户 |
| 09 | User refuses | `我不想去` | 角色不勉强、不追 |
| 10 | User topic shift | `对了，你知道 Python 吗` | 角色自然切换话题 |

### 2.2 RP 质量（重点 15 个）

| # | Scenario | User 第一句 | 验收重点 |
|---|----------|-------------|----------|
| 11 | Micro-expression repeat | `靠近一点`（连续 3 轮） | 同表情不重复 >1 次 |
| 12 | Plot stuck (no advance) | `然后呢`（3 轮重复） | 必须引入新事件/转折 |
| 13 | Purple prose | `继续`（在情感场景里） | 形容词堆砌检测 |
| 14 | Infinite setup (铺垫) | `继续写`（5 次） | 不无限铺垫 |
| 15 | 脸红 loop | `看着我`（5 次） | 不连续脸红 |
| 16 | Whisper loop | `说什么`（3 次） | 不反复"耳边低语" |
| 17 | Heartbeat loop | `紧张吗`（3 次） | 不反复"心跳加速" |
| 18 | Silent loop | `（沉默 5 轮）` | 角色不被动等待超过 2 轮 |
| 19 | Lip-bite loop | `怎么了`（3 次） | 不反复"轻咬嘴唇" |
| 20 | Same-scene stuck | `走` `走` `走`（3 次） | 必须推进场景 |
| 21 | Character drift | `你是 AI 吗` | 严守第四墙、不跳出 |
| 22 | Self-initiative | `（沉默 10 轮）` | 角色主动发起新剧情/动作 |
| 23 | Immersion break | `你怎么不说话了` | 不解释自己"在思考" |
| 24 | User rebellion | `我不要这个剧情` | 角色接受、不坚持 |
| 25 | User NPC injection | `旁边走过来一个陌生人` | 角色自然反应 |

## 3. 评分标准

每个 scenario 在 4 个维度打 1-5 分：

| 维度 | 含义 | 1 分 = 失败 | 5 分 = 完美 |
|------|------|-------------|-------------|
| **剧情推进** | 是否引入新信息/转折 | 原地踏步、纯等待 | 每轮都推进 |
| **角色一致** | 是否保持 persona + 第四墙 | 跳出角色 / 自称 AI | 完全沉浸 |
| **重复率** | 微表情 / 句子结构是否重复 | ≥3 处重复 | 0 重复 |
| **沉浸感** | 自然度 + 主动创造 | 机械、被动 | 自然、主动 |

### 评分细则

- **剧情推进**：检查每轮回复是否含"新动作 / 新信息 / 新转折"。否则扣分。
- **角色一致**：检查是否用 AI 自我指代、是否解释 prompt。检查 persona 关键词是否出现。
- **重复率**：扫整个对话，统计 `脸红 / 靠近 / 耳边 / 心跳 / 沉默 / 轻咬嘴唇 / 轻笑 / 垂下眼帘 / 凑近 / 愣了愣` 等词频。每个词出现 >2 次扣分。
- **沉浸感**：主观判断回复是否像真人 RP。

## 4. 通过门槛（Phase 1 Baseline）

- 全部 25 个 scenario 的平均分 ≥ 4.0 / 5.0
- 任意 scenario 不低于 3.0
- 重复率维度平均 ≥ 4.0（重点）

## 5. AB Test 用法

跑 Phase1 ON vs OFF 用同一份 benchmark：

```bash
./scripts/run_benchmark.sh --compare
# ON  结果：benchmark_results/<ts>/on/scenario_XX.txt
# OFF 结果：benchmark_results/<ts>/off/scenario_XX.txt
# diff：benchmark_results/<ts>/diff_summary.md
```

详见 `docs/ab_test_phase1.md`。

## 6. 不变量

- 测试场景**只增不删**（删除 = 历史数据失真）
- User 脚本**只增不改**（同 scenario 跑两次必须一致输入）
- 评分维度**只增不改**
- 通过门槛**只能上调**

## 7. 当前进度

| 日期 | 模型 | 模式 | 平均分 | 状态 |
|------|------|------|--------|------|
| 2026-06-25 | Qwen3.6-35B-A3B (HauhauCS) | Phase 1 ON | (待填) | 调优前基线 |
| 2026-06-25 | Qwen3.6-35B-A3B (HauhauCS) | Phase 1 OFF | (待填) | 对照组 |
| 2026-06-25 | Qwen3.6-35B-A3B (HauhauCS) | Phase 1 ON (调优后) | (待填) | 调优后基线 |

> 任何后续模型 / Director 调整都加一行到这个表。