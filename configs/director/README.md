# director/ — Director Prompt presets (planned)

Phase 4+ will ship multiple Director presets here:

```
director/
├── default.yaml           # No looping, advance narrative, no purple prose
├── soft.yaml              # Lighter guidance, more creative freedom
├── strict.yaml            # Heavy formatting / style enforcement
└── README.md
```

Phase 1 ships with the default rules hard-coded in `director.py`. Once stable,
they move into YAML and become swappable per-character.