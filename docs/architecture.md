# Telegram RP Bot — 架构重构方案
## 基于 SillyTavern 架构思想

> 本文档：分析 SillyTavern 设计思想 → 提出 Telegram Bot 的等效实现
> 不复制代码，只学习架构。UI/前端相关内容跳过。

---

## 一、SillyTavern 核心设计思想提炼

### 1. Prompt Pipeline（核心）

SillyTavern 的 prompt 不是一个大字符串，而是一个**模块化 Itemized 管道**。

**Prompt Item 的结构：**
```
{
  id: "main_prompt" | "char_description" | "lore" | "summary" | ...
  role: "system" | "user" | "assistant"
  content: str
  position: "absolute" | "in_chat"
  depth: int          # 插入到 chat history 的第几层（0=最后，1=倒数第2，...）
  enabled: bool
  triggers: ["normal", "continue", "swipe", ...]
}
```

**组装顺序（由上到下，优先级递增）：**

```
[System Prompt]          ← 主指令，告诉模型"做什么"
[Character Description]  ← 角色描述
[Personality]            ← 性格
[Scenario]              ← 场景设定
[Lorebook / World Info] ← 按关键词动态注入
[Persona Description]   ← 用户人设
[Example Dialogues]     ← 示例对话
[Chat History]          ← 真实对话历史
[Author's Note]         ← 深度注入（在 chat history 内部）
[Post-History Instruc.]  ← 最后指令，最高优先级
```

**关键设计：**
- `{{anchorBefore}}` / `{{anchorBefore}}` — 在 char defs 前后插入扩展内容
- `Author's Note` — 在 chat history 内部特定深度插入，绕过历史限制强化指令
- `Post-History Instructions` — 在 chat history 之后，作为最后指令，优先级高于 main prompt

### 2. World Info / Lorebook

**SillyTavern 的 Lorebook 设计：**

```
Entry {
  keys: list[str]              # 激活关键词（支持正则）
  keysecondary: list[str]      # 次要关键词 + 逻辑（AND ANY / AND ALL / NOT ANY / NOT ALL）
  content: str                 # 实际插入 prompt 的内容
  insertion_position: enum     # before_char_defs | after_char_defs | before_examples | after_examples | top_of_AN | bottom_of_AN | @depth
  depth: int                   # 如果是 @depth 位置
  order: int                   # 插入顺序（越大越靠后，影响越大）
  scan_depth: int              # 扫描聊天历史多少条消息
  context_budget: float        # 最大 token 占比
  probability: int             # 触发概率（0-100）
  sticky: int / cooldown: int / delay: int  # 定时效果
  group: str                  # 互斥组（组内只选一个）
  recursive: bool             # 是否允许递归触发
  constant: bool              # 无需关键词，始终激活
}
```

**关键词匹配逻辑：**
- 扫描 chat history 最近 N 条消息
- 支持"扫描 Chat Lore / Persona Lore / Character Lore / Global Lore" 分层
- 递归：Entry A 的 content 里提到 Entry B 的 key → B 也被激活
- Token budget：最多占用 prompt 的 X%，防止 lore 塞爆

### 3. Memory / Summary

**SillyTavern 的 Summary 设计：**

- Summary 作为**消息锚点**：summary 附加在特定 msg_id 上
- 当旧消息被裁掉时，summary 留在 context 里，继续提供背景
- 自动触发：每 X 条消息 或 每 Y tokens
- 注入方式：`{{summary}}` 宏 + injection position（before main prompt / after main prompt / @depth）

**本质：** 不是简单压缩，而是"情节摘要"——保留事实性剧情，丢弃细节

### 4. Character Card 结构

SillyTavern 支持 V2/V3 标准：

```
CharacterCard {
  spec: "chara_card_v2" | "chara_card_v3"
  name: str
  description: str            # Always included
  personality: str             # Always included
  scenario: str               # Always included
  first_mes: str              # Only at start
  mes_example: str            # Pushed out when context full (or forced keep)
  creator_notes: str
  system_prompt: str           # Prompt override ( Prefer Char. Prompt )
  post_history_instructions: str  # Post-History override
  character_book: dict         # V3 内嵌 lorebook
  alternate_greetings: list[str]
  depth_prompts: list[DepthPrompt]  # 深度注入：@depth N, role, content
}
```

### 5. Context 管理

