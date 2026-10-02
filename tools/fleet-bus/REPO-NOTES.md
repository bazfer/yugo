# fleet-bus operational tooling — repo notes

These files ran in production for weeks **with no version control, no review and no
history**. This commit is a baseline import: every file is byte-identical to what was on
the host at `/home/luna/fleet-bus` on 2026-10-02. **No logic was changed.**

## Why they are here and not in `bazfer/fleet-bus`

`bazfer/fleet-bus` was **archived on 2026-09-18** and superseded by this repo. Yugo is the
live home of the fleet bus, so its operational tooling belongs beside it.

## What runs in production today

**`tap.ts`** — runs as the `fleet-bus-tap` container (`bun tap.ts`), which bind-mounts
`/home/luna/fleet-bus` to `/app`. It mirrors bus envelopes to Discord and collapses
`.status` messages on a state hash.

**Important: committing this file does not change what runs.** The container binds the host
directory, not a checkout of this repo. Until that is resolved, this import improves
history and auditability only. Resolving it is explicitly out of scope for this PR — see
"Known gaps".

Everything else here is a developer or operator utility: probes, a publisher, a listener, a
status inspector, a demo, and a test suite.

`fb-1/` holds the FB-1 stream provisioning runbook and its two Python scripts, previously
root-owned on the host.

## Secrets

**No credential literals.** Every secret is read from the environment or from a file path
given by the environment:

- `FLEET_BUS_CONSOLE_PASS`, `DISCORD_BOT_TOKEN`, `FLEET_BUS_WEBHOOK_URL` — environment
- `bus-publish.ts` reads the NATS password from the file named by `FLEET_BUS_TOKEN_FILE`,
  never inline

Verified before import: no `.env`, no `*.pem`, no `*.key` in the source directory, and no
token-, webhook- or key-shaped literals anywhere in these files.

**CORRECTION (Ohm, review of 615f79b): the imported `.gitignore` does NOT cover `.env*`.**
It lists `.env`, `.env.development.local`, `.env.test.local`, `.env.production.local` and
`.env.local` — so **`.env.production`, `.env.staging` and `.env.backup` are NOT ignored**,
confirmed with `git check-ignore`. Nothing is leaked today because no such file exists, but
it is a live foot-gun for whoever adds one. Hardening it changes an imported file, so it is
deliberately left to the follow-up PR rather than smuggled into a baseline.

## Known gaps, carried as-is rather than fixed here

Found during the 2026-10-01 architect review of the deaf-bus detection plan. They are
recorded so the next reader does not have to rediscover them, and deliberately **not** fixed
in a baseline import — a commit that mixes baseline and changes leaves a reviewer unable to
see the delta.

1. **`connect()` passes no `maxReconnectAttempts`.** nats@2.29.3 defaults to 10 attempts at
   2s. A NATS outage longer than ~20s closes the client, the `for await` ends, the process
   exits and Docker restarts it — **losing all in-memory state.**
2. **`drain()` uses `fetch` with no timeout** under a `draining` flag. One hung POST stalls
   mirroring **silently and indefinitely**.
3. **No liveness signal on any surface anyone watches.** **CORRECTED (Ohm):** it *does*
   log on startup — `[tap] connected to <url> as console` and `[tap] subscribed to fleet.>`
   — via `process.stderr.write` (lines 27 and 147), which my first pass missed by grepping
   for `console.log`. The real gap is narrower and still real: those lines reach only
   `docker logs`, **nothing is posted to Discord on start**, and there is no `HEALTHCHECK`.
   Nobody watches the watcher.
4. **No last-seen map and no silence timer**, so a bot that goes quiet on the bus produces
   no signal — which is the gap the deaf-bus detector is meant to close.
5. **A numeric envelope `id` crashes the tap** (Ohm, reproduced). `format()` at line 93 does
   `parsed.id.slice(0, 8)`; `id` is typed `id?: string` but that is compile-time only, so an
   envelope carrying `id: 42` raises an uncaught `TypeError` and **terminates the process**.
   Line 92 has the same shape for `in_reply_to`. A single malformed envelope from any
   publisher takes the tap down — which is the same family as defects 1-3: the component
   that is supposed to notice silence can itself be silenced.

## Deployment

Unchanged by this commit. The container still runs from `/home/luna/fleet-bus`.
