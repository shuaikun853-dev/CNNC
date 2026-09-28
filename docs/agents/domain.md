# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the CNNC codebase.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root, or
- **`CONTEXT-MAP.md`** at the repo root if it exists: it points at one `CONTEXT.md` per context. Read each one relevant to the topic.
- **`docs/adr/`**: read ADRs that touch the area you're about to work in. In a future multi-context layout, also check context-scoped ADR directories.

If any of these files don't exist, **proceed silently**. Don't flag their absence or create placeholder domain files merely for setup. The `/domain-modeling` flow should create or sharpen them lazily when terminology or architectural decisions are actually resolved.

## File structure

CNNC is configured as a **single-context** repository:

```text
/
├── CONTEXT.md
├── docs/adr/
└── app/, scripts/, tests/, ...
```

If CNNC later becomes a genuine multi-package/multi-context repository, introduce a root `CONTEXT-MAP.md` that points to context-specific `CONTEXT.md` files and update this configuration.

## Use the glossary's vocabulary

When output names a domain concept (in an issue title, refactor proposal, hypothesis, test name, report, or runbook), use the term as defined in `CONTEXT.md` when present. Do not drift to synonyms the glossary explicitly avoids.

If a required concept is not in the glossary, either reconsider whether the codebase already has an established term or note a genuine terminology gap for `/domain-modeling`.

## Flag ADR conflicts

If a proposed change contradicts an existing ADR, surface it explicitly rather than silently overriding it.

Example:

> Contradicts ADR-0007, but worth reopening because the production constraint has changed.