**SillyTavern 的 context 策略：**
1. 优先保留 Character Card 的 permanent tokens
2. Example dialogues 可被强制保留或逐步淘汰
3. Chat history 按消息顺序入栈，超出 context 时从最老的消息开始裁掉
4. Summary 作为"历史快照"，即使旧消息被裁掉也保留情节
5. Author's Note 可以在任意深度注入

**裁剪优先级：**
```
永久保留 > Example Messages (force) > Summary > Example Messages > Chat History
```

### 6. Group Chats（多角色）

- 每个角色有独立的 Character Card、Chat History、Depth Prompts
- 群聊的 chat history 包含所有角色的消息（按顺序）
- 激活策略：NATURAL（自然顺序）、LIST（指定顺序）、MANUAL（手动选择）
- 每个角色有独立的 `talkativeness` 概率

### 7. Chat Templates

SillyTavern 根据模型 hash 选择 chat template：
- Llama 3, Mistral V2/V3, Gemma 2, DeepSeek, Qwen 等
- 每个 template 有 `context` 和 `instruct` 两个模板
- 用于把消息数组渲染成特定格式的 prompt 字符串

### 8. Sampling

每个 Preset 独立配置：
```
Temperature, Top P, Top K, Min P, Repeat Penalty,
Presence Penalty, Frequency Penalty, ...
```

---

## 二、Telegram Bot 目标架构

### 设计原则

1. **Pipeline 化**：Prompt 由多个独立 Block 组装，每个 Block 可开启/关闭/调顺序
2. **Lorebook 动态注入**：基于关键词，不是静态塞入
3. **Summary 作为历史压缩**：不是简单截断，而是保留情节
4. **Depth Injection**：支持在 chat history 特定深度注入指令
5. **多角色预留**：架构上支持群聊 + 多角色
6. **Chat Template 抽象**：适配 Qwen / Llama 等不同模型模板
7. **完全解耦**：Lorebook、Memory、Character、Director 都是独立模块

---

## 三、模块设计

### 3.1 PromptItem（最小单元）

```python
@dataclass
class PromptItem:
    """Prompt 管道中的单个 Item"""
    id: str                           # 唯一标识
    role: Literal["system", "user", "assistant"]
    content: str                      # 内容（为空时使用 resolver）
    position: Literal["absolute", "in_chat"] = "absolute"
    depth: int = 0                   # in_chat 时：0=最后，1=倒数第2，...
    enabled: bool = True
    triggers: set[str] = field(default_factory=set)  # "normal", "continue", ...
    resolver: Callable[[], str] | None = None  # 懒加载内容
    priority: int = 0                # 同一 position+depth 时的顺序

    def resolved_content(self) -> str:
        return self.content if self.content else (self.resolver() if self.resolver else "")
```

### 3.2 Lorebook（世界设定）

```python
@dataclass
class LoreEntry:
    """单个 Lore 条目"""
    uid: int
    keys: list[str]                  # 主关键词
    keysecondary: list[str]          # 次要关键词
    selective_logic: str             # "AND_ANY", "AND_ALL", "NOT_ANY", "NOT_ALL"
    content: str
    position: LorePosition           # 见下方
    depth: int = 0
    order: int = 0
    scan_depth: int = 10
    probability: int = 100           # 触发概率 0-100
    sticky: int = 0
    cooldown: int = 0
    delay: int = 0
    group: str | None = None
    group_weight: int = 100
    constant: bool = False
    recursive: bool = False
    enabled: bool = True

class LorePosition(Enum):
    BEFORE_CHAR_DEFS = "before_char_defs"      # char description 之前
    AFTER_CHAR_DEFS = "after_char_defs"        # char description + personality + scenario 之后
    BEFORE_EXAMPLES = "before_examples"
    AFTER_EXAMPLES = "after_examples"
    TOP_OF_AN = "top_of_an"
    BOTTOM_OF_AN = "bottom_of_an"
    AT_DEPTH = "at_depth"                      # 深度注入
    OUTLET = "outlet"                          # 命名出口，需手动引用


@dataclass
class Lorebook:
    """Lorebook 引擎"""
    entries: list[LoreEntry]
    scan_depth: int = 10
    include_names: bool = True
    token_budget_pct: float = 0.25             # 最多占 prompt 的 25%
    active_effects: dict[int, dict] = field(default_factory=dict)  # sticky/cooldown

    def activate(self, chat_text: str, scan_depth: int | None = None) -> list[LoreEntry]:
        """扫描 chat_text，返回被激活的 entries"""

    def compute_budget(self, max_context_tokens: int) -> int:
        """根据 token budget 计算最多可激活多少 entries"""

    def resolve_sticky_cooldown(self, msg_count: int) -> None:
        """处理定时效果"""
```

