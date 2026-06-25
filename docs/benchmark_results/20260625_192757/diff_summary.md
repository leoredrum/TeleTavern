# AB Test: Phase 1 ON vs OFF

- **Model**: `fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive`
- **Date**: 20260625_192757
- **Char**: Penelope
- **Sampling**: temp=0.95, top_p=0.92, repeat_penalty=1.18, num_predict=1024
- **Endpoint**: ollama-native /api/chat (think=false)

## Outputs

- Phase 1 ON:  `docs/benchmark_results/20260625_192757/on/`
- Phase 1 OFF: `docs/benchmark_results/20260625_192757/off/`

## 评分表（人工填写）

| Scenario | 输入 | ON 推进 | OFF 推进 | ON 一致 | OFF 一致 | ON 重复 | OFF 重复 | ON 沉浸 | OFF 沉浸 |
|----------|------|---------|----------|---------|----------|---------|----------|---------|----------|
| 01_greeting | `你好` | /5 | /5 | /5 | /5 | /5 | /5 | /5 | /5 |
| 02_daily | `今天天气不错` | /5 | /5 | /5 | /5 | /5 | /5 | /5 | /5 |
| 03_emotional | `我今天有点难过` | /5 | /5 | /5 | /5 | /5 | /5 | /5 | /5 |
| 05_plot_advance | `我们去看电影吧` | /5 | /5 | /5 | /5 | /5 | /5 | /5 | /5 |
| 06_user_leads | `我带你回我老家` | /5 | /5 | /5 | /5 | /5 | /5 | /5 | /5 |
| 07_user_silent | `（不说话等 3 轮再回复）` | /5 | /5 | /5 | /5 | /5 | /5 | /5 | /5 |
| 09_user_refuses | `我不想去` | /5 | /5 | /5 | /5 | /5 | /5 | /5 | /5 |
| 10_topic_shift | `对了，你知道 Python 吗` | /5 | /5 | /5 | /5 | /5 | /5 | /5 | /5 |
| 11_micro_repeat | `靠近一点` | /5 | /5 | /5 | /5 | /5 | /5 | /5 | /5 |
| 12_plot_stuck | `然后呢` | /5 | /5 | /5 | /5 | /5 | /5 | /5 | /5 |
| 15_blush_loop | `看着我` | /5 | /5 | /5 | /5 | /5 | /5 | /5 | /5 |
| 16_whisper_loop | `说什么` | /5 | /5 | /5 | /5 | /5 | /5 | /5 | /5 |
| 17_heartbeat_loop | `紧张吗` | /5 | /5 | /5 | /5 | /5 | /5 | /5 | /5 |
| 19_lipbite_loop | `怎么了` | /5 | /5 | /5 | /5 | /5 | /5 | /5 | /5 |
| 21_character_drift | `你是 AI 吗` | /5 | /5 | /5 | /5 | /5 | /5 | /5 | /5 |

## 平均分

- ON: ___
- OFF: ___

## 结论

Phase 1 PASS / FAIL: __

理由：___
