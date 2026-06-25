# Deployment

每个 bot 实例是一个独立的 deployment：自带 `venv/`、`data/`、`.env`、日志。
本仓库是规范代码源；bot 实例目录里的 `.py` 是 `run.sh` 用的本地副本（gitignored）。

## 目录布局

```
~/Documents/telegramtavern/                  ← 本仓库（source of truth）
├── bot.py / pipeline.py / director.py / ... ← 规范代码
├── characters/<name>/                        ← 角色卡 PNG（gitignored 之外的备份）
├── docs/                                     ← 架构 / 部署 / Phase 报告
├── scripts/
│   ├── start.sh / stop.sh / restart.sh
│   ├── healthcheck.sh
│   └── check_config.sh                       ← 配置 + secret 自检（新增）
├── .env.example                              ← 根级模板
├── README.md
└── penelope/  june/  aqua/                   ← 三个 bot 实例目录
    ├── README.md              ← gitignored 之外的说明
    ├── .env.example           ← bot 专属 env 模板
    ├── .env                   ← gitignored — 含真实 token
    ├── data/                  ← gitignored — character PNG + *.db
    ├── venv/                  ← gitignored
    ├── bot.log                ← gitignored
    └── run.sh                 ← gitignored — 启动脚本
```

## 新机器从零部署

```bash
# 1. 克隆仓库
git clone <repo-url> ~/Documents/telegramtavern
cd ~/Documents/telegramtavern

# 2. 对每个 bot：复制 env 模板，填入 token
for bot in penelope june aqua; do
  cp "$bot/.env.example" "$bot/.env"
  echo "→ 编辑 $bot/.env，填入 TELEGRAM_BOT_TOKEN（向 @BotFather 拿）"
done

# 3. 准备角色卡（已经迁过来的可以跳过）
#    Penelope:  data/Penelope3.png
#    June:      data/June.png
#    Aqua:      data/Aqua.png

# 4. 启动 + 自检
./scripts/check_config.sh
cd penelope && ./run.sh --bg && cd ..
cd june     && ./run.sh --bg && cd ..
cd aqua     && ./run.sh --bg && cd ..
```

## 跑配置自检

```bash
./scripts/check_config.sh              # 三个 bot 都查
./scripts/check_config.sh penelope     # 单个
```

`check_config.sh` 会校验：

| 检查项 | 失败时代表什么 |
|---|---|
| `penelope/.env` 存在 | 还没 `cp .env.example .env` |
| `TELEGRAM_BOT_TOKEN` 非空 | token 没填 |
| `CHARACTER_PATH` 文件存在 | 角色卡 PNG 没放 / 路径写错 |
| `DB_PATH` 父目录存在 | `data/` 还没建 |
| `SHARED_DB_PATH` 父目录存在 | 同上 |
| `.env` 没被 git track | **UNSAFE — 立刻 `git rm --cached` + 加进 `.gitignore`** |
| `.env.example` 已被 git track | 模板没提交；下次新机器拉不到 |
| tracked 文件里没有真实 token | 之前误提交过；需要 `git filter-repo` 或 BFG 清理历史 |

## .gitignore 策略

```gitignore
# secrets
.env
.env.*
!.env.example
!**/.env.example

# per-bot: 只 track README.md + .env.example
/penelope/*
/penelope/.*
!/penelope/README.md
!/penelope/.env.example
# 同上 june / aqua

# runtime
data/  **/data/  *.db
*.log  logs/
venv/  .venv/
```

**为什么不全 ignore 整个 bot 目录？** 之前 `/penelope/` 是被整目录 ignore 的，
意味着 `.env.example` 也进不了 git，新机器克隆下来看不到模板。把策略改成
`/*` + 白名单 `README.md` / `.env.example` 之后，模板随仓库发布，token 仍然只
留在本地 `.env` 里。

## 启动后常见问题

| 症状 | 排查 |
|---|---|
| Bot 不响应 Telegram | `tail -f <bot>/bot.log`；常见是 `Conflict: terminated by other getUpdates` —— 另一个实例还占着这个 token |
| Ollama 报 `connection refused` | `ollama serve` 起了吗？`curl http://127.0.0.1:11434/api/tags` 试一下 |
| 角色卡加载失败 | `CHARACTER_PATH` 是否指向带 `tEXt chara` chunk 的 PNG？V2/V3 JSON 也行 |
| 中文不稳定 | `REPLY_LANGUAGE=zh-CN` 已设；Qwen3 默认 thinking 模式下偶尔漏中文，需要 `think:false`（见 `docs/phase1_delivery.md`） |

## 回滚

`.env` 不在 git 里，回滚 git 不会丢 token。但每个 bot 实例目录里的
`data/chat.db` 也不在 git 里 —— 删实例目录 = 丢历史对话。
备份建议：定期 `cp <bot>/data/chat.db ~/backups/`。