### 3.3 Memory / Summary

```python
@dataclass
class ChatSummary:
    """情节摘要（附加在特定 msg_id）"""
    attached_msg_id: int            # 附加在哪条消息之后
    text: str                       # 摘要内容
    created_at: float
    # 原始历史范围（方便调试/编辑）
    first_msg_id: int
    last_msg_id: int


@dataclass
class MemoryManager:
    """Memory 管理器"""
    summaries: list[ChatSummary]     # 按时间顺序
    update_every_n_messages: int = 20
    update_every_n_tokens: int = 0
    injection_template: str = "[情节摘要]\n{{summary}}\n[/情节摘要]\n"
    injection_position: InjectionPosition = InjectionPosition.BEFORE_CHAT

    async def generate_summary(
        self,
        history: list[dict],
        ollama_client,
        char_label: str,
    ) -> str:
        """调用 LLM 生成情节摘要"""

    def attach_summary(self, msg_id: int, text: str, first_msg: int, last_msg: int) -> None:
        """将摘要附加到特定消息"""

    def get_latest_summary(self) -> str | None:
        """获取最新的情节摘要"""

    def prune_older_than(self, msg_id: int) -> None:
        """删除附加在 msg_id 之前的摘要"""
```

### 3.4 Character Card（已是 V2/V3，扩展 depth prompt）

```python
@dataclass
class DepthPrompt:
    """深度注入的 prompt"""
    depth: int                      # 在 chat history 第几层注入（0=最后）
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass
class ExtendedCharacterCard(CharacterCard):
    """扩展角色卡，兼容 V2/V3"""
    depth_prompts: list[DepthPrompt] = field(default_factory=list)

    @classmethod
    def from_standard_card(cls, card: CharacterCard) -> "ExtendedCharacterCard":
        """从标准 CharacterCard 升级"""
```

### 3.5 Director Prompt（已有的，升级）

```python
class DirectorConfig:
    """Director 层配置"""
    repeat_ban_phrases: list[str] = field(default_factory=_DEFAULT_REPEAT_BAN)
    max_reply_chars: tuple[int, int] = (80, 280)  # 短-长范围
    no_option_listing: bool = True
    no_meta_commentary: bool = True
    proactive_role: bool = True
    advance_narrative: bool = True


def build_director_block(char_label: str, user_label: str, config: DirectorConfig) -> PromptItem:
    """把 Director 规则生成一个 PromptItem"""
```

### 3.6 Chat Template（适配不同模型）

```python
class ChatTemplate(ABC):
    """Chat Template 抽象基类"""

    @abstractmethod
    def render(self, messages: list[dict]) -> str:
        """把消息列表渲染成模型所需的格式"""

    @property
    @abstractmethod
    def model_family(self) -> str:
        """模型家族：qwen | llama | mistral | gemma | deepseek"""


class QwenChatTemplate(ChatTemplate):
    """Qwen 官方模板"""
    def render(self, messages):
        # <|im_start|>user\ncontent<|im_end|>\n<|im_start|>assistant\n...


class LlamaChatTemplate(ChatTemplate):
    """Llama 3 模板"""


class DefaultChatTemplate(ChatTemplate):
    """SillyTavern 风格：直接拼接"""


class ChatTemplateRegistry:
    """模板注册表"""
    _templates: dict[str, type[ChatTemplate]] = {}

    @classmethod
    def register(cls, model_family: str, template_cls: type[ChatTemplate]):
        cls._templates[model_family] = template_cls

    @classmethod
    def get_for_model(cls, model_name: str) -> ChatTemplate:
        # 根据 model name 推断家族，注册对应 template
```

### 3.7 Sampling Config（模型级配置）

```python
@dataclass
class SamplingConfig:
    """采样参数（per-model 可覆盖）"""
    temperature: float = 1.05
    top_p: float = 0.93
    top_k: int = 0               # 0 = disabled
    min_p: float = 0.0
    repeat_penalty: float = 1.08
    presence_penalty: float = 0.0
    frequency_penalty: float = 0.0

    def to_ollama_payload(self) -> dict:
        return asdict(self)


# 模型预设
MODEL_PRESETS: dict[str, SamplingConfig] = {
    "qwen3": SamplingConfig(temperature=1.05, top_p=0.93, repeat_penalty=1.08),
    "llama3": SamplingConfig(temperature=1.0, top_p=0.9, repeat_penalty=1.1),
    "mistral": SamplingConfig(temperature=1.0, top_p=0.95, repeat_penalty=1.05),
    "default": SamplingConfig(),
}
```

