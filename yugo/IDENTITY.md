# Persona template

**Replace this file with your bot's persona.** Yugo reads it at startup and
prepends it to every conversation as the system prompt.

Recommended structure:

## Who you are
Name, role, personality traits, expertise areas.

## How you communicate
Voice, tone, formatting conventions (Discord-friendly bullets, no tables, etc.).

## What you know
Domain knowledge that should always be in context — fleet layout, project state,
key relationships.

## What you should never do
Constraints, safety rails, format rules to avoid.

## References
Pointers to memory files, docs, external systems the bot should be aware of.

## Declaring tools

**Not here.** Tools are declared in `$YUGO_TOOLS_FILE` (default
`/etc/yugo/tools.yaml`), mounted read-only — never in this file. A tool
declaration is a capability grant; this file is prose that anyone tuning a
bot's voice edits, and it is prepended verbatim to every LLM call. Keeping
the two apart means a persona edit cannot change what a bot may touch, and
the read-only mount makes the grant tamper-proof at the kernel rather than at
a path check. Grammar in SPEC §9.1; the reasoning is in §9.

v0.4a parsed a `## Tools` bullet list out of this file. That grammar is
superseded and removed in v0.4b — do not write one.

`loop_probe` is the v0.4a scaffold: it echoes its argument back and touches
nothing. Real tools (`read_file`, `write_file`, `http_get`) arrive in v0.4b
and v0.4c with the SPEC §9 sandbox guards attached.
