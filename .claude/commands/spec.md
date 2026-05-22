---
description: Create a new feature spec using the Spec-Driven Development workflow (Requirements → Design → Tasks)
argument-hint: [feature description]
allowed-tools: Read, Write, Glob, Grep
---

Read the full SDD rules from `.speq/RULES.md` before proceeding.

Read all steering docs from `.specs/steering/` (if they exist) to understand project context.

You are starting the **Requirements-First** spec workflow for this feature:

**$ARGUMENTS**

Follow these rules exactly:

1. **Create the spec directory**: `.specs/specs/{feature-name}/` using kebab-case derived from the feature description.

2. **Generate and save `requirements.md`** to disk immediately:
   - Generate a complete draft IMMEDIATELY — do NOT ask a series of clarifying questions first.
   - Use EARS notation for all acceptance criteria: `WHEN [trigger] THE SYSTEM SHALL [behavior]`
   - Include: User Stories with numbered acceptance criteria, Non-Functional Requirements, Out of Scope, Open Questions
   - Cover error cases and edge cases, not just happy paths
   - Focus ONLY on WHAT the system should do, not HOW
   - **Write the file to `.specs/specs/{feature-name}/requirements.md` immediately.**

3. Tell the user the file has been saved and ask: "I've saved `requirements.md` — review it in your IDE or here, then let me know your feedback or say **LGTM** to proceed to Design."

4. **Wait for approval** — do NOT proceed to design until the user explicitly approves.

5. After approval, **re-read `requirements.md` from disk** (the user may have edited it directly), then **generate and save `design.md`** to disk immediately, following the design phase rules in `.speq/RULES.md`.

6. After design approval, **re-read `design.md` from disk**, then **generate and save `tasks.md`** to disk immediately, following the tasks phase rules in `.speq/RULES.md`.

Each phase: write the file to disk → ask for review → wait for approval → re-read from disk before advancing.