### 3.8 Prompt Pipeline（核心编排器）

```python
class PromptPipeline:
    """Prompt 组装管道"""

    def __init__(
        self,
        card: ExtendedCharacterCard,
        lorebook: Lorebook | None = None,
        memory: MemoryManager | None = None,
        director_config: DirectorConfig | None = None,
        template: ChatTemplate | None = None,
        persona: Persona | None = None,
    ):
        ...

    def build_items(
        self,
        history: list[dict],
        user_message: str,
        generation_type: str = "normal",
    ) -> list[PromptItem]:
        """组装所有 PromptItem（不拼接，返回有序列表）"""

    def assemble(
        self,
        history: list[dict],
        user_message: str,
        generation_type: str = "normal",
    ) -> list[dict]:
        """返回最终的 messages 列表（符合 Ollama chat API 格式）"""

    # Pipeline 顺序（绝对位置）
    POSITION_ORDER = [
        "system_anchor",       # 0: role anchor（"You are X..."）
        "language_override",   # 1: 语言指令
        "director",            # 2: Director 规则
        "lore_before",        # 3: Lorebook（before char defs）
        "char_defs",          # 4: Character card sections
        "lore_after",         # 5: Lorebook（after char defs）
        "persona",            # 6: Persona description
        "lore_examples",      # 7: Lorebook（example 位置）
        "examples",           # 8: Example dialogues
        "summary",            # 9: Memory summary
        "chat_history",       # 10: Real chat history
        "depth_injects",     # 11: Depth injections（在 chat history 内部）
        "phi",               # 12: Post-History Instructions（最后，最高优先级）
    ]
```

---

## 四、Pipeline 执行流程

```
用户发送消息
    ↓
Pipeline.build_items()
    ├── Director Prompt ← DirectorConfig
    ├── Lorebook.activate() ← 扫描 chat history
    │     ├── 关键词匹配
    │     ├── Token budget 裁剪
    │     └── Sticky/Cooldown 处理
    ├── MemoryManager.get_latest_summary() ← 情节摘要
    ├── CharacterCard.sections → PromptItem
    ├── Persona.description → PromptItem
    ├── Example Dialogues → PromptItem
    ├── Chat History (原始消息)
    └── Depth Prompts → 按 depth 插入
    ↓
按 POSITION_ORDER 组装
    ↓
ChatTemplate.render(messages)
    ↓
OllamaClient.stream_chat(payload, sampling=SamplingConfig)
    ↓
流式返回给 Telegram
```

---

## 五、群聊（多角色）架构预留

```python
@dataclass
class ChatParticipant:
    """群聊参与者"""
    participant_id: str
    card: ExtendedCharacterCard
    persona: Persona | None
    lorebook: Lorebook | None
    memory: MemoryManager
    recent_messages: list[dict]    # 该角色的最近消息
    talkativeness: float = 0.5    # 激活概率
    enabled: bool = True


@dataclass
class GroupChatState:
    """群聊状态"""
    chat_id: str
    participants: dict[str, ChatParticipant]  # user_id → participant
    activation_strategy: str = "natural"  # "natural" | "list" | "manual"
    last_speaker: str | None = None
    auto_continue: bool = True
```

---

## 六、实现计划

### Phase 1：核心管道（2-3 小时）

**目标：** 把当前 bot 升级为模块化 Prompt Pipeline

1. `prompt_item.py` — PromptItem dataclass
2. 重构 `prompt_builder.py`：
   - `PromptPipeline` 类（核心编排器）
   - `DirectorConfig`（Director 层配置）
   - `ExtendedCharacterCard`（兼容标准卡 + depth prompts）
   - `DepthPrompt`（深度注入）
3. 更新 `bot.py` — 使用 `Pipeline.build()` 而不是直接拼接
4. 更新 `ollama_client.py` — 支持 `SamplingConfig`
5. 保留向后兼容：`build_system_prompt()` 依然可用

**验证：** 发消息测试，回复风格与之前一致

### Phase 2：Lorebook（3-4 小时）

**目标：** 实现关键词动态注入

1. `lorebook.py` — Lorebook 引擎
   - `LoreEntry` dataclass
   - `Lorebook.activate(chat_text)` — 关键词匹配
   - Token budget 裁剪
   - Sticky / Cooldown / Delay 定时效果
   - Recursive 触发
   - Inclusion Group 互斥选择
