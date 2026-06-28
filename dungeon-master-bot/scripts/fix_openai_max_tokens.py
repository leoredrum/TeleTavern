#!/usr/bin/env python3
"""精确修改 ST settings.json 的 openai_max_tokens（默认 1024）→ 目标值（4096）。
- 先备份 settings.json.bak-maxtokens
- 用正则只替换该字段的值，保留文件其余格式/顺序（不重新 json.dump，避免重排）
- 校验改后仍是合法 JSON
用法：fix_openai_max_tokens.py <settings.json 路径> <新值>
"""
import re
import sys
import json
import shutil

p = sys.argv[1]
new_val = int(sys.argv[2]) if len(sys.argv) > 2 else 4096
shutil.copy(p, p + ".bak-maxtokens")
s = open(p, encoding="utf-8").read()
m = re.search(r'"openai_max_tokens"\s*:\s*(-?\d+)', s)
old_val = m.group(1) if m else "<not found>"
new = re.sub(r'("openai_max_tokens"\s*:\s*)-?\d+', r'\g<1>%d' % new_val, s, count=1)
assert new != s, "ERROR: no change made (openai_max_tokens not found or already target)"
json.loads(new)  # 校验仍是合法 JSON
open(p, "w", encoding="utf-8").write(new)
m2 = re.search(r'"openai_max_tokens"\s*:\s*(-?\d+)', new)
print("openai_max_tokens: %s -> %s  JSON_valid=yes backup=.bak-maxtokens"
      % (old_val, m2.group(1)))
