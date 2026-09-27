# Fleet-bus subject topology (FB-2)

**Status:** authoritative declaration of the subject topology. Closes yugo #9, which
blocks FB-3 (#10).
**Verified against the live broker and the live streams on 2026-09-27.** Every
"transitional" statement below was read off the running system in one sitting, not
carried over from an earlier revision.

This document states **two lists, separately**: the transitional topology that is
live today, and the post-FB-3 target. The issue requires them stated apart so the
two are not confused, because the live broker is mid-migration and several grants
exist for subjects nothing currently uses.

---

## 1. Subject vocabulary

| Subject | Direction | Meaning |
|---|---|---|
| `fleet.<bot>.request` | inbound to `<bot>` | A request addressed to `<bot>`. Captured by the `FLEET_REQUEST` stream. |
| `fleet.<bot>.inbox` | inbound to `<bot>` | The coordinator's durable relay of a request. Captured by the `FLEET_INBOX` stream. |
| `fleet.<bot>.result` | inbound to `<bot>` | **Transitional only.** A reply correlated to an earlier request. Removed by FB-3. |
| `fleet.<bot>.status` | outbound from `<bot>` | Heartbeat and liveness. Every bot subscribes `fleet.*.status`. |
| `fleet.broadcast.>` | fan-out | Fleet-wide announcements. |
| `pr.>`, `incident.>` | fan-out | Topic lanes, outside the request/reply contract. |
| `_INBOX_<bot>.>` | NATS internal | Per-bot core-NATS request/reply inbox prefix. Not part of the fleet contract. |

`<bot>` is the NATS username, which is also the bot's subject identity. A bot may
subscribe only its own inbound subjects.

---

## 2. Transitional topology — live as of 2026-09-27

### 2.1 Streams

| Stream | Subjects captured | Messages | Consumers |
|---|---|---|---|
| `FLEET_REQUEST` | `fleet.*.request` | 191 | `yugo-coordinator-request` — durable pull, `AckPolicy.EXPLICIT`, `DeliverPolicy.NEW`. Caught up: pending 0, redelivered 0. |
| `FLEET_INBOX` | `fleet.*.inbox` | 191 | **none** |
| `FLEET_RESULT` | `fleet.*.result` | 1 | **none** |

All three are limits-retention with a 7-day `max_age`. An unacked message still
disappears when its `max_age` expires; ack state does not extend it. The
configured `7d >> 15min` hold relation is a deployment invariant, not a promise
made by any consumer (`yugo/coordinator.py:227-232`).

### 2.2 Per-bot grants

Identical in shape for every bot (`luna`, `deet`, `kat`, `vec`, `ohm`, `myc`,
`helm`, `chis`):

- **subscribe:** `fleet.<self>.request`, `fleet.<self>.inbox`,
  `fleet.<self>.result`, `fleet.*.status`, `fleet.broadcast.>`, `pr.>`,
  `incident.>`, `_INBOX_<self>.>`
- **publish:** `fleet.*.request`, `fleet.*.inbox`, `fleet.*.result`,
  `fleet.<self>.status`, `fleet.broadcast.>`, `pr.>`, `incident.>`,
  `_INBOX_<self>.>`, `$JS.API.INFO`, `$JS.API.STREAM.INFO.*`

### 2.3 Coordinator grants

- **subscribe:** `fleet.>`, `_INBOX_coordinator.>`
- **publish:** `fleet.*.inbox`, `fleet.coordinator.status`, `$JS.API.INFO`, plus
  the JetStream consumer API scoped to `FLEET_REQUEST` only
  (`STREAM.INFO.FLEET_REQUEST`, `CONSUMER.CREATE.FLEET_REQUEST.>`,
  `CONSUMER.DURABLE.CREATE.FLEET_REQUEST.>`, `CONSUMER.INFO.FLEET_REQUEST.*`,
  `CONSUMER.MSG.NEXT.FLEET_REQUEST.*`, `$JS.ACK.FLEET_REQUEST.>`)

A read-only `console` user subscribes `fleet.>`, `pr.>` and `incident.>` with
publish denied on `>`.

### 2.4 What the adapters actually use

Granted is not consumed. Both adapters subscribe **core NATS**, not JetStream:

- TypeScript: `fleet.<self>.request`, `.result` and `.status` at
  `src/fleet-bus.ts:1065-1067`, plus `fleet.broadcast.>` at
  `src/fleet-bus.ts:1069` only when `config.subscribeBroadcast` is set — all
  through `this.nc.subscribe` at `src/fleet-bus.ts:2079`.
- Python: `self._nc.subscribe` at `yugo/fleet_bus.py:1537`.

**No adapter subscribes `.inbox`, in either port, despite every bot being granted
it.** The coordinator relays every request into `FLEET_INBOX` and acks the
request only after that write lands (`yugo/coordinator.py:275-290`), so the
durable copy exists — and nothing reads it. 191 messages are parked in a stream
no bot consumes, aging toward the 7-day expiry.

The consequence, stated plainly: **delivery to a bot is currently fire-and-forget.**
A bot that is down loses its message, because the copy that survived is on a
subject it does not subscribe to. This is the gap FB-3 closes, and it is the
reason #10 item 1 exists.

---

## 3. Post-FB-3 target topology

### 3.1 Streams

| Stream | Fate |
|---|---|
| `FLEET_REQUEST` | Retained. Coordinator-consumed, as today. |
| `FLEET_INBOX` | Retained. **Consumed by each bot through a durable JetStream consumer.** |
| `FLEET_RESULT` | **Removed**, once no adapter subscribes it (#10 item 9). |

### 3.2 Per-bot grants

- **subscribe:** `fleet.<self>.inbox`, `fleet.*.status`, `fleet.broadcast.>`,
  `pr.>`, `incident.>`, `_INBOX_<self>.>`
- **publish:** `fleet.*.request`, `fleet.<self>.status`, `fleet.broadcast.>`,
  `pr.>`, `incident.>`, `_INBOX_<self>.>`, and the JetStream API surface each
  port needs to bind and ack its own inbox consumer

Removed relative to transitional: `fleet.<self>.request` and
`fleet.<self>.result` subscribe, and `fleet.*.result` publish. Revoking `.result`
subscribe alone would leave every bot able to publish to a channel nothing gates,
so both directions go (SPEC §6.2.1 step 3).

### 3.3 Replies

Replies travel on `fleet.<bot>.request` carrying `in_reply_to`, resolved against
the outbound waiter ledger. **The ledger is retained.** It is subject-independent
correlation state, not `.result`-specific; deleting it would remove
`request(wait: true)` from the topology altogether, which is a separate breaking
change (SPEC §6.2.1).

---

## 4. Open decision, not settled by this document

**Should a bot keep `publish` on `fleet.*.inbox`?**

Today every bot can publish directly to every other bot's inbox, which means the
coordinator can be bypassed entirely: a sender can write a message into a peer's
durable inbox without the relay, the dedup arbitration, or any of the ack
semantics the coordinator provides. Restricting `fleet.*.inbox` publish to the
coordinator would make the relay the only path into an inbox.

This is deliberately left open here because it is an authorization decision
entangled with **#22** (the bus does not authenticate `from` — the field is
self-asserted), and because it is the kind of change that should be made with the
`from` authentication rather than ahead of it. FB-3 does not require it.

---

## 5. Migration phases and gates

The reply-lane migration is expand → migrate → contract with two global gates,
defined normatively in **SPEC §6.2.1**. Both gates have passed: #17 (Expand) and
#18 (Migrate) are closed.

**#10 bundles two independent migrations, and this is worth stating because their
hazards differ:**

1. **The reply-lane contract** — delete the `.result` subscription and handler,
   revoke `.result` authz both directions, drop `FLEET_RESULT`. The hazard is
   peer stranding, which is what SPEC §6.2.1's gates exist to prevent.
2. **The request-lane delivery move** — start consuming `.inbox`, stop consuming
   `.request`, revoke `.request` subscribe. The hazard is different: because the
   coordinator relays **every** request to `.inbox` unconditionally, an adapter
   consuming both lanes receives **each ordinary request twice**, not only on
   redelivery. Dedup keys on envelope id rather than subject, so both paths share
   arbitration provided the relayed id is unchanged.

Ohm ruled on 2026-09-27 that item 1 — start consuming `.inbox` — may ship as a
separate additive PR ahead of the removals, which stay gated. That PR must carry
a durable JetStream consumer (not another core-NATS subscription), shared dedup
and correlation across both paths, ack semantics that never ack the inbox copy
merely because the direct path holds a *pending* claim, tests for both arrival
orders plus concurrent delivery, crash/restart and store failure, and an explicit
policy for the retained backlog rather than a blind replay.

---

## 6. How to re-verify this document

```sh
# streams and their consumers
curl -s 'http://127.0.0.1:8222/jsz?streams=1&consumers=1'

# what each adapter actually subscribes
grep -n 'this.nc.subscribe' src/fleet-bus.ts
grep -n '_nc.subscribe'     yugo/fleet_bus.py

# the coordinator's durable consumer
sed -n '245,270p' yugo/coordinator.py
```

Broker grants live in the fleet-bus NATS configuration, which is deployment
infrastructure and not in this repo. Read the `authorization` block there; the
passwords are `$VAR` references resolved from a separate included file and must
never be quoted into a document or a review.