2. Lorebook 文件格式：JSON（与 SillyTavern 兼容）
3. `CharacterCard.character_book` → 自动注册到 Lorebook
4. 支持 `{{outlet::name}}` 宏
5. 集成到 `PromptPipeline`

**验证：** 定义一个有关键词的 lore entry，发送包含关键词的消息，确认 lore 被注入

### Phase 3：Memory / Summary（2-3 小时）

**目标：** 情节摘要替代简单截断

1. 重构 `MemoryManager`：
   - Summary 作为消息锚点
   - `generate_summary()` — 调用 LLM 生成摘要
   - `attach_summary()` — 附加到特定 msg_id
   - `prune_older_than()` — 删除旧摘要
   - Injection template + position
2. 自动摘要触发逻辑（每 N 条消息）
3. 集成到 `PromptPipeline`

**验证：** 发送 20+ 条消息，确认 summary 正确生成并注入

### Phase 4：Chat Template + Sampling（1-2 小时）

**目标：** 支持多种模型模板

1. `chat_template.py` — ChatTemplate 抽象 + Qwen / Llama / Default 实现
2. `sampling.py` — SamplingConfig + MODEL_PRESETS
3. 从 config.yaml 读取模型家族，自动选择 template
4. 更新 `ollama_client.py` — 使用 `SamplingConfig`

**验证：** 在不同模型上测试，确认格式正确

### Phase 5：多角色 / 群聊架构（预研）

**目标：** 架构预留，不需要实现 UI

1. `ChatParticipant` / `GroupChatState` dataclass
2. `MultiCharPipeline` — 多角色 prompt 拼接
3. 群聊激活策略（Natural / List / Manual）
4. 群聊消息路由

---

## 七、文件结构（目标）

```
sillytavern-telegram-bot/
├── prompt_item.py          # PromptItem dataclass
├── lorebook.py             # Lorebook 引擎
├── memory.py               # MemoryManager + Summary
├── chat_template.py        # ChatTemplate 抽象
├── sampling.py              # SamplingConfig + presets
├── director.py              # DirectorConfig + Director Prompt 生成
├── pipeline.py              # PromptPipeline（核心编排器）
├── prompt_builder.py        # 兼容层（保留，向后兼容）
│
├── bots/                   # 多角色支持（Phase 5）
│   ├── base.py             # BaseBot
│   ├── solo.py             # SoloBot（当前单 bot）
│   └── group.py            # GroupBot（群聊）
│
├── lorebooks/              # Lorebook 数据目录
│   ├── default.json        # 默认 lorebook
│   └── character_lore/     # 角色专属 lorebook
│
├── character_card.py       # ExtendedCharacterCard + DepthPrompt
├── persona.py              # Persona dataclass
├── config.py               # SamplingConfig + 模型预设
├── ollama_client.py        # 使用 SamplingConfig
├── bot.py                  # 使用 PromptPipeline
├── db.py                   # Summary 存储扩展
```

---

## 八、关键设计决策

### Q1: 为什么不直接用 SillyTavern 的源码？

SillyTavern 是 Node.js + 前端 UI 的混合体：
- prompt assembly 在前端 JS（`script.js` 的 `getCombinedPrompt()`）
- World Info 引擎在 `world-info.js`（浏览器端，大量 DOM 依赖）
- 数据存储在前端 `localforage`（IndexedDB）
- 很多逻辑与 UI 紧耦合

我们只需要**后端 prompt 逻辑**，用 Python 重新实现，纯异步，无 UI 依赖。

### Q2: 为什么不一开始就做群聊？

群聊涉及：消息路由、多角色状态管理、激活策略、发言顺序。复杂度高，但架构上预留了接口。先把单角色做到 SillyTavern 级别，再扩展到多角色。

### Q3: Lorebook 数据存储在哪里？

每个角色可以绑定一个 `.json` lorebook 文件（放在 `lorebooks/{char_name}/`）。格式与 SillyTavern 兼容，可以直接导入。也可以通过 Bot 指令管理（`/lore add ...` / `/lore list`）。

### Q4: Summary 用什么模型生成？

用同一个 Ollama 实例 + `HauhauCS` 模型生成摘要。Summary prompt 专门设计为简短事实性摘要（3-5 句话），不会太长。

### Q5: 如何保持向后兼容？

`prompt_builder.py` 保留 `build_system_prompt()` 和 `RPBuilder`，逐步废弃。新代码用 `PromptPipeline`。bot.py 不感知底层细节。
