# Follow-ups from the Intelligence pane redesign review (2026-07-11)

- **Legacy auto-namer bypasses the consent matrix (needs a Linear ticket — high priority).**
  `src/screencap/namer.py` runs default-on after recording stop (daemon
  `RecordingStartRequest` carries no `auto_name`/`local_only` fields, so
  `session.py` defaults `auto_name_enabled=True, local_only=False`) and sends
  UNSTRIPPED window titles/action events/transcript — and screenshots via
  `_try_anthropic_api`/`_try_openai_api` (`screenshots_b64` image blocks) — to
  cloud models whenever vendor keys/CLIs are present, ignoring the Intelligence
  provider selection, the consent toggles, and the privacy strip. Verified
  independently twice during the pane-redesign review. Options: route the namer
  through the consent policy + `activity_summary` strip, or default its cloud
  chain off. Until fixed, pane copy stays scoped to "summaries, answers, and
  day-splitting" (do not restore "any model" claims).
- **CLI ordering contract for the two-slot selection.** The pane encodes
  clear-`cloud_provider`-first / heal-then-select ordering purely client-side;
  `screencap settings intelligence` exposes the two writes independently with
  no documented ordering. Either document the contract in the CLI help or add
  an atomic `select <row>` verb so scripts/agents can't produce the dual-slot
  confusion state (agent-native review finding).
- **Capture the optimistic-flip pattern** (flip → revert-on-failure →
  error-after-reconcile → per-row serialization) as a `docs/solutions/`
  design-patterns entry — third controller now uses it (learnings-researcher
  suggestion).
