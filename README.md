# yugo

Yoke for the Artifice fleet: the envelope contract both languages must satisfy,
the TypeScript bus client, and the Python bot framework and NATS coordinator
that run on it.

## Why TypeScript is at the root

**This layout is forced, not chosen.** bun cannot install a git dependency from
a subdirectory ([oven-sh/bun#15506](https://github.com/oven-sh/bun/issues/15506),
open as of 2026-09-17; verified against bun 1.4.2). `artifice-discord` consumes
this repository as a git dependency, so the TypeScript `package.json` **must**
sit at the repository root or the plugin cannot install.

Python lives under `yugo/` as a consequence of that and nothing else. Please do
not "tidy" the layout — moving `package.json` into a subdirectory breaks the
Discord plugin the whole fleet is operated through.

## Layout

| Path | What |
| --- | --- |
| `schema/`, `conformance/`, `SPEC.md` | the envelope contract — language-neutral, binding on every implementation |
| `src/`, `test/`, `package.json` | the TypeScript bus client |
| `yugo/` | the Python bot framework and NATS coordinator, with its own `SPEC.md` and tests |

## Implementations of the contract

Three, in two languages. They never call each other; they agree by passing the
same conformance vectors.

| Implementation | Where | Entry point |
| --- | --- | --- |
| TypeScript | this repo, `src/fleet-bus.ts` | `validateEnvelope` |
| Python | this repo, `yugo/fleet_bus.py` | `validate_envelope` |
| Python | `artifice-ia/codex-container`, `bus.py` | `validate_envelope` |

## Status

Migrated from `bazfer/fleet-bus` and `bazfer/yugo` on 2026-09-17. History was
deliberately not carried across; both source repositories remain readable.
