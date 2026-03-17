---
name: compound
description: Document a solved problem as a self-contained solution doc in docs/solutions/. Use after solving a non-trivial bug or debugging session. Triggers on "compound this", "document this solution", "write this up", "that was hard, let's document it", "save this solution", "record this fix".
---

# Compound — Document a Solved Problem

Capture a solved problem as a self-contained solution document in `docs/solutions/`. Each doc must be understandable by a developer who has never seen the codebase and only has access to `docs/solutions/`.

## Handling Args

- `/compound` — document the most recent fix from conversation context
- `/compound "brief description"` — seed subagents with additional context about what to document
- If the user provides context after the command, use it to focus the research

## Instructions

### Step 1: Check for Existing Documentation

Before creating a new doc, check if the problem is already documented:

```
Glob: docs/solutions/**/*.md
```

If a matching doc exists, **update it** instead of creating a duplicate. Tell the user: "Found existing doc at `<path>` — I'll update it rather than create a duplicate."

### Step 2: Parallel Research (Subagents)

Launch these subagents **in parallel** using the Agent tool. Each must return **text data only** — no file writes.

**Subagent 1: Context & Solution Extractor**

```
You are researching a solved problem. DO NOT write any files. Return TEXT DATA only.

From the conversation history, extract:

1. **Context**: What is the system/component? How is it deployed/used? (2-3 sentences a newcomer would understand — no jargon without explanation)
2. **Problem**: What happened? Exact error messages in code blocks, environment details (OS version, architecture, CI runner)
3. **Root Causes**: Full chain from trigger to failure for each root cause. Answer "why" at each step.
4. **Investigation Steps**: Numbered chronological list — include dead ends and what didn't work
5. **Working Solution**: For each fix, the conceptual explanation + code with file paths and enough context to apply standalone

Return all sections as markdown text.
```

**Subagent 2: Prevention & Classification**

```
You are analyzing a solved problem for prevention and classification. DO NOT write any files. Return TEXT DATA only.

Return:

1. **Prevention Strategies**: How to avoid this class of problem. Include checklists, warning signs, CI checks.
2. **Category**: One of: build-errors, runtime-errors, test-failures, performance-issues, integration-issues (or suggest a new one if none fit)
3. **Filename slug**: Descriptive, e.g. pyinstaller-frozen-binary-ci-failures
4. **YAML frontmatter** with these fields:
   - title: "Short descriptive title"
   - problem_type: build-and-packaging | runtime | test-failure | performance | integration
   - component: comma-separated affected components
   - symptoms: list of exact error messages
   - root_causes: list of one-sentence root causes
   - tags: list of searchable keywords

Do NOT include `date` — the orchestrator sets that.

Return all as markdown/YAML text.
```

**Subagent 3: Related Docs Finder**

```
You are finding related documentation. DO NOT write any files. Return TEXT DATA only.

Derive 2-3 search terms from the problem description, then search for:
1. Existing docs in docs/solutions/ that relate to this problem
2. Source files that were changed or are referenced by the fix
3. Key git commits: run git log --oneline --grep="<term>" for each search term

Return a ## Related Documentation section with:
- Cross-references to other docs/solutions/ files (if any exist)
- Links to source files (e.g. pyinstaller/screencap.spec, src/screencap/cli.py)
- Key commit SHAs with one-line descriptions
- NEVER link to docs/epics/, docs/plans/, docs/tickets/
```

### Step 3: Assemble & Write

**Wait for all subagents to complete**, then:

1. Collect all text results
2. Set `date` to today's date
3. Assemble into the document template below
4. Create directory: `mkdir -p docs/solutions/<category>/`
5. Write the single file: `docs/solutions/<category>/<slug>.md`

### Step 4: Post-Write Verification

After writing the file, re-read it with the Read tool and verify:

| Check | How to verify |
|---|---|
| Context is newcomer-friendly | No unexplained jargon, no assumed project knowledge |
| Error messages are exact | In code blocks, not paraphrased |
| Root cause chain is complete | Every "why" has an answer |
| Code blocks are standalone | Include file paths, enough context to apply without reading full source |
| Related links resolve | Run Glob to verify referenced files exist |
| No dead-end links | No references to docs/epics/, docs/plans/, docs/tickets/ |

If any check fails, fix the section. Then tell the user: "Solution doc created at `<path>`. Want me to adjust anything?"

## Document Template

```markdown
---
title: "Short descriptive title of the problem and fix"
date: YYYY-MM-DD
problem_type: build-and-packaging | runtime | test-failure | performance | integration
component: comma-separated list of affected components
symptoms:
  - "Exact error message or observable behavior 1"
  - "Exact error message or observable behavior 2"
root_causes:
  - "Technical root cause 1 — one sentence"
  - "Technical root cause 2 — one sentence"
tags:
  - relevant
  - searchable
  - keywords
---

# Title matching the frontmatter

## Context

2-3 sentences explaining what this project/component is and how the
affected system works. A developer with zero project context should
understand the domain after reading this paragraph.

## Problem

What happened. Include the exact error output the user/CI saw.
Use code blocks for error messages. Be specific about the environment.

## Root Cause Analysis

For each root cause, explain the full chain from trigger to failure.
Use ### subheadings if there are multiple root causes.
Include the "why" at each step.

## Investigation Steps

Numbered list of what was tried, in chronological order.
Include what didn't work and why.

## Working Solution

### Fix N: Descriptive name

Explain the fix conceptually, then show the code.
Include file paths. Show enough context to apply standalone.

## Prevention Strategies

How to avoid this class of problem in the future.
Include checklists, warning signs, CI checks.

## Related Documentation

- Cross-reference other docs/solutions/ files
- Link to source files changed by the fix
- Key commit SHAs with one-line descriptions
- NEVER link to docs/epics/, docs/plans/, docs/tickets/
```

## Key Principles

- **Self-contained**: Every doc readable by someone with zero project context and only `docs/solutions/` access. Always include Context section. Never reference planning docs.
- **Exact**: Copy-paste error messages, include file paths and line numbers, name exact versions (macOS 11.7.5, not "older macOS").
- **Investigation trail**: Dead ends save the next person time. The numbered steps are often the most valuable section.
- **Prevention compounds**: Without prevention strategies, it's a postmortem. With them, you've made the next engineer faster.

## What NOT to Document

Skip if any of these apply:
- Simple typo or obvious one-liner fix
- No general lesson (only affected one conversation)
- Fix is just "update the dependency" with no deeper insight
- Already covered by an existing solution doc (update that one instead)
- The debugging didn't require multiple attempts or non-obvious investigation

## Naming Conventions

Good: `pyinstaller-frozen-binary-ci-failures.md`, `macos-pre14-binary-install-failure.md`
Bad: `fix-bug.md`, `2026-03-17-issue.md`, `pr-95-fixes.md`
