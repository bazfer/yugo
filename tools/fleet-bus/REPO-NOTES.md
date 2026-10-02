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

The imported `.gitignore` already excludes `.env*`. Verified before import: no `.env`, no
`*.pem`, no `*.key` in the source directory, and no token-, webhook- or key-shaped literals
anywhere in these files.

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
3. **No liveness signal of its own.** No `HEALTHCHECK`, nothing logged or posted on start.
   Nobody watches the watcher.
4. **No last-seen map and no silence timer**, so a bot that goes quiet on the bus produces
   no signal — which is the gap the deaf-bus detector is meant to close.

## Deployment

Unchanged by this commit. The container still runs from `/home/luna/fleet-bus`.
