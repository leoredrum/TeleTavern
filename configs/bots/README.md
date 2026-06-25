# bots/ — per-bot instance overrides (planned)

Phase 4 will let each character bot (Penelope / June / Aqua) have its own YAML:

```
bots/
├── penelope.yaml          # char_label, language, nsfw, sampling overrides
├── june.yaml
├── aqua.yaml
└── README.md
```

Currently the same config is duplicated in each instance's `.env`. After Phase 4
we collapse to: one bot.yaml + one character card + one model profile.