"""Curated model catalog for the 模型 page.

Every entry was verified to exist on 2026-10-05 (Ollama registry page or
Hugging Face repo + Q4_K_M file). `pull` is the exact `ollama pull` name;
HF GGUF repos use Ollama's `hf.co/<repo>:<quant>` syntax.
Sizes are the Q4_K_M download size; `ram_gb` is the unified memory a Mac
needs to run it comfortably alongside the system.
"""
from __future__ import annotations

import subprocess

ROLE_CHAT, ROLE_EXTRACT = "chat", "extract"

CATALOG: list[dict] = [
    # ---- chat / roleplay (uncensored) ------------------------------------------------------------
    {"pull": "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest", "name": "Qwen3.6 35B-A3B Uncensored (HauhauCS)",
     "role": ROLE_CHAT, "size_gb": 22.1, "ram_gb": 32, "tier": "32 GB+", "chinese": "优秀",
     "desc": "当前默认模型。Qwen3.6 MoE，35B 总参数 / 3B 激活，速度接近小模型、文笔接近大模型，中文最强，无审查。",
     "url": "https://ollama.com/fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive", "recommended": True},
    {"pull": "huihui_ai/qwen3-abliterated:30b-a3b", "name": "Qwen3 30B-A3B abliterated",
     "role": ROLE_CHAT, "size_gb": 18.6, "ram_gb": 32, "tier": "32 GB+", "chinese": "优秀",
     "desc": "官方 Qwen3 MoE 去审查版（abliterated，不改文风只移除拒答）。中文好、速度快，比上面更「本色」。",
     "url": "https://ollama.com/huihui_ai/qwen3-abliterated"},
    {"pull": "huihui_ai/qwen3-abliterated:14b", "name": "Qwen3 14B abliterated",
     "role": ROLE_CHAT, "size_gb": 9.3, "ram_gb": 16, "tier": "16 GB", "chinese": "优秀",
     "desc": "16 GB 机器的中文 RP 首选：Qwen3 14B 去审查，叙事连贯，适合 RPG 模式长对话。",
     "url": "https://ollama.com/huihui_ai/qwen3-abliterated", "recommended": True},
    {"pull": "huihui_ai/qwen3-abliterated:8b", "name": "Qwen3 8B abliterated",
     "role": ROLE_CHAT, "size_gb": 5.2, "ram_gb": 8, "tier": "8 GB", "chinese": "良好",
     "desc": "8 GB 机器能跑的中文 RP 入门选择，去审查。文笔弱于 14B，但够用。",
     "url": "https://ollama.com/huihui_ai/qwen3-abliterated", "recommended": True},
    {"pull": "huihui_ai/mistral-small-abliterated:24b", "name": "Mistral Small 24B abliterated",
     "role": ROLE_CHAT, "size_gb": 14.0, "ram_gb": 32, "tier": "32 GB+", "chinese": "一般",
     "desc": "Mistral Small 3 去审查版，英文 RP 质量高、指令跟随稳；中文可用但不如 Qwen。",
     "url": "https://ollama.com/huihui_ai/mistral-small-abliterated"},
    {"pull": "hf.co/TheDrummer/Cydonia-24B-v4.1-GGUF:Q4_K_M", "name": "Cydonia 24B v4.1 (TheDrummer)",
     "role": ROLE_CHAT, "size_gb": 14.3, "ram_gb": 32, "tier": "32 GB+", "chinese": "一般",
     "desc": "社区口碑最好的 RP 专调之一（Mistral Small 底座），角色性格鲜明、R18 场景自然；英文最佳，中文需靠语言覆盖。",
     "url": "https://huggingface.co/TheDrummer/Cydonia-24B-v4.1-GGUF"},
    {"pull": "hf.co/TheDrummer/Rocinante-12B-v1.1-GGUF:Q4_K_M", "name": "Rocinante 12B v1.1 (TheDrummer)",
     "role": ROLE_CHAT, "size_gb": 7.5, "ram_gb": 16, "tier": "16 GB", "chinese": "一般",
     "desc": "Mistral Nemo 底座的经典 RP 专调，16 GB 机器上英文 RP 的首选，创意强、不啰嗦。",
     "url": "https://huggingface.co/TheDrummer/Rocinante-12B-v1.1-GGUF"},
    {"pull": "hf.co/bartowski/MN-12B-Celeste-V1.9-GGUF:Q4_K_M", "name": "Celeste 12B v1.9",
     "role": ROLE_CHAT, "size_gb": 7.5, "ram_gb": 16, "tier": "16 GB", "chinese": "一般",
     "desc": "以人类写作数据训练的 RP 模型，叙事更像小说、少 AI 味；英文为主。",
     "url": "https://huggingface.co/bartowski/MN-12B-Celeste-V1.9-GGUF"},
    {"pull": "hf.co/bartowski/L3-8B-Stheno-v3.2-GGUF:Q4_K_M", "name": "Stheno 8B v3.2 (Sao10K)",
     "role": ROLE_CHAT, "size_gb": 4.9, "ram_gb": 8, "tier": "8 GB", "chinese": "较弱",
     "desc": "8B 级别最受欢迎的英文 RP 模型，轻快、情绪表达好；中文能力弱，建议只在英文卡上用。",
     "url": "https://huggingface.co/bartowski/L3-8B-Stheno-v3.2-GGUF"},
    {"pull": "hf.co/TheDrummer/Skyfall-36B-v2-GGUF:Q4_K_M", "name": "Skyfall 36B v2 (TheDrummer)",
     "role": ROLE_CHAT, "size_gb": 22.4, "ram_gb": 48, "tier": "48 GB+", "chinese": "一般",
     "desc": "Cydonia 的放大版，细节与长程一致性更好；慢一些。",
     "url": "https://huggingface.co/TheDrummer/Skyfall-36B-v2-GGUF"},
    {"pull": "hf.co/TheDrummer/Anubis-70B-v1.1-GGUF:Q4_K_M", "name": "Anubis 70B v1.1 (TheDrummer)",
     "role": ROLE_CHAT, "size_gb": 42.5, "ram_gb": 64, "tier": "64 GB+", "chinese": "良好",
     "desc": "Llama 3.3 70B 的 RP 专调，目前本地可跑的顶级文笔；64 GB 机器约 8–12 tok/s。",
     "url": "https://huggingface.co/TheDrummer/Anubis-70B-v1.1-GGUF"},
    # ---- extraction (RPG world-state JSON) --------------------------------------------------------
    {"pull": "qwen3:14b", "name": "Qwen3 14B（抽取）", "role": ROLE_EXTRACT, "size_gb": 9.3, "ram_gb": 16,
     "tier": "16 GB", "chinese": "优秀", "recommended": True,
     "desc": "RPG 模式的世界状态抽取模型：读叙述、输出 JSON（新敌人、死亡、地点）。准确率高，32 GB 以上机器与对话模型同时常驻。",
     "url": "https://ollama.com/library/qwen3"},
    {"pull": "qwen3:8b", "name": "Qwen3 8B（抽取）", "role": ROLE_EXTRACT, "size_gb": 5.2, "ram_gb": 8,
     "tier": "8 GB", "chinese": "良好",
     "desc": "更省内存的抽取模型，16 GB 机器与 14B 对话模型搭配时用它。",
     "url": "https://ollama.com/library/qwen3"},
    {"pull": "qwen3:4b", "name": "Qwen3 4B（抽取）", "role": ROLE_EXTRACT, "size_gb": 2.6, "ram_gb": 8,
     "tier": "8 GB", "chinese": "良好",
     "desc": "最小可用的抽取模型，8 GB 机器专用；偶尔漏抽，但不会拖慢对话。",
     "url": "https://ollama.com/library/qwen3"},
]

