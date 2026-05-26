# Auto-seeded by eidolon-memory-supervisor on first start.
# Once users.yaml exists at the resolved path, the supervisor will never
# overwrite it — edit freely. See README §7.1 for the full schema.

users:
  - id: default
    port: 8030
    enabled: true
    # Phase 4 — opt-in background theme worker. Uncomment to have the
    # supervisor also spawn `eidolon-memory-consolidator` for this user.
    # consolidator:
    #   enabled: true
    #   interval_hours: 6        # how often to refresh themes
    #   window_days: 30          # look-back when distilling themes
    #   min_drawers: 3           # skip wings with fewer drawers
    #   min_confidence: 0.6      # drop low-confidence themes
