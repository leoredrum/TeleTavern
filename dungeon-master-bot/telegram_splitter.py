#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram 长消息安全分段发送（只修输出层；不动 Prompt / Rule Engine / Director / ST Core）。

提供两个接口：
  - split_text(text, limit=SAFE_LIMIT) -> list[str]
        纯函数：把任意长度文本按自然边界切成 <= limit 的段。
  - send_long_message(message, chat_id, text, first_message=None) -> list
        异步：顺序发送多段（不并发、不乱序），可选复用流式 placeholder 作第一段。

切分原则（只在「行边界」切 → 行内 Markdown ** ` _ ~ 等绝不跨段）：
  1) 空行（段落边界）         最优先
  2) Markdown 标题行 #
  3) 分隔线 --- / *** / ___
  4) 任意换行
  5) （超长单行）句末标点 。！？.!? → 逗号 ，,;；、 → 空格 → 强制切

代码块：切点尽量避开 ``` 代码块内部；若单段代码块本身超长，
        _balance() 在段尾补 ``` 保证结构合法（fence 成对）。
状态栏：HP/XP/Inventory 等状态栏通常是一整段（空行 / 分隔线分隔），
        会自然整体落在最后一条，不会被切一半。
长度：  SAFE_LIMIT = 3800（Telegram 单条硬上限 4096，预留余量不卡上限）。
"""
import re

TELEGRAM_LIMIT = 4096          # Telegram 单条消息硬上限
SAFE_LIMIT = 3800              # 每段安全长度（预留余量）
STREAM_PREVIEW_LIMIT = 3800    # 流式实时预览截断长度（edit_text 同样受 4096 限制）

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_SENT_END = re.compile(r"[。！？!?…]+")
_COMMA = re.compile(r"[，,;；、]")
_WS = re.compile(r"\s")
_SEP_RE = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")


# --------------------------------------------------------------------------- #
# 边界判定
# --------------------------------------------------------------------------- #
def _is_fence(line):
    return bool(_FENCE_RE.match(line or ""))


def _is_good_boundary(line, in_code):
    """该行结束后切是否「优质」（段落 / 标题 / 分隔线）。代码块内一律不算。"""
    if in_code:
        return False
    s = (line or "").strip()
    if s == "":
        return True              # 空行 = 段落边界，最优先
    if s.startswith("#"):
        return True              # Markdown 标题（下一段从标题行开始更整齐）
    if _SEP_RE.match(s):
        return True              # 分隔线
    return False


def _last_good_boundary(cur):
    """cur 中最后一个优质边界行索引（排除最后一行；独立跟踪代码块状态）。"""
    in_code = False
    last = None
    for k, line in enumerate(cur[:-1]):      # 切在最后一行之后 = 没切，故排除
        if _is_fence(line):
            in_code = not in_code
        if _is_good_boundary(line, in_code):
            last = k
    return last


# --------------------------------------------------------------------------- #
# 主切分
# --------------------------------------------------------------------------- #
def split_text(text, limit=SAFE_LIMIT):
    text = "" if text is None else text
    if len(text) <= limit:
        return [text]
    chunks = _split_lines(text, limit)
    return [_balance(c) for c in chunks if c != ""]


def _split_lines(text, limit):
    lines = text.split("\n")
    chunks = []
    cur = []
    cur_len = 0
    in_code = False
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        ll = len(line) + 1                       # +1 给换行符

        # 超长单行：先收尾当前段，再行内切（句末标点优先）
        if ll > limit:
            if cur:
                chunks.append("\n".join(cur))
                cur, cur_len = [], 0
            for sub in _split_long_line(line, limit):
                chunks.append(sub)
            i += 1
            continue

        would_exceed = bool(cur) and cur_len + ll > limit

        if would_exceed:
            # 代码块内：尽量不切，但若已超限只能切（_balance 兜底补 fence）
            if not in_code:
                gi = _last_good_boundary(cur)
                if gi is not None and gi < len(cur) - 1:
                    keep, rest = cur[:gi + 1], cur[gi + 1:]
                    chunks.append("\n".join(keep))
                    cur = rest
                    cur_len = sum(len(x) + 1 for x in cur)
                    continue                   # 重新评估当前 line（不 i+=1）
            # 无优质边界 / 代码块内：在当前累积处切
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0
            continue                           # 重新评估当前 line

        # 正常累积
        if _is_fence(line):
            in_code = not in_code
        cur.append(line)
        cur_len += ll
        i += 1

    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _split_long_line(line, limit):
    """单行 > limit：在句末标点 / 逗号 / 空格处切，最后才强制。"""
    parts = []
    s = line
    while len(s) > limit:
        cut = _best_inline_cut(s, limit)
        if cut <= 0:
            cut = limit
        parts.append(s[:cut])
        s = s[cut:]
    if s:
        parts.append(s)
    return parts


def _best_inline_cut(s, limit):
    window = s[:limit]
    m = None
    for m in _SENT_END.finditer(window):       # 句末标点（取最后一个）
        pass
    if m:
        return m.end()
    m = None
    for m in _COMMA.finditer(window):          # 逗号 / 分号 / 顿号
        pass
    if m:
        return m.end()
    m = None
    for m in _WS.finditer(window):             # 空格
        pass
    if m:
        return m.end()
    return limit                                # 强制切


def _balance(chunk):
    """保证代码块 fence 成对：未闭合则段尾补 ```。"""
    fences = re.findall(r"^\s*(```|~~~)", chunk, re.MULTILINE)
    if len(fences) % 2 == 1:
        chunk = chunk.rstrip() + "\n```"
    return chunk


# --------------------------------------------------------------------------- #
# 统一发送接口
# --------------------------------------------------------------------------- #
async def send_long_message(message, text,
                            first_message=None, parse_mode=None):
    """顺序发送长消息（不并发、不乱序）。

    message      : 有 reply_text 的对象（通常是 update.message）。
    text         : 任意长度文本。
    first_message: 可选，流式占位 Message；若提供，第一段复用它（edit_text），
                   其余段顺序 reply_text —— 保证 Part1→Part2→Part3 顺序。
    parse_mode   : 可选 'Markdown' / 'MarkdownV2' / 'HTML'；失败自动回退纯文本。
    返回：已发送的 Message 列表。
    """
    chunks = split_text(text)
    if not chunks:
        return []
    sent = []

    head, rest = chunks[0], chunks[1:]
    if first_message is not None:
        try:
            await first_message.edit_text(head)
        except Exception:
            pass                                # placeholder 编辑失败不阻塞后续
        sent.append(first_message)
    else:
        sent.append(await _safe_send(message, head, parse_mode))

    for chunk in rest:
        sent.append(await _safe_send(message, chunk, parse_mode))
    return sent


async def _safe_send(message, chunk, parse_mode):
    try:
        return await message.reply_text(chunk, parse_mode=parse_mode) \
            if parse_mode else await message.reply_text(chunk)
    except Exception:
        # parse_mode 解析失败 → 纯文本重试，绝不丢内容
        try:
            return await message.reply_text(chunk)
        except Exception:
            return None
