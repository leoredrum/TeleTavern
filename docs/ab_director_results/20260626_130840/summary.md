# English vs Chinese Director — A/B Test Results

**Date:** 2026-06-26 13:09  |  **Bot:** penelope
**Model:** fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest

## Head-to-Head Comparison

| Metric | A: Chinese Director | B: English Director | Δ |
|--------|-------------------|-------------------|---|
| Plot advance rate | 100% | 100% | +0% |
| Avg repeat/turn | 0.93 | 0.20 | -0.73 |
| Stuck in loop | False | False | — |
| Chinese dominant turns | 27% | 0% | -27% |
| Avg Chinese ratio | 79% | 40% | -39% |
| Any 4th-wall break | False | False | — |
| Quality score (avg) | 6.6 | 6.1 | -0.5 |
| Total time | 27.6s | 27.4s | — |

## Per-Round Quality Score (A vs B, max=8 per round)

| Round | Tag | A score | B score | Δ | A plot | B plot | A rep | B rep |
|-------|-----|---------|---------|---|--------|--------|-------|-------|
|  1 | greeting | 6 | 7 | +1 | ✓ | ✓ | 1 | 0 |
|  2 | order_drink | 7 | 7 | 0 | ✓ | ✓ | 2 | 0 |
|  3 | smalltalk | 5 | 7 | +2 | ✓ | ✓ | 3 | 0 |
|  4 | plot_walk | 8 | 6 | -2 | ✓ | ✓ | 0 | 0 |
|  5 | physical_intimacy | 7 | 6 | -1 | ✓ | ✓ | 0 | 0 |
|  6 | callback | 7 | 5 | -2 | ✓ | ✓ | 0 | 1 |
|  7 | user_objection | 8 | 6 | -2 | ✓ | ✓ | 0 | 0 |
|  8 | user_reversal | 8 | 6 | -2 | ✓ | ✓ | 0 | 0 |
|  9 | emotional_probe | 7 | 6 | -1 | ✓ | ✓ | 0 | 0 |
| 10 | user_silence | 6 | 6 | 0 | ✓ | ✓ | 1 | 1 |
| 11 | confrontation | 6 | 6 | 0 | ✓ | ✓ | 2 | 0 |
| 12 | user_vulnerable | 7 | 5 | -2 | ✓ | ✓ | 0 | 1 |
| 13 | intimate_escalation | 5 | 6 | +1 | ✓ | ✓ | 3 | 0 |
| 14 | long_silence | 6 | 6 | 0 | ✓ | ✓ | 1 | 0 |
| 15 | closing | 6 | 6 | 0 | ✓ | ✓ | 1 | 0 |

## Verdict

**VERDICT: No significant difference.**
  Plot Δ=+0%, repeat Δ=-0.73, quality Δ=-0.5
  → Root Cause #1 NOT the main cause. Move to next hypothesis.

Results saved to: `/Users/leo/Documents/telegramtavern/docs/ab_director_results/20260626_130840/`