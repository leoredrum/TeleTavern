# models/ — per-model config presets (planned)

Phase 4 (Model Adapter) will populate this directory with one file per model profile:

```
models/
├── qwen3-32b.yaml          # default Qwen3 32B
├── qwen3-35b-a3b.yaml      # Qwen3.6 35B-A3B aggressive (current prod)
├── qwen3-14b.yaml          # smaller / faster
└── README.md
```

Each file declares: model id, sampling defaults (temperature, top_p, repeat_penalty),
context window, stop tokens, system prefix.