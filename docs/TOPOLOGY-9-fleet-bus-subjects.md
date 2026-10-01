# Fleet-bus subject topology (FB-2)

**Status:** authoritative declaration of the subject topology. Closes yugo #9, which
blocks FB-3 (#10).
**Transitional facts verified against the live broker and live streams on
2026-09-27.** Target facts are transcribed from `yugo/SPEC.md`, not derived.

> **This repository has two specs.** `SPEC.md` at the root (31 KB) covers the
> TypeScript contract and the reply-lane migration. **`yugo/SPEC.md` (321 KB) is
> the authoritative system spec** and is where the authz map, the DeliverPolicy
> rules and the de-dup contract live. An earlier revision of this document
> derived a target topology from the root spec alone and got it wrong in three
> places. Read `yugo/SPEC.md` §4.3, §4.7, §14 and §15 before changing anything
> here.

This document states **two lists, separately**: the transitional topology live
today, and the post-FB-3 target. The issue requires them stated apart because the
live broker is mid-migration and several grants exist for subjects nothing
currently uses — and, as §4 records, one grant exists that the target forbids.

---

## 1. Subject vocabulary

| Subject | Direction | Meaning |
|---|---|---|
| `fleet.<bot>.request` | inbound, **coordinator-consumed** | A request addressed to `<bot>`. JetStream stream `FLEET_REQUEST`. Gates the first hop. **Target shape is `fleet.<bot>.request.<sender>`** — see §3.2 and `yugo/SPEC.md` §4.7.1. |
| `fleet.<bot>.inbox` | inbound to `<bot>` | The coordinator's released envelope. JetStream stream `FLEET_INBOX`. Gates the last hop. |
| `fleet.<bot>.result` | inbound to `<bot>` | **Transitional only.** A reply correlated to an earlier request. Removed by FB-3. |
| `fleet.<bot>.status` | outbound from `<bot>` | Heartbeat and liveness. |
| `fleet.broadcast.>` | fan-out | Fleet-wide announcements. |
| `pr.>`, `incident.>` | fan-out | Topic lanes outside the request/reply contract and outside §4.7's authz map. |
| `_INBOX_<bot>.>` | NATS internal | Per-bot core-NATS request/reply inbox prefix. Not part of the fleet contract. |

`<bot>` is the NATS username, which is also the bot's subject identity. A bot may
subscribe only its own inbound subjects.

**Granted is not consumed.** Several statements below distinguish a permission in
the broker config from a subscription an adapter actually opens. The two differ
today on `.inbox`, which is the central finding of §2.4.

---

## 2. Transitional topology — live as of 2026-09-27

### 2.1 Streams

| Stream | Subjects captured | Messages | Consumers |
|---|---|---|---|
| `FLEET_REQUEST` | `fleet.*.request` | 191 | `yugo-coordinator-request` — durable pull, `AckPolicy.EXPLICIT`, `DeliverPolicy.NEW`. Caught up: pending 0, redelivered 0. |
| `FLEET_INBOX` | `fleet.*.inbox` | 191 | **none** |
| `FLEET_RESULT` | `fleet.*.result` | 1 | **none** |

Both request and inbox families are limits-retention, `max_age: 7d`,
`max_msgs_per_subject: 100000`, `discard: old` (`yugo/SPEC.md` FB-1). An unacked
message still disappears when `max_age` expires; ack state does not extend it
(`yugo/coordinator.py:227-232`).

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

A read-only `console` user subscribes `fleet.>`, `pr.>` and `incident.>` with
publish denied on `>`.

### 2.4 What the adapters actually consume

- TypeScript: `fleet.<self>.request`, `.result` and `.status` at
  `src/fleet-bus.ts:1065-1067`, plus `fleet.broadcast.>` at `:1069` only when
  `config.subscribeBroadcast` is set — all through `this.nc.subscribe` at
  `src/fleet-bus.ts:2079`, i.e. **core NATS, not JetStream**.
- Python: `self._nc.subscribe` at `yugo/fleet_bus.py:1537`, same.

**No adapter subscribes `.inbox`, in either port, despite every bot being granted
it.** The coordinator relays every released request into `FLEET_INBOX` and acks
the `.request` copy only after that write lands
(`yugo/coordinator.py:275-295`), so the durable copy exists and nothing reads it:
191 messages parked in a stream no bot consumes, aging toward the 7-day
`max_age`.

Two consequences, and the second is the more serious one:

1. **Delivery to a bot is fire-and-forget.** A bot that is down loses its
   message, because the copy that survived is on a subject it does not subscribe
   to.
2. **The interposition the policy gate will need is not in place.** Per
   `yugo/SPEC.md` §4.3 the coordinator's designed role is not merely a durable
   relay — it applies a policy check and can hold an envelope for human approve /
   reject / redirect before releasing it to `.inbox`. **That is a target, not
   today's behaviour:** §7 describes the deployed v0.6a relay as forward-only
   (audit + forward); v0.7a adds policy evaluation and **v0.7b is where holding
   starts**. An adapter subscribing `fleet.<self>.request` directly would
   therefore receive an envelope regardless of a **future** hold decision, which
   is why §4.7 records it as the **SEV1-1** gap: "a still-active `.request`
   subscription would bypass coordinator holds."

So FB-3 does two things: it stops losing messages while a bot is restarting, and
it is the precondition for the hold ever being enforceable. No policy hold is
being bypassed today, because nothing holds yet.

---

## 3. Post-FB-3 target topology

Transcribed from `yugo/SPEC.md` §4.7 ("NATS authz map, v0.7 target"), not derived.

### 3.1 Streams

| Stream | Fate |
|---|---|
| `FLEET_REQUEST` | Retained. **Coordinator is the sole consumer.** Its subject list and the coordinator consumer's filter both widen to cover `fleet.*.request.*` alongside `fleet.*.request` before any publisher adopts the sender token (§3.2). |
| `FLEET_INBOX` | Retained. Consumed by each bot through its own durable JetStream consumer. |
| `FLEET_RESULT` | **Removed** once no adapter subscribes it (#10 item 9). |

### 3.2 Per-bot grants

- **subscribe:** `fleet.<self>.inbox`, **`fleet.<self>.status` (self only)**,
  `fleet.broadcast.>` — **unchanged by sender-binding. Receivers do not move.**
- **publish:** **`fleet.*.request.<self>`**, **`fleet.broadcast.*.<self>`**,
  `fleet.<self>.status`, plus the JetStream API surface needed to bind and ack its
  own `.inbox` durable

  **The sender is a literal final token, and that is the entire authentication
  mechanism** (`yugo/SPEC.md` §4.7.1, amended 2026-10-01 for yugo #22). Earlier
  revisions of this document transcribed `fleet.*.request` and
  `fleet.broadcast.>`, because §4.7 said so: one grant per lane, identical for
  every bot, carrying no sender. Under that map `envelope.from` is unenforceable by
  any component, which is the gap #22 describes. **This document transcribes; it
  did not derive the old shape and does not derive the new one.**

  Three facts a reader of this section needs, each stated where it belongs rather
  than left to the system spec:

  - **A receiver never sees the enforced token.** The coordinator rewrites the
    subject when it forwards to `fleet.<recipient>.inbox`
    (`yugo/coordinator.py` `inbox_subject`), so broker-enforced identity reaches a
    bot only as the coordinator-written `verified_from` field. There is no
    receiver-side subject derivation in this topology.
  - **Nothing enforces this yet.** The live grant is `fleet.*.request` (§2.2) and
    the coordinator writes no field. §4.7.1 carries the clause-by-clause table of
    what refuses and what does not.
  - **The deployment order is normative and the publish path has no PubAck.**
    `FLEET_REQUEST`'s subject list and the coordinator's consumer filter are both
    `fleet.*.request` today, and NATS `*` matches exactly one token — so a
    four-token publish matches neither, and a core publish that lands in no stream
    is lost with no error at either end. Stream and coordinator first, publishers
    fourth, broad grant revoked last (§4.7.1).
- **denied:** `fleet.<self>.request` **subscribe** — the coordinator is the sole
  consumer, closing SEV1-1
- **denied:** `fleet.*.inbox` **publish** — see §4
- **removed:** `fleet.<self>.result` subscribe and `fleet.*.result` publish.
  Revoking subscribe alone would leave every bot able to publish to a channel
  nothing gates, so both directions go (`SPEC.md` §6.2.1 step 3)

`pr.>`, `incident.>` and `_INBOX_<self>.>` sit outside §4.7's map and are not
changed by FB-3.

### 3.3 Coordinator grants

- **subscribe:** `fleet.*.status` **of all bots**, `fleet.broadcast.>`, and its
  pull consumer on `FLEET_REQUEST`
- **publish:** `fleet.*.inbox` — **the only user with this grant** — and
  `fleet.coordinator.status`. **No broadcast publish:** §4.7 grants the
  coordinator `fleet.broadcast.>` subscribe only.

The tap / console user keeps subscribe on request, inbox, status and broadcast,
with **zero publish**.

### 3.4 Replies

Replies travel on `fleet.<bot>.request` carrying `in_reply_to`, resolved against
the outbound waiter ledger; the coordinator gates them by matching `root_id`
(`yugo/SPEC.md` §4.7, §7A.1 `strict` mode return-gate). **The ledger is
retained** — it is subject-independent correlation state, not `.result`-specific,
and deleting it would remove `request(wait: true)` from the topology altogether,
a separate breaking change (`SPEC.md` §6.2.1).

### 3.5 Inbox consumer DeliverPolicy

Specified in `yugo/SPEC.md` §14 (SEV1-3 v7 fix) — **not a policy this migration
gets to choose:**

- **Initial creation**, durable does not yet exist server-side →
  `DeliverPolicy=New`. Skips the historical backlog, preventing replay of
  historical `prod_op` / `spend` envelopes on the FB-3 flip.
- **Re-creation**, the durable existed and was deleted → `DeliverPolicy=All`.
  **Do not re-skip**; persistent envelope-id de-dup absorbs the replay.
- The signal is an **`_initialized_at` marker row**, exempt from the retention
  cull — its presence, **not** the row count, distinguishes the two cases, because
  a quiet bot can legitimately have zero rows after a cull. The marker must be
  written in the **same SQLite transaction** that creates the schema. Three
  startup cases:
  - store file absent → **INITIAL**: create schema + marker in one transaction,
    `DeliverPolicy=New`
  - file present, marker present → **RE-CREATION**: `DeliverPolicy=All`
  - file present, **marker absent → FAIL-LOUD ABORT**: log and refuse to start.
    Marker-absent means ops truncation, a cleanup-script bug or a hand-crafted
    store — every case where a silent `New` would drop approved holds.
- Adapter de-dup retention **MUST be ≥ the `.inbox` stream `max_age`** of 7d;
  the default is 8d, one day of slack. Guarded in the TypeScript constructor at
  `src/fleet-bus.ts:156`.
- The store path is configured explicitly, `$YUGO_DEDUP_STORE_PATH`.

**Specified but NOT implemented.** The following are §14/§15 targets, not features
that exist today, and this document does not claim otherwise:

- The operator recovery command `yugo dedup-recover --store <path>
  --deliver-policy <all|new|since:<ts>> --acknowledge-replay-risk`. It requires an
  explicit policy with no default and re-stamps the marker. `all` risks mass
  re-execution of `prod_op`/`spend` envelopes older than the surviving rows;
  `new` risks silently dropping approved holds in flight during the outage;
  **`since:<ts>`** replays from an ISO-8601 timestamp using JetStream
  `DeliverByStartTime` and is the **recommended** choice when the store held rows
  before truncation. §14's heuristic: prefer `since:<ts>` or `new` after
  truncation, `all` only when the store is young and the fleet is
  idempotent-safe.
- The coordinator's own persistent de-dup store for its own `.request` durable
  (`$YUGO_COORDINATOR_DEDUP_PATH`, §15 S2-B).

**De-dup arbitration belongs to the receiving adapter**, not to the coordinator's
relay. The relay is unconditional; the adapter is what decides whether an envelope
id has already been claimed — and it does so **however the envelope arrived**.

---

## 4. Discrepancy: bots currently hold `fleet.*.inbox` publish

**This is a live deviation from the target, not an open question.**
`yugo/SPEC.md` §7 states it as a requirement in as many words:

> **Bots never publish to `.inbox` directly and (post-FB-3) never subscribe to
> `.request`** — NATS authz enforces both.

§4.7 says the same from the authz side — "Coordinator has narrowly scoped publish
(only to `.inbox`)" — and grants `.inbox` publish to the coordinator user alone.
§4.3's message flow shows every release to `.inbox` originating at the
coordinator, after the policy check.

Today every bot holds `publish` on `fleet.*.inbox` (§2.2). That means any bot can
write directly into any peer's durable inbox, bypassing the coordinator entirely
— its audit record, its forward accounting, and the policy hold that v0.7b will
add. It is the publish-side twin of the SEV1-1 subscribe gap.

**It does not bypass de-dup.** De-dup lives in the receiving adapter and keys on
envelope id regardless of how the envelope reached the inbox (§3.5). The grant's
hazard is unaudited, ungated injection, not duplicate execution.

**Required action:** revoke `fleet.*.inbox` publish from every bot user, leaving
it to the coordinator. This is a broker-config change and therefore
fleet-infrastructure work, not a change in this repository.

This is **independent of #22** (the bus does not authenticate `from`; the field is
self-asserted). Sender authentication and coordinator interposition are separate
controls: authenticating `from` would not stop an authenticated bot writing
straight to a peer's inbox, and revoking the grant does not tell you who sent
what. Both are wanted; neither substitutes for the other. An earlier revision of
this document treated the grant as an open decision blocked on #22, which was
wrong on both counts.

---

## 5. Migration phases and gates

The reply-lane migration is expand → migrate → contract with two global gates,
defined normatively in `SPEC.md` §6.2.1. **Both gates have passed** — #17 (Expand)
and #18 (Migrate) are closed.

`yugo/SPEC.md` §4.7 also defines a **migration-window exception**: during
per-adapter FB-3 migration the migrating bot user *retains* temporary subscribe
permission on `fleet.<self>.request`, and that grant is revoked in the same PR
that flips the adapter to `.inbox`-only. The end state is §3.2 as written.

**#10 bundles two independent migrations, whose hazards differ:**

1. **Reply-lane contract** — delete the `.result` subscription and handler,
   revoke `.result` authz both directions, drop `FLEET_RESULT`. Hazard: peer
   stranding, which §6.2.1's gates exist to prevent.
2. **Request-lane delivery move** — consume `.inbox` as a durable JetStream
   consumer, stop consuming `.request`, revoke `.request` subscribe. Hazard:
   double delivery. The coordinator relays every released request to `.inbox`
   unconditionally, so an adapter consuming both lanes receives each ordinary
   request **twice**, not only on redelivery. De-dup keys on envelope id rather
   than subject, so both paths share arbitration provided the relayed id is
   unchanged.

Ohm ruled on 2026-09-27 that item 1 — start consuming `.inbox` — may ship as a
separate additive PR ahead of the gated removals, under the migration-window
exception above. That PR must carry:

- a **durable JetStream consumer**, not another core-NATS subscription;
- shared de-dup and correlation handling across both arrival paths;
- ack semantics where **a pending claim is not completed work** — never ack the
  inbox copy merely because the direct path holds a pending claim, or a
  subsequent crash loses the recovery;
- tests for both arrival orders, concurrent delivery, crash/restart and store
  failure;
- the §14 DeliverPolicy behaviour above rather than a blind replay of the
  retained backlog.

Durability is not landed until those conditions pass.

### 5.1 Sender-binding is a third migration, and it is not #10

`yugo/SPEC.md` §4.7.1 (yugo #22) changes the **publish** side of §3.2 while leaving
every subscription where it is. It shares a file with #10 — the broker config — and
nothing else: no gate of #10's is a gate of its own, and its hazard is different in
kind.

Its order, normatively in §4.7.1 and repeated here because this document is what an
operator reads before touching the topology:

1. widen `FLEET_REQUEST`'s subjects and the coordinator consumer's filter to cover
   `fleet.*.request.*`, keeping the three-token shape — **and the `break-glass`
   relay's subscribe grant with them**, since `fleet.*.request` cannot see a
   four-token subject and a break-glass relay is opened precisely when nobody is
   watching for silence
2. teach the coordinator the four-token subject and add the `verified_from` stamp —
   `inbox_subject` currently **raises** on anything that is not exactly three
   tokens, so a four-token delivery before this step is an unacked redelivery loop
   until `max_age`, not a mis-forward
3. grant the new publish subject **in addition to** the old one
4. flip publishers, one adapter per PR
5. teach receivers `verified_from` and the mismatch drop
6. revoke `fleet.*.request` publish — **the only step that makes a forged sender
   impossible**

**Inverting 4 and 1 is total silent loss for that bot** — the publish succeeds, no
stream captures it, nothing is delivered, and the sender's own audit record says it
was sent. That inversion is the cheapest one to make by accident, because the
publisher's change is a single subject string.

---

## 6. How to re-verify this document

```sh
# streams, their subject lists and their consumers' filter subjects
curl -s 'http://127.0.0.1:8222/jsz?streams=1&consumers=1&config=1'

# whether the running broker has reloaded the config file on disk
curl -s http://127.0.0.1:8222/varz   # compare config_load_time with the file mtime

# what each adapter actually subscribes
grep -n 'this.nc.subscribe' src/fleet-bus.ts
grep -n '_nc.subscribe'     yugo/fleet_bus.py

# the coordinator's durable consumer and its ack ordering
sed -n '245,270p' yugo/coordinator.py
sed -n '275,295p' yugo/coordinator.py

# the target authz map, DeliverPolicy and de-dup contract
awk '/^#+ *4\.7/,/^#+ *4\.8/' yugo/SPEC.md
grep -n 'DeliverPolicy' yugo/SPEC.md
```

Broker grants live in the fleet-bus NATS configuration, which is deployment
infrastructure and not in this repository. Read the `authorization` block there;
the passwords are `$VAR` references resolved from a separate included file and
must never be quoted into a document or a review. **Confirm it is the file the
server loaded** — `varz`'s `config_load_time` against the file's mtime — because a
config edited and never SIGHUP'd is not a grant.

**`yugo/config/nats-coordinator-authz.conf` in this repository is a template and is
not what runs.** It describes an `accounts` isolation model with `CHANGEME`
placeholders; the deployed config is a flat `authorization { users = [...] }` block
with no `accounts` block at all, verified 2026-10-01. Quoting the template's
permissions as live — as yugo #22 did — describes a file nothing loads, and its bot
grant (`fleet.>` with `.inbox` denied) is **wider** than the deployed one.
