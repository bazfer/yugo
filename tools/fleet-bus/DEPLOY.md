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

## The FIRST deployment through this file, step by step

The container running today was created with `docker run` and carries no compose labels.
Compose does not adopt it; it fails on the name.

**WARNING: record `OLD_REF` before you start. That ref IS the fallback.** Keeping the
old container is not a fallback, for two separate reasons, both verified:

- **A retained container preserves its configuration, not its revision.** The source is a
  shared bind mount, so starting it again runs whatever is in the checkout now. (Ohm.)
- **Renaming it does not even retain it.** Compose identifies containers by project and
  service labels rather than by name, so `--force-recreate` removes the renamed container
  too. Verified: rename to `<name>-keep`, recreate, and `<name>-keep` is gone. (Kat
  raised the `rm`-versus-rename hazard; testing the rename showed neither one helps.)

```
# Check 3 compares against this. Set it HERE: an empty $PULLED_AT makes check 3 pass
# trivially, which is worse than no check, because it reads as one. (Kat, PR 69.)
PULLED_AT=$(date -u +%FT%TZ)
OLD_REF=$(sudo -u luna git -C /home/luna/yugo rev-parse HEAD)
echo "fallback ref: $OLD_REF"   # write this down. Nothing else recovers it.
sudo -u luna docker rm -f fleet-bus-tap
sudo -u luna docker compose -f /home/luna/yugo/tools/fleet-bus/compose.yml up -d
```

Then run the four validation checks. If they fail, roll the SOURCE back to `$OLD_REF`.

Skipping the `docker rm -f` produces:

```
Container fleet-bus-tap  Error response from daemon: Conflict. The container name
"/fleet-bus-tap" is already in use by container "<id>". You have to remove (or rename)
that container to be able to reuse that name.
```

Every later deployment uses the section below.

## Deploying a code change

```
./tools/fleet-bus/deploy.sh                     # deploys, runs checks 1-3, exits 2
./tools/fleet-bus/deploy.sh --mirror-confirmed  # after reading #fleet-bus; deploys NOTHING
```

**Run it as the owning user or as yourself; both work.** It escalates only when it has to.

**The second command deploys nothing.** It closes out the deployment the first one made,
and refuses if the container has restarted since, because then you verified a different
process. Confirming a check used to require re-running the whole deploy, which restarted
the tap a second time: deploying in order to verify a deployment.

**Expect a burst of status posts in #fleet-bus after any tap restart.** The status dedupe
is in memory, so a restart empties it and the first heartbeat from each watched bot reads
as new. One post per bot, once. It is not the detector firing.

**WARNING: do not substitute `git pull && docker compose up -d`.** Compose recreates a
container when the SERVICE DEFINITION changes. The source is a bind mount, so new code
changes no definition, compose reports the service up to date, and the running process
keeps the code it loaded at start. The checkout is new and the process is old, and a
revision check then agrees with itself while both halves are wrong.

What the script does:

- records the time before moving the source, which check 3 needs
- pulls, then compares `bun.lock` across the pull and installs only if it moved
  (`node_modules` is gitignored, so a lockfile change does not arrive with the source)
- removes and recreates the container, because `--force-recreate` alone is not enough
  once you are doing this by hand
- runs the four checks below

It does not `docker pull` the image. The tag floats, so pulling changes Bun underneath the
tap, which is a larger change than the one being deployed. Do that deliberately and
separately.

**There is no container to preserve.** Renaming it does not retain it, because compose
identifies containers by project and service labels, so recreating removes the renamed one
as well. And a retained container would preserve its configuration rather than its
revision, since the source is a shared bind mount. **The ref the script prints is the
fallback.** Write it down.

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
   ```

   **`StartedAt` must be later than `$PULLED_AT`.** Two timestamps, nothing else.

   **This check deliberately does NOT print the revision.** An earlier version showed
   `git rev-parse HEAD` beside the start time, which invites comparing a SHA to a
   timestamp: an impossible comparison that still reads as a check. The revision is
   precisely the wrong signal here, because it is new whether or not the process
   restarted, which is the trap this check exists to catch. (Kat, PR 69.) Bun reads the source once, at
   process start, so the start time is what says which code is loaded. Hashing the mounted
   file proves nothing: the mount is live, so the file always matches the checkout.

4. **It MIRRORS. This is the only check that proves the tap does its job.**

   Send any bus message and confirm it appears in #fleet-bus.

   **A reply from the recipient does not prove this.** It proves bus delivery, which
   works whether or not the tap is running. **An absence of errors in the log does not
   prove it either:** the tap logs only on failure, so a quiet log is consistent with a
   tap that posts nothing. Read the channel.

## A failed deployment, and the window that remains

**A failed deploy leaves the old container running.** The script does not remove a
compose-managed container before recreating it, so a failure during the deploy ends with
the previous process still serving.

**The window is narrowed, not closed.** `--force-recreate` is stop, remove, create,
start. A failure between remove and create still leaves nothing running. What changed is
that the gap is short and inside compose's control rather than spanning two commands.

**So a deploy can still take the tap down**, and nothing in the tap can report that,
because the tap is what reports. Watching the tap from outside its own process is a
separate piece of work and it is not here yet.

## Rolling back

**WARNING: keeping the old container does NOT roll back the code.** The source is a
mutable bind mount shared with the checkout, so starting the old container runs whatever
is in the checkout at that moment — which, after a deployment, is the new code. **A
retained container preserves its configuration, not its revision.** (Ohm, PR 69.)

So a rollback reverts the SOURCE, and the container is incidental.

Before deploying, record the revision. **That is the whole of the preparation** — there
is no container to keep, per the two reasons above:

```
OLD_REF=$(sudo -u luna git -C /home/luna/yugo rev-parse HEAD)
```

To roll back:

```
./tools/fleet-bus/deploy.sh --rollback "$OLD_REF"
```

**Use the script rather than running the steps by hand.** Every defect found in this file
during review was in prose that restated what the script does, and the last one was in
these very commands: they compared `bun.lock` between `$OLD_REF` and `HEAD` *after*
checking out `$OLD_REF`, so the diff compared a commit to itself, always succeeded, and
the install never ran on a rollback. The script records the revision before and after
separately and is correct. (Kat, PR 69.)

What it does, so you can judge whether it is doing the right thing:

- reverts the source to the ref you name
- compares `bun.lock` across the move and installs only if it changed, **in either
  direction**, because a rollback can need OLDER packages than the ones installed
- recreates the container, which is required rather than optional
- runs the four checks and refuses to report success until you confirm the mirror

**A rollback is a deployment and earns the same verification**, check 4 included.

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

**The limit:** the remount is refused because the container lacks `CAP_SYS_ADMIN`.
Running the tap with `--privileged` or `--cap-add SYS_ADMIN` removes this
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
