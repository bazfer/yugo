# FB-1 — enable JetStream on the live fleet bus

Verified on a throwaway `nats:2.11.8` container, not reasoned about.

## Findings that change the plan

1. **No storage volume.** The `nats` container mounts only two config files.
   JetStream with no volume gives durability that ends at the next container
   restart — worse than today's honest in-memory behaviour, because it looks
   durable. `-v /home/luna/fleet-bus/js-data:/data` is required, not optional.
2. **No bot can reach `$JS.API.>`.** Every user's permissions stop at `fleet.*`,
   `pr.>`, `incident.>` and their `_INBOX_`. Confirmed by running it: a user
   without the grant does not fail cleanly, it **times out after 5s** and reads
   as a hang.
3. **The roadmap names subjects that do not exist.** It says `fleet.*.inbox`;
   live it is `fleet.*.result` plus `_INBOX_<bot>.>`. FB-1 as written targets the
   post-FB-2 world.
4. **The clients use a custom inbox prefix** — `_INBOX_<bot>`, set in both
   `bus.py:228` and `fleet-bus.ts:670`. Any provisioning or ops script must pass
   it or its own JetStream calls are refused. Cost me the first run.
5. **`nats.conf` is not version controlled.** `/home/luna/fleet-bus` has a
   `.gitignore` and no `.git`. The live broker config for eight bots exists in
   exactly one place, owned by another user.

## Verified behaviour

- **Durability holds.** 5 messages published to `fleet.vec.request` with no
  subscriber, `docker restart`, all 5 delivered to the durable consumer after.
- **Unmigrated bots keep working.** A core-NATS subscriber with no `$JS.API`
  grant still received a message published by a migrated bot.
- **Core publishes land in the stream**, so a migrated consumer sees traffic from
  an unmigrated publisher. Rollout order is therefore free — no lockstep.
- **The proposed config parses:** `nats-server -t` reports valid.

## Apply

```sh
cp /home/luna/fleet-bus/nats.conf /home/luna/fleet-bus/nats.conf.pre-fb1
cp nats.conf.proposed /home/luna/fleet-bus/nats.conf
mkdir -p /home/luna/fleet-bus/js-data
# recreate the container WITH the data volume — a restart alone will not add it
docker rm -f nats && docker run -d --name nats --restart unless-stopped -p 4222:4222 \
  -v /home/luna/.claude/fleet-bus-tokens.conf:/etc/tokens.conf:ro \
  -v /home/luna/fleet-bus/nats.conf:/etc/nats.conf:ro \
  -v /home/luna/fleet-bus/js-data:/data \
  nats:2.11.8 -c /etc/nats.conf
python3 provision-streams.py
```

Pin `2.11.8` rather than `latest`: the running container is `nats:latest`, so
today's restart already risks a silent version change independent of this work.

## Rollback

```sh
cp /home/luna/fleet-bus/nats.conf.pre-fb1 /home/luna/fleet-bus/nats.conf
docker rm -f nats && docker run -d --name nats --restart unless-stopped -p 4222:4222 \
  -v /home/luna/.claude/fleet-bus-tokens.conf:/etc/tokens.conf:ro \
  -v /home/luna/fleet-bus/nats.conf:/etc/nats.conf:ro nats:latest -c /etc/nats.conf
```

Streams are additive; leaving them provisioned while JetStream is off is inert.

## Open

`console` is publish-denied, so it cannot use the JetStream API at all — the
read-only observer cannot inspect streams. Fine for FB-1, needs a decision
before FB-4's gate suite wants an observer.

---

## APPLIED 2026-09-02 17:51 CDT. What the runbook above got wrong.

The plan was written against a throwaway broker. Four things only showed up
against the real one, and three of them would have caused damage:

1. **The pinned version was wrong, and pinning it would have been a DOWNGRADE.**
   The runbook said `nats:2.11.8`. The live server was reporting **2.14.5** —
   `nats:latest` had moved. Pinned to the exact running digest instead,
   `nats@sha256:026a66a4497c6d7d3eed741781770099c48c755bf3a55b6950d76dd21059
   6eb3`, which is a true no-change pin. **Read the version off the live
   artifact, never off the plan.**
2. **The container is on TWO networks** — `bridge` and `fleet-bus-net`.
   `docker run` attaches only one, so the second needs
   `docker network connect bridge nats` afterwards. Missing it would have cut
   every bot that reaches the broker over the other network.
3. **Port bindings are IP-scoped**: `127.0.0.1:4222`, `172.17.0.1:4222` and
   `127.0.0.1:8222`. The runbook's `-p 4222:4222` binds `0.0.0.0` — that is
   **the fleet bus published to every interface on the host**. A security
   regression introduced by a convenience shorthand.
4. `fleet-bus-tokens.conf` is `NAME = "value"`, not shell `export`. A sloppy
   extraction yields an empty password and the client retries an
   `Authorization Violation` forever rather than failing once.

## Verified after apply

- All **10 connections** returned, same 9 bots, across two separate restarts.
- 3 messages published to a throwaway subject with nothing listening survived a
  `docker restart` and were delivered in order.
- A real `status_ping` to ohm landed in `FLEET_REQUEST` — live traffic is being
  captured durably.
- Verification subject purged by subject, never by wildcard; streams left at 0.

## Rollback (corrected)

```sh
sudo cp /home/luna/fleet-bus/nats.conf.pre-fb1 /home/luna/fleet-bus/nats.conf
docker rm -f nats
docker run -d --name nats --restart unless-stopped --network fleet-bus-net \
  -p 127.0.0.1:4222:4222 -p 172.17.0.1:4222:4222 -p 127.0.0.1:8222:8222 \
  -v /home/luna/.claude/fleet-bus-tokens.conf:/etc/tokens.conf:ro \
  -v /home/luna/fleet-bus/nats.conf:/etc/nats.conf:ro \
  nats@sha256:026a66a4497c6d7d3eed741781770099c48c755bf3a55b6950d76dd210596eb3 -c /etc/nats.conf
docker network connect bridge nats
```

`js-data/` can stay; it is inert with JetStream off.

## Still open

`nats.conf` is not in version control. The broker config for eight bots lives
in one uncontrolled directory, and this change now includes a security-relevant
permission grant (`$JS.API.>` per user) with no history behind it.
