---
description: Create a feature spec seeded from a Linear, Jira, or GitHub issue
argument-hint: [issue-id e.g. ENG-142]
allowed-tools: Read, Write, Glob, Grep
---

Read the full SDD rules from `.speq/RULES.md` before proceeding.

Read all steering docs from `.specs/steering/` (if they exist) to understand project context.

You are starting a **spec from an existing issue tracker issue**:

**$ARGUMENTS**

## Step 1: Fetch the Issue

Detect which issue tracker MCP tools are available and use the appropriate one:

- **Linear MCP**: Use tools like `get_issue`, `list_comments`, etc. to fetch the issue by ID or identifier.
- **Jira MCP**: Use available Jira MCP tools to fetch the issue.
- **GitHub Issues MCP**: Use available GitHub MCP tools to fetch the issue.

If no issue tracker MCP tools are available, tell the user: "No issue tracker MCP detected. Paste the issue title, description, and any acceptance criteria here and I'll use that as the seed."

Extract from the issue:
- Title
- Description / body
- Acceptance criteria (if any)
- Comments (for additional context)
- Labels, priority, linked issues (if available)

## Step 2: Generate the Spec (Same as /spec)

1. **Create the spec directory**: `.specs/specs/{feature-name}/` using kebab-case derived from the issue title.

2. **Generate and save `requirements.md` to disk immediately** using the issue content as the seed:
   - Add a metadata block at the top of the file linking back to the source issue:
     ```
     <!-- source: {tracker}:{issue-id} -->
     ```
     For example: `<!-- source: linear:ENG-142 -->` or `<!-- source: jira:PROJ-142 -->`
   - Transform PM-language descriptions into structured EARS notation requirements
   - Don't just copy-paste the issue — restructure and enrich it with technical edge cases
   - Use EARS notation for all acceptance criteria: `WHEN [trigger] THE SYSTEM SHALL [behavior]`
   - Include: User Stories with numbered acceptance criteria, Non-Functional Requirements, Out of Scope, Open Questions
   - Cover error cases and edge cases the original issue may have missed
   - **Write the file immediately.**

3. Tell the user the file has been saved and ask: "I've saved `requirements.md` — review it in your IDE or here, then let me know your feedback or say **LGTM** to proceed to Design."

4. **Wait for approval** — do NOT proceed to design until the user explicitly approves.

5. After approval, **re-read `requirements.md` from disk** (the user may have edited it directly), then **offer to sync enriched requirements back to the issue**:
   - Ask: "Would you like me to update the issue with these refined requirements? This can help the PM and team see the fully defined acceptance criteria."
   - If yes, translate the EARS requirements back into PM-readable language and update the issue description or add a comment via MCP.
   - If no MCP is available, skip this step silently.

6. **Generate and save `design.md`** to disk immediately, following the design phase rules in `.speq/RULES.md`.

7. After design approval, **re-read `design.md` from disk**, then **generate and save `tasks.md`** to disk immediately, following the tasks phase rules in `.speq/RULES.md`.

Each phase: write the file to disk → ask for review → wait for approval → re-read from disk before advancing.
