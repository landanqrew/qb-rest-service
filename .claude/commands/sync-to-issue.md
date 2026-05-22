---
description: Push spec progress or updated requirements back to the linked issue tracker issue
argument-hint: [spec-name]
allowed-tools: Read, Write, Glob, Grep
---

Read the full SDD rules from `.speq/RULES.md` before proceeding.

The user wants to sync spec state back to its linked issue:

**$ARGUMENTS**

## Step 1: Load the Spec

Identify the spec name from `$ARGUMENTS`. If not provided, list available specs in `.specs/specs/` and ask the user to choose.

Read the spec's `requirements.md` (or `bugfix.md`), `design.md` (if it exists), and `tasks.md` (if it exists).

## Step 2: Find the Source Issue

Look for the source metadata comment at the top of `requirements.md` (or `bugfix.md`):
```
<!-- source: {tracker}:{issue-id} -->
```

If no source metadata is found, ask the user: "This spec doesn't have a linked issue. Provide an issue ID to link it, or say 'skip' to cancel."

If an issue ID is provided, add the `<!-- source: ... -->` metadata to the requirements file.

## Step 3: Check MCP Availability

Detect which issue tracker MCP tools are available (Linear, Jira, or GitHub Issues).

If no MCP tools are available, tell the user: "No issue tracker MCP detected. Here's a summary you can manually paste into the issue:" and then output a PM-readable summary of the spec state.

## Step 4: Ask What to Sync

Ask the user what they want to sync back:

1. **Progress comment** — Post a comment on the issue summarizing current task completion (e.g., "Spec created: `.specs/specs/user-reviews/` — 14 tasks, 3 complete, covers requirements 1.1–4.3")
2. **Updated description** — Update the issue description with refined requirements translated to PM-readable language (not raw EARS notation)
3. **Status change** — Update the issue status (e.g., "In Progress", "In Review", "Done")
4. **All of the above**

## Step 5: Execute the Sync

Based on the user's choice:

- **Progress comment**: Calculate task completion from `tasks.md` (count `- [x]` vs `- [ ]`). Summarize which requirements are covered. Post as a comment via MCP.
- **Updated description**: Translate EARS requirements back to natural PM-readable language. Preserve any original content that wasn't part of the spec. Update via MCP.
- **Status change**: Ask which status to set, then update via MCP.

When translating requirements back to PM language:
- Convert `WHEN [trigger] THE SYSTEM SHALL [behavior]` to natural language acceptance criteria
- Group by user story
- Include any non-functional requirements and out-of-scope items
- Keep it concise and scannable for non-technical stakeholders

Confirm what was synced after completion.
