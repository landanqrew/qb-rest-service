---
description: Analyze the project codebase and generate steering docs (.specs/steering/)
allowed-tools: Read, Write, Glob, Grep, Bash(find:*), Bash(cat:*), Bash(head:*)
---

Read `.speq/RULES.md` (the Steering Docs section) and the templates in `.speq/templates/steering/` before proceeding.

Analyze this project's codebase and generate three steering documents in `.specs/steering/`.

Examine:
- `package.json` / `Cargo.toml` / `go.mod` / `requirements.txt` / `build.gradle` (dependencies)
- Configuration files (tsconfig, eslint, prettier, vite, webpack, etc.)
- Directory structure (run `find . -type f -maxdepth 3 | head -100`)
- README and existing documentation
- 2-3 sample source files to understand coding patterns
- Database schemas or migrations if present
- CI/CD configuration if present

Generate these files:

**`.specs/steering/product.md`** — What the product does, target users, core features, business objectives. Infer from README, routes, UI structure. Include a `speq:features` block listing all core features as slugs:

```markdown
## Features

<!-- speq:features — list features that should have specs. slugs map to .specs/specs/<slug>/ -->
- `feature-slug` — Short description
- `another-feature` — Another description
<!-- speq:features:end -->
```

Each slug should be kebab-case and map to a likely spec folder name. Include every distinct feature the product has or needs.

**`.specs/steering/tech.md`** — Languages, frameworks, versions, dev tools, hard prohibitions, preferred patterns. Be specific — include version numbers, reference actual config.

**`.specs/steering/structure.md`** — Directory layout with purpose annotations, naming conventions (inferred from existing code), import patterns, architectural patterns, component patterns.

Rules:
- Be specific to THIS project, not generic
- Include version numbers where available
- For prohibitions, only list things justified by the codebase (e.g., no class components found → prohibit them)
- Mark uncertain items with `<!-- TODO: confirm -->` rather than guessing
- **Save all three files to disk immediately**, then tell the user: "I've saved the steering docs to `.specs/steering/` — review them in your IDE or here, then let me know your feedback or say **LGTM** to approve."