INTRO = {
    "minimum": [
        "对话模型 ×1：驱动所有 bot 的角色扮演输出。要无审查、支持 R18、中文好。",
        "抽取模型 ×1（仅 RPG 模式需要）：每回合把叙述转成结构化世界状态。要小、快、JSON 稳。可以与对话模型相同，但会变慢。",
    ],
    "tiers": {
        "8 GB": "对话：Qwen3 8B abliterated；抽取：Qwen3 4B。两者不能同时常驻，Ollama 会自动换入换出，RPG 回合会慢。",
        "16 GB": "对话：Qwen3 14B abliterated（中文）或 Rocinante 12B（英文）；抽取：Qwen3 8B。",
        "32 GB+": "对话：Qwen3.6 35B-A3B Uncensored（当前默认）或 Cydonia 24B；抽取：Qwen3 14B。两者可同时常驻。",
        "64 GB+": "对话：Anubis 70B 追求文笔，或继续用 35B-A3B 追求速度；抽取：Qwen3 14B。",
    },
}


def machine_ram_gb() -> int:
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=3).stdout
        return int(int(out.strip()) / (1024 ** 3))
    except Exception:  # noqa: BLE001
        return 0


def tier_for(ram_gb: int) -> str:
    if ram_gb >= 64:
        return "64 GB+"
    if ram_gb >= 48:
        return "48 GB+"
    if ram_gb >= 32:
        return "32 GB+"
    if ram_gb >= 16:
        return "16 GB"
    return "8 GB"


def catalog_for(installed: set[str], ram_gb: int) -> list[dict]:
    out = []
    for m in CATALOG:
        d = dict(m)
        d["installed"] = any(n == m["pull"] or n.split(":")[0] == m["pull"].split(":")[0] and m["pull"].endswith(":latest")
                             for n in installed)
        d["fits"] = ram_gb == 0 or m["ram_gb"] <= ram_gb
        d.setdefault("recommended", False)
        out.append(d)
    return out
