# Deploying the tap

The tap mirrors bus traffic into Discord. It is an observer: it subscribes and posts, and
it is not in the data path of any bot. A tap outage costs visibility, not delivery.

## What the layout is, and why it is split

```
/home/luna/yugo/tools/fleet-bus    the checkout. Code only. `git pull` deploys.
/home/luna/fleet-bus/tap.env       the tap environment. Secrets. NOT in the repo.
/home/luna/fleet-bus/nats.conf     the broker config, bind-mounted into the nats
                                   container. Plaintext credentials for every bot.
                                   NOT in the repo, and neither are its backups.
/home/luna/fleet-bus/js-data       JetStream data. NOT in the repo.
```

**WARNING: keep `/home/luna/fleet-bus` out of version control.** Putting that directory
under git publishes every fleet credential. A `.gitignore` is not sufficient protection
for a directory whose purpose is to hold secrets; the separation above is.

## Deploying a code change

**WARNING: \`compose up -d\` alone does not pick up new code.** Compose recreates a
container when the SERVICE DEFINITION changes. The source is a bind mount, so pulling new
code does not change the definition, compose treats the service as up to date, and the
running Bun process keeps the code it loaded at start.

**This is the trap worth stating plainly: the checkout is new and the process is old.**
Checking \`git rev-parse HEAD\` then reports the new revision, so the verification agrees
with itself and both halves are wrong. (Ohm, PR 69.)

Deploying therefore RECREATES explicitly:

```
PULLED_AT=\$(date -u +%FT%TZ)
sudo -u luna git -C /home/luna/yugo pull
sudo -u luna docker compose -f /home/luna/yugo/tools/fleet-bus/compose.yml \\
  up -d --force-recreate
```

Keep \`\$PULLED_AT\`. Validation check 3 needs it.

Run these as the owning user. \`env_file\` is read by the invoking user, not by the
daemon, so another user fails with a permission error on \`tap.env\`.

## The FIRST compose deployment needs the hand-made container removed

The container running today was created with \`docker run\` and carries no compose
labels. **Compose does not adopt it. It fails on the name**, verified in a throwaway:

```
Container fleet-bus-tap  Error response from daemon: Conflict. The container name
"/fleet-bus-tap" is already in use by container "<id>". You have to remove (or rename)
that container to be able to reuse that name.
```

So the first deployment through this file removes or renames the existing container first,
which the rollback section below does anyway. Every later deployment is an ordinary
\`compose up -d\`.

## Validating a deployment

Four checks, in order. Each one rejects a failure the one before it cannot see.

1. **It starts and stays up.**

   ```
   sudo -u luna docker inspect fleet-bus-tap --format "{{.State.Status}} {{.RestartCount}}"
   ```

2. **It reached the broker and subscribed.**

   ```
   sudo -u luna docker logs fleet-bus-tap | head -3
   ```

   Expect `connected to` and `subscribed to fleet.>`.

3. **The PROCESS restarted after the pull.** Not "the checkout is at the right
   revision" — a bind mount makes that true even when the process is old.

   ```
   sudo -u luna docker inspect fleet-bus-tap --format "{{.State.StartedAt}}"
   sudo -u luna git -C /home/luna/yugo rev-parse --short HEAD
   ```

   **\`StartedAt\` must be later than \`\$PULLED_AT\`.** Bun reads the source once, at
   process start, so the start time is what says which code is loaded. Hashing the mounted
   file proves nothing: the mount is live, so the file always matches the checkout.

4. **It MIRRORS. This is the only check that proves the tap does its job.**

   Send any bus message and confirm it appears in #fleet-bus.

   **A reply from the recipient does not prove this.** It proves bus delivery, which
   works whether or not the tap is running. **An absence of errors in the log does not
   prove it either:** the tap logs only on failure, so a quiet log is consistent with a
   tap that posts nothing. Read the channel.

## Rolling back

**WARNING: keeping the old container does NOT roll back the code.** The source is a
mutable bind mount shared with the checkout, so starting the old container runs whatever
is in the checkout at that moment — which, after a deployment, is the new code. **A
retained container preserves its configuration, not its revision.** (Ohm, PR 69.)

So a rollback reverts the SOURCE, and the container is incidental.

Before deploying, record the revision and keep the old container:

```
OLD_REF=\$(sudo -u luna git -C /home/luna/yugo rev-parse HEAD)
sudo -u luna docker rename fleet-bus-tap fleet-bus-tap-\${OLD_REF:0:7}
```

To roll back, move the source back first, then recreate:

```
sudo -u luna git -C /home/luna/yugo checkout "\$OLD_REF"
# Only if dependencies changed between the two revisions:
sudo -u luna docker run --rm -v /home/luna/yugo/tools/fleet-bus:/app -w /app \\
  oven/bun:latest bun install --frozen-lockfile
sudo -u luna docker rm -f fleet-bus-tap
sudo -u luna docker compose -f /home/luna/yugo/tools/fleet-bus/compose.yml \\
  up -d --force-recreate
```

Then run the four validation checks again. **A rollback is a deployment and earns the
same verification**, including check 4.

**Check \`bun.lock\` between the two revisions before skipping the install step.**
\`node_modules\` is gitignored, so reverting the source does not revert dependencies, and
a lockfile change in either direction leaves the tree wrong for the revision now checked
out.

## The handover gap

A stop-then-start leaves a window with no subscriber. The 2026-10-03 deployment measured
**281 ms**. Bus traffic inside that window is never mirrored, and nothing replays it,
because the tap subscribes to a core NATS subject rather than consuming a stream.

That is acceptable for an observer and it is not acceptable silently. Deploy when traffic
is quiet, and treat a gap during an incident as lost visibility rather than as a clean
record.

## Why the mount is read-only, and what that does and does not guarantee

Verified with disposable controls, not asserted:

```
rw mount, uid 0 in container, write    -> SUCCEEDS, host file mutated
ro mount, uid 0 in container, write    -> "Read-only file system", host file unchanged
ro mount, uid 0, mount -o remount,rw   -> "permission denied", host file unchanged
```

The positive control matters as much as the negative one: without it, a refused write
could mean the test cannot write at all rather than that the mount protects anything.

**The limit:** the remount is refused because the container lacks \`CAP_SYS_ADMIN\`.
Running the tap with \`--privileged\` or \`--cap-add SYS_ADMIN\` removes this
protection. Nothing in this file grants either, and nothing should.

## Dependencies

`node_modules` is gitignored and absent from a fresh checkout, so the tap cannot import
`nats` until it exists. `package.json` and `bun.lock` are committed, so:

```
sudo -u luna docker run --rm -v /home/luna/yugo/tools/fleet-bus:/app -w /app \
  oven/bun:latest bun install --frozen-lockfile
```

## Enabling the deaf-bus detector

**Not enabled by this file.** Add `FLEET_BUS_WATCH_BOTS` to `tap.env` as a
comma-separated list of bot names, then recreate the container. Leaving it unset means no
watching and no new traffic.

The list cannot be derived from the fleet manifest: that file is not mounted into this
container, and its `bot_names` includes entities that are not on the bus. Keeping the
list in step is manual.

Related durations, all optional and all validated at startup:
`FLEET_BUS_SILENCE_MS`, `FLEET_BUS_GRACE_MS`, `FLEET_BUS_REMINDER_MS`,
`FLEET_BUS_TICK_MS`.
