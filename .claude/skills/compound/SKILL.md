---
name: compound
description: Document a solved problem, completed feature, porting decision, or architecture learning to compound team knowledge. Auto-triggers on "that worked", "it's fixed", "feature complete", "porting done". Creates one MD file per item in docs/solutions/[category]/. Use after research, planning, implementation, or debugging sessions.
disable-model-invocation: false
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent
---

# Compound — Capture Knowledge While It's Fresh

**Purpose:** After you finish research, planning, implementation, or debugging — document what you learned so the next conversation starts smarter, not from scratch.

**One file per item.** `docs/solutions/[category]/[filename].md` with YAML frontmatter for searchability.

## When to Invoke

**Auto-detect phrases:** "that worked", "it's fixed", "working now", "feature complete", "porting done", "problem solved", "implementation done"

**Manual:** `/compound [brief context]`

**Good candidates:**
- Non-trivial bugs that took investigation
- Feature implementations with design decisions
- Cross-platform porting decisions (macOS ↔ Windows)
- Architecture choices with trade-offs
- Dependency decisions (why X over Y)
- Performance discoveries
- Privacy system rules learned

**Skip:** Simple typos, obvious fixes, trivial one-liners.

## Execution

### Phase 1: Gather Context (from conversation history)

Extract ALL of these from the current conversation:

**Required:**
- **What was done:** Feature, bug fix, porting task, research finding
- **Problem/Goal:** What triggered this work
- **Symptoms:** Error messages, user-visible behavior, or feature requirements
- **Solution:** What actually worked (code changes, config, architecture)
- **Why it works:** Technical explanation of root cause or design rationale

**If available:**
- **What didn't work:** Failed approaches and why they failed
- **Platform specifics:** macOS vs Windows differences discovered
- **Dependencies involved:** Libraries, APIs, Win32 calls, pyobjc frameworks
- **Files changed:** Key files with line numbers
- **Prevention/gotchas:** How to avoid issues next time

**If critical context is missing, ask:**
```
I need a few details to document this properly:
1. What was the main problem/goal?
2. Which files/modules were involved?
3. What was the key insight or root cause?
```

### Phase 2: Classify and Generate Path

**Determine `problem_type` from context:**

| problem_type | Category Directory | When to Use |
|---|---|---|
| `bug_fix` | `docs/solutions/bug-fixes/` | Fixed a bug (crash, wrong behavior) |
| `feature` | `docs/solutions/features/` | Implemented new functionality |
| `porting` | `docs/solutions/porting/` | Cross-platform macOS ↔ Windows work |
| `performance` | `docs/solutions/performance/` | Speed, memory, or resource optimization |
| `architecture` | `docs/solutions/architecture/` | Design decisions, refactors, patterns |
| `dependency` | `docs/solutions/dependencies/` | Library choices, API decisions, version issues |
| `privacy` | `docs/solutions/privacy/` | Privacy system rules, PII detection, scrubbing |
| `build_packaging` | `docs/solutions/build-packaging/` | PyInstaller, Inno Setup, CI/CD, distribution |
| `testing` | `docs/solutions/testing/` | Test infrastructure, fixtures, CI |
| `config` | `docs/solutions/config/` | Config system, paths, env vars |

**Generate filename:** `[slug]-[YYYYMMDD].md`
- Lowercase, hyphens, no special chars, < 80 chars
- Example: `uwp-applicationframehost-resolution-20260317.md`

**Check for existing similar docs:**
```bash
grep -rl "[key symptom or topic]" docs/solutions/ 2>/dev/null
```

### Phase 3: Write the Document

Create the file at `docs/solutions/[category]/[filename].md`.

**Use this exact structure:**

```markdown
---
title: [Clear descriptive title]
date: [YYYY-MM-DD]
problem_type: [from enum above]
component: [primary module/subsystem affected]
platform: [macos|windows|cross-platform]
severity: [critical|high|medium|low]
symptoms:
  - "[Observable symptom 1]"
  - "[Observable symptom 2]"
root_cause: [1-line technical root cause]
tags: [keyword1, keyword2, keyword3]
files_changed:
  - [path/to/key/file1.py]
  - [path/to/key/file2.py]
---

# [Clear Problem/Feature Title]

## Problem / Goal
[1-3 sentences: what triggered this work, what was observed or needed]

## What Didn't Work
[Skip this section if solution was immediate]

**Attempt 1:** [What was tried]
- **Why it failed:** [Technical reason]

**Attempt 2:** [What was tried]
- **Why it failed:** [Technical reason]

## Solution

[The actual fix/implementation that worked]

**Key changes:**
```python
# Before (if bug fix):
[problematic code]

# After:
[fixed code with explanation]
```

**Files changed:**
- `path/to/file.py:123` — [what changed and why]

## Why This Works

[Technical explanation — root cause analysis for bugs, design rationale for features]

1. [Key insight 1]
2. [Key insight 2]

## Platform Notes
[Skip if not relevant]

| Aspect | macOS | Windows |
|--------|-------|---------|
| [API] | [macOS approach] | [Windows approach] |

## Prevention / Gotchas

- [How to avoid this issue in future]
- [What to watch out for]
- [Testing strategy]

## Related

- [Link to related docs/solutions/ files if any]
- [Link to relevant GitHub issues/PRs]
```

### Phase 4: Present Results

```
✓ Knowledge compounded

File created:
  docs/solutions/[category]/[filename].md

Summary:
  Type: [problem_type]
  Platform: [platform]
  Key insight: [1-line summary of root cause or decision]

What's next?
1. Continue working (recommended)
2. Add to critical patterns (docs/solutions/patterns/critical-patterns.md)
3. Link to related documentation
4. View the documentation
5. Other
```

**Handle responses:**

**Option 2 — Critical Patterns:**
Extract the key lesson as a ❌ WRONG vs ✅ CORRECT pattern and append to `docs/solutions/patterns/critical-patterns.md`. These get read by future sessions.

**Option 3 — Link Related:**
Add cross-references between this doc and related docs.

## Quality Rules

**Good documentation has:**
- Exact error messages (copy-paste)
- Specific file:line references
- Code examples (before/after for bugs, key snippets for features)
- "Why" not just "what"
- Platform-specific notes for porting work
- Failed attempts documented (saves future investigation time)

**Avoid:**
- Vague descriptions ("fixed the thing")
- Missing technical details
- Just code dumps without explanation
- No prevention guidance

## The Compounding Philosophy

```
Research → Plan → Implement → Debug → Document → Deploy
    ↑                                       ↓
    └───── Next session starts HERE ────────┘
```

First time solving "UWP ApplicationFrameHost PID resolution" → 2 hours of research.
Document it → `docs/solutions/porting/uwp-applicationframehost-resolution-20260317.md` (5 min).
Next time → 2 minutes to look up the solution.

**Each unit of engineering work should make subsequent units easier — not harder.**