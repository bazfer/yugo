---
issue: bazfer/yugo#28
status: DRAFT v6 for review — no code
date: 2026-09-25
supersedes: v1 (REWORK), v2 and v3 (MORE CHANGES NEEDED). See "What v1 got wrong" and "What v2 got wrong".
---

# #28 — Bounded recovery for a never-completing session callback

## Summary

**This is a TypeScript-only gap.** The Python port is already bounded — by two
mechanisms, not one (§2). The bound the TypeScript port needs is **not** on the
`injectIntoSession` await, which is milliseconds long in the deployed embedder.
It belongs on the **age of the pending claim**.

**On expiry the claim is RELEASED**, through the existing `abandonRepliedClaim`
path. Not because a retry is waiting — none is — but because every unsettled
claim leaks a renewal timer and two ledger entries, and at the measured rate that
is ~30 leaked timers per day on one bot.

**The deadline detects an unanswered request, not a hung callback**, and at a 74%
non-settle rate that is the common case rather than the exception. It must be
named accordingly.

## What v1 got wrong

Recorded rather than quietly replaced, because two of the three errors were
errors of *scope* and the next reader should see how the design was aimed wrong.

1. **v1 bounded the wrong await.** It raced `injectIntoSession` and sized a
   deadline for "hung, not slow" work. In the deployed embedder that callback is
   a single MCP notification write
   (`artifice-discord/0.8.2/server.ts:980`, read off the running version). It
   returns in milliseconds. The turn happens *afterwards*, under renewal that
   continues until `publishReply`. So v1 left the realistic hang — **frame
   delivered, reply never sent** — completely unbounded, while claiming to fix
   it.
2. **v1 made every reconnect a lease-loss event.** Its D4 stopped renewal inside
   `disconnect()`, which `run()` calls on every reconnect cycle, not only at
   shutdown. Healthy in-flight work would have lost its lease during ordinary
   connection churn.
3. **v1 assumed a retry producer that does not exist.** Its argument for
   releasing a hung claim was that a retry could then be admitted. Nothing in
   either port sends a same-id retry (§4 below).
4. **v1 ignored the Python port entirely**, although #28 names `_teardown`
   explicitly. Python turns out to be the port that already solves this.

## What v2 got wrong

1. **v2 assumed a claim's expected lifetime is a turn.** Measurement says 74% of
   claims never settle at all, in a bimodal distribution with no middle. So the
   deadline detects an unanswered request, not a hung callback, and "firing is
   itself a signal" was false. §3.
2. **v2 chose audit-only on an incomplete cost/benefit.** It weighed release
   against admitting a retry, found no producer, and never weighed the resource
   leak that release fixes for free. §4 reverses it.
3. **v2 named only one of Python's two bounds**, giving the shutdown path and
   omitting `RESPONSE_TIMEOUT`, which is the one that answers #28's actual
   complaint. §2.
4. **v2 left the age timer's lifecycle unspecified**, which is the "fix the
   class" review round waiting to happen. §4a.

## What v3 got wrong

1. **§7 claimed a fencing benefit the chosen path does not have.** §7 describes
   *lapse*; §4 chose *release*. Release `DELETE`s the row, so nothing is
   preserved and nothing is fenced — and nothing needs to be. §7.
2. **§4 listed `receiveLedger` among the leaked resources release fixes.**
   Release does not clear it and must not: a late reply after release still
   publishes, reading the inbound envelope from exactly that entry. Listing it
   invited a regression. §4.
3. **The event could not carry the name the design insisted on.**
   `abandonRepliedClaim` hardcodes its `reason` and puts the caller's string in
   `note`, so the code would have been misleading and the name grep-invisible.
   §4 now threads a reason parameter. §4.
4. **"~30 leaked timers per day" was the worst day**, not the rate. 27 on
   2026-09-24, ~10 on a three-day mean.

## 1. The delivery contract already permits either answer

#28's decision 3 asks whether a hung callback should lose its claim, calling
release "the at-least-once-consistent choice". **SPEC §6.4 has since settled the
contract as "deduplicated admission; effects may repeat; eventual execution is
not guaranteed."** Eventual execution is not promised, so the contract compels
neither disposition. The decision is made on cost and benefit, below, not by
appeal to a guarantee.

#28's fourth item — that a clock step is not the only cause of lease loss — **is
already fixed**; §6.4 carries a non-exhaustive list.

## 2. Python is already bounded. The gap is TypeScript's.

`_on_request` opens **one cleanup scope for the entire claimed lifetime**
(`yugo/fleet_bus.py`, the `settled = False` / `try` / `finally` around the claim)
and deliberately **does not catch `CancelledError`** — the `except BaseException`
re-raises it, with the stated reason that swallowing it would stall `drain()`
behind an LLM call.

**Python's bound is TWO mechanisms, and v2 named only one.** Corrected in v3:

- **Living-process bound: `RESPONSE_TIMEOUT`** (120s, `yugo/bot.py`), which caps
  the whole `ask_llm` turn. This is the one that answers #28's actual complaint —
  a claim held "for as long as the process lives" — and it lives in the
  **embedder**, not the adapter.
- **Shutdown bound: drain, then close, then the cleanup scope.** `drain()` does
  **not** cancel; it waits on the pending queue. After `drain_timeout` (5s in
  yugo) it falls through to `_close()`, which cancels each subscription's message
  task. nats-py awaits the callback inline and yugo awaits `_on_request` inline
  with nothing swallowing in between, so the `CancelledError` reaches the cleanup
  scope's `finally`, which **releases the claim** because `settled` is still
  False, then stops the renewer.
  Two caveats on it: `RESPONSE_TIMEOUT` is read with `_env_int` and has **no
  ceiling**, so this bound is operator-defeatable by configuration; and
  `history.build_messages` / `record_turn` are **synchronous**, so a stall there
  blocks the whole loop rather than one claim — a different failure class, out of
  scope here but worth not confusing with this one.
- **Not bounded except by shutdown:** the adapter's own awaits inside the scope —
  `_warn_origin`'s publish, which the code comment says "can stall on a full
  pending buffer mid-outage", and the reply publish.

So "bounded by construction" is true, and the construction is two mechanisms with
a gap between them. Cancellation with confirmed termination followed by release —
#28's decision 1 — is the shutdown half, already implemented.

**Correction to the review that surfaced this:** the bound does **not** come from
`_teardown`. `_teardown` only drains or closes the connection. The bound comes
from asyncio cancellation semantics reaching that single `finally`. The
distinction matters because it is the *cleanup scope*, not the teardown method,
that would have to be preserved by any future refactor.

The TypeScript port has no equivalent: `injectIntoSession` is an awaited promise
with no cancellation channel, and `disconnect()` neither cancels it nor stops
renewal.

**This is a `known_divergence`**, and it owes a conformance vector per #27's
precedent. Naming it is part of this work.

## 3. The bound that actually fits: pending-claim age

The claim is registered at `src/fleet-bus.ts:1567`, before the callback is
awaited, and is removed by `publishReply` on settle, by `abandonRepliedClaim`, or by capacity
eviction. **Measure from registration.**

**Corrected in v5 — this bounds ONE failure mode, not two.** v3 and v4 both said
"both failure modes with one timer". That is false, and the reason it is false is
also the reason it must stay that way:

- **Mode 2 — the write returns, the session never replies. BOUNDED by this
  design.** The claim is registered, `injectionActive` is already false, and the
  deadline releases it.
- **Mode 1 — `injectIntoSession` itself never returns. NOT bounded, by design.**
  `abandonRepliedClaim` only records `abandonReason` while `injectionActive` is
  true; the actual release runs in `finishInjection`, which runs from the
  `finally` around the callback — so it runs only when the callback returns or
  throws. A callback that does neither never reaches it.

**That is the correct disposition, not a gap to close.** Releasing a claim whose
callback is still executing is exactly the premature release #25 fixed over three
rounds. Mode 1 remains bounded by process lifetime, as today.

So: **the timer no-ops while `injectionActive` is true.** It does not defer, does
not record an abandon reason, and does not fire again. One check, and it makes
the scope honest instead of merely documented — see §4b for the state that
deferring would otherwise create.

v1 needed a second mechanism for the second case and did not have one.

**But the assumption v2 replaced it with is also wrong, and this is the measured
part.** v2 said the question becomes tractable "because the claim's expected
lifetime is a turn, and turn length is something an operator can observe." The
audit log says otherwise. Matching `.request` `in` lines to any `out` line by
`req_id` on this bot:

```
all history      : 62 lines, 11 settled, 51 never settled  — 82%
post-#25 only    : 39 claims, 10 settled, 29 never settled — 74%
```

(Honest count: 60/11/49 — two of the 62 carry the `ledger_matched` note and never
had a pending claim.) Settle latencies span **13.9-64.6s**. The distribution is
bimodal with nothing in between: settled turns cluster in that band, unsettled
ones never settle — not late, never.

### What that population actually is — corrected in v6

v3 through v5 read this as "sessions do not reply", and sized the deadline
against it. **That diagnosis was wrong.**

`onRequest` handles `in_reply_to` **only when an outbound waiter exists**. An
unwaited reply, or one arriving after its waiter timed out, falls through to the
fresh-turn path and is handed a pending claim **owed a reply it will never get**.
The `.result` lane does the opposite with the identical case — unmatched replies
there go to `injectUnsolicited` and settle at inject time. The two lanes disagree
about what an unmatched reply is.

Measured: **40 of the 51 unsettled claims arrive within 120 seconds of this bot's
own outbound `.request` publish to a peer** — 28 to 45 second gaps, which are peer
turn times. And the whole log holds **7 `request_timeout` drops**, so nearly every
`bus_request` is `wait:false` and registers no waiter.

So most of this population is **peer answers to questions this bot asked**,
mis-classified on arrival. The traffic pattern generating it is this bot's own
normal way of talking to the fleet.

**Consequences for this design, and they are not small:**

- The event is **not** "the session ignored a request". Naming it
  `claim_unanswered` would enshrine a wrong diagnosis in the audit log.
- The real fix is routing unmatched replies on the `.request` lane through
  `injectUnsolicited`, matching `onResult`. That is #27's asymmetry, filed
  separately, and it removes most of this population at the source.
- **This deadline is a backstop for whatever remains, not the fix.** v3-v5 sized
  and justified it against a population that mostly should not exist.

The genuinely-unanswered population may be in the remaining 11. That is a much
smaller number than the one this design has been arguing from.

So the event is named for **the point in time**, not a diagnosis it cannot
support: **`claude_discord_adapter_claim_deadline_released`**. A name that
asserts *why* the claim was never settled would be a guess, and on current
evidence the most common answer is not the one v3-v5 assumed.

**Existing bound, for completeness:** `pendingReplyClaims` capacity eviction
(`DEFAULT_RECEIVE_LEDGER_CAP = 1000`) is a *count*, not a time. On a quiet bot it
never fires. And the renewal timer is `unref`'d, so process exit stops renewal —
**the only time bound today is process lifetime.**

## 4. On expiry: RELEASE, via the path that already exists

**v2 said audit-only. That was wrong, and the reversal is the main change in
v3.**

v2 weighed release against exactly one benefit — admitting a retry — found no
producer, and stopped there. It never weighed the **resource leak**, which
release fixes for free.

### The leak

Every unsettled claim leaves behind, until capacity eviction at
`DEFAULT_RECEIVE_LEDGER_CAP = 1000`:

- a `setInterval` from `renewWhileRunning` issuing one SQLite `UPDATE` every
  `lease × 0.4` — 24s at the default 60s lease,
- a `pendingReplyClaims` entry,
- a `receiveLedger` entry — **which release does NOT clear, deliberately.**

At the rate measured in §3 that is **10-30 leaked timers per day on one bot** —
27 on the worst day measured, ~10 on a three-day mean.

**Corrected in v5: the ceiling is lower than v4 claimed, and the leak is
self-limiting.** `renew()` returns false once the row is gone, and
`renewWhileRunning` clears its interval on false — so a leaked timer lives at
most **TTL plus prune cadence**, not until capacity eviction. And the plugin
restarts, which clears them.

**Corrected in v6 — v5 asserted this as verified and it was false.** v5 said the
running server started after *all* of the worst day's unsettled claims. It did
not. The server started 2026-09-24T20:03:41Z and unsettled claims followed at
20:06:49Z, 23:47:15Z, and twice more on 09-25. The live store confirms **29
pending rows, 10 completed, and four leases still renewing in the running
process** as this is written. Restarts reduce the population; they did not clear
it.

Reaching v4's "1000-timer floor at ~40 UPDATEs/second" would need ~125 unsettled
claims per day sustained inside a single 8-day process lifetime. At the measured
~10/day the steady state is roughly **80 timers, about 3 UPDATEs per second.**

**And each leaked row emits a spurious `dedup_lease_lost` drop line when prune
finally kills it** — 27 false "duplicate" alarms in the audit log on the worst
day measured, from rows that were never duplicates. That noise is arguably worse
than the writes.

Still worth closing — 3 writes per second of pure waste against a store on the
critical path is real, and the bound depends on restarts that are not
guaranteed.
But the honest number is 3/s, not 40/s, and a design should not be sold on a
figure an order of magnitude too large.

### Release does not lose a late answer — and that is why `receiveLedger` stays

**Do not "fix" the `receiveLedger` entry.** `abandonRepliedClaim` leaves it
alone, and that is load-bearing at a 74% non-settle rate.

A session that replies *after* release still succeeds. `publishReply` finds no
pending claim under the `reqId`, so the token check is skipped — the code says so
at the guard: *"Replies with NO pending claim (unsolicited and late-reply paths)
are unaffected: they mutate no claim."* It then reads the inbound envelope from
`receiveLedger`, builds the reply, and publishes it. No settle, no store write,
answer on the wire.

Clear `receiveLedger` on release and that path fails with
`claude_discord_adapter_req_id_unknown` instead. So the entry is **retained until
capacity eviction, on purpose.** Listing it among the leaked resources without
this paragraph would have invited exactly that regression.

**The codebase already documented this exact failure**, in
`abandonRepliedClaim`'s docstring: *"Left alone the renewal timer keeps extending
a lease for work that finished long ago: on a quiet process the entry is never
capacity-evicted, peer retries are rejected as duplicates, and recovery needs an
unrelated eviction, a restart, or the full TTL rather than the lease. Releasing
hands the envelope straight back."* That reasoning was written for a different
trigger and applies unchanged here.

### Why the cost is zero today

v2's own finding, unchanged and still load-bearing: **no same-id retry producer
exists.** TS `request()` mints one `randomUUID()` and publishes once, resolving
`timed_out` with no re-send; the Python request path mints `uuid.uuid4()` per
call; both adapters use core NATS with no broker redelivery. A caller who re-asks
mints a *new* id and was never suppressed by the stuck claim.

So there is nothing to duplicate. The release path's cost — the
duplicate-injection window #25 closed over three rounds — **is not paid on
current evidence.**

### The mechanism: reuse, do not invent

Release goes through **`abandonRepliedClaim`**, the existing three-round-reviewed
path. It already does the right things:

- defers when `injectionActive` is still true, setting `abandonReason` so
  `finishInjection` performs the release the moment the callback exits;
- otherwise deletes the entry, calls `stopRenewing()`, and releases the claim by
  **owner**, so a replacement owner after a takeover is untouched.

An age deadline is one more entry in that existing class of triggers, not a new
release path. That is the strongest argument for this shape: it adds a *reason*,
not a *mechanism*.

### The reason code needs one small change to that function

`abandonRepliedClaim` currently hardcodes
`reason: 'claude_discord_adapter_reply_undelivered'` and puts the caller's string
in `note`. That code is **wrong for this case** — nothing was undelivered,
nothing was attempted — and SPEC §8 defines `reason` as the drop code, so an
event the design insists must be "named for what it catches" would be
grep-visible only in `note`, under a misleading code.

**Thread the reason through as a parameter, defaulting to the current value.**
No behaviour change for existing callers.

**Two emit sites, not one** — corrected in v5. `finishInjection` **also**
hardcodes `reason: 'claude_discord_adapter_reply_undelivered'` and puts the
stored string in `note`, so changing only `abandonRepliedClaim` leaves the
deferred branch still emitting the wrong code. Both sites change, or
`finishInjection` emits the stored code as its `reason`.

(With §3's no-op the deferred branch is unreachable *from this trigger*, but the
site is shared with every other abandon reason, so the fix is owed regardless.)

This is the one place the design does touch the mechanism rather than only adding
a reason, and it is worth being explicit that v3 promised the naming while
specifying something that could not deliver it.

### The no-producer finding is an observation. Make it an invariant.

**"No same-id retry producer exists" is measured, not enforced.** Anyone adding a
retry loop to `request()` in either port silently converts every released claim
into a re-injection — and nothing would fail.

**So add a tripwire:** a test on each port's `request` path asserting **one
publish per envelope id**, whose failure message names this design. That turns a
prose promise into something a future PR trips over, which is the only kind of
promise worth writing down.

### Make the audit line consumed rather than decorative

At a 74% fire rate the event is **not an alarm and must not be described as
one** — but it is the only per-request record that a request went unanswered, and
it is the evidence base for the separate issue filed from §3.

**Add a `claims_released_unanswered` counter to `statusSnapshot`**, which today
carries `injections_delivered` / `injections_failed` and nothing about claims.
`bus_status` then surfaces the rate without anyone grepping a log.

**This is a CROSS-REPO change, not one edit.** `statusSnapshot` lives in the
plugin (`artifice-discord`, `src/fleet-bus-wiring.ts`), not in yugo, and it
counts injections by *wrapping* `injectIntoSession`. A released-claim counter has
nothing to wrap — it needs a new surface on `FleetBus` (a getter or a callback)
**plus** a plugin change consuming it. v4 presented it as a single edit in one
file; it is two repos and a released plugin version. Scope it accordingly or drop
it from this design and file it separately.

### If a retry producer ever appears

Durable execution retries, a replayer, or a supervisor republishing with the
original id would make release start costing something. Revisit then, and **name
that producer with a file and line** rather than assuming it.

## 4b. The state that deferring would create — and why the no-op avoids it

**Found on an independent fresh review, after four rounds had approved the
deferring version.** Worth recording in full, because the sequence is reachable
and every step is ordinary.

If the timer deferred instead of no-opping, with an embedder whose callback spans
the turn — which is how `fleet-bus.test.ts` models `injectIntoSession`:

1. Deadline fires while `injectionActive` is true → `abandonRepliedClaim` records
   `abandonReason = claim_unanswered` and returns.
2. The session replies. `publishReply` puts **the answer on the wire** and writes
   its `out` audit line.
3. `settleRepliedClaim` then **refuses**, because `abandonReason` is set.
4. The callback exits. `finishInjection` **deletes the row** and audits
   `reply_undelivered`.

Net result: **an answered envelope, released as unanswered**, with a false drop
line and the `claims_released_unanswered` counter incremented for a claim that
was in fact answered. The metric that §3's separate issue will be measured
against would be corrupted by the mechanism meant to produce it.

Two ways out were available. Clearing a deadline-origin `abandonReason` inside
`settleRepliedClaim` would work, but it special-cases one reason inside a
reviewed function. **The no-op is better:** every #25 path stays byte-identical,
and the honest scope in §3 falls out of the mechanism rather than relying on
prose.

Unreachable with the deployed millisecond embedder. That is exactly why it is
worth writing down — it is invisible today and normal for any embedder whose
callback spans the turn.

## 4c. The deadline value

**v4 excluded this from the open question and then never answered it anywhere.**
"Any value from two minutes to eight days" is an observation, not a
specification.

- **Constant:** `DEDUP_CLAIM_DEADLINE_MS`, alongside the existing dedup constants.
- **Default: 15 minutes.** The "14× the longest observed settle" argument v5 used
  is **withdrawn** — it rests on 11 samples capped at 65s, and a Claude Code turn
  mid-task routinely runs longer than 15 minutes, so that reasoning would have
  justified almost any number.

  The honest defence is that **premature release costs close to nothing on
  current evidence**: a late reply still publishes, because `receiveLedger` is
  retained (§4), and no same-id retry producer exists. So the cost of choosing
  too short is a released claim that still answers correctly.

  The price is that a released-then-answered claim is counted as released. That is
  §4b's corruption at a longer timescale, and it is why the counter should be
  correctable — tag a publish that lands after its claim's release so the number
  can be adjusted rather than quietly wrong.
- **Config knob:** overridable per deployment, like the other dedup settings.
- **Bounds: `leaseMs <= deadline < dedupTtlMs`**, validated at construction,
  refusing a configuration that violates either.

  **Upper bound, with the justification corrected in v6.** v5 said a deadline at
  or above TTL is "silently inert". Not quite — the timer still deletes the
  `pendingReplyClaims` entry and calls `stopRenewing()`; what it cannot do is
  release a row that prune already removed, and renewal would have self-terminated
  at TTL anyway once `renew()` started returning false. So the real reason for the
  bound is that **past TTL the mechanism is redundant rather than inert**, and a
  configuration that can never do its job should be refused rather than shipped.

  **Lower bound, which v5 omitted entirely.** A zero or tiny deadline fires inside
  the inject await, where under v6's re-arm rule it would re-arm in a tight loop,
  and under v5's fire-once rule it would do nothing at all. Below one lease it
  cannot distinguish a slow turn from a stuck one. Refuse it.

## 4a. Timer lifecycle

Specified rather than left to the implementer, because leaving it unsaid is the
"fix the class" review round waiting to happen.

The per-claim age timer is `unref`'d and **fires once**. It does **not** need
clearing from any of the four existing removal paths — settle, abandon, eviction,
`finishInjection`. Instead, **on fire it no-ops unless the claim it was created
for is still the live one**:

```
pendingReplyClaims.get(reqId) === claim && claim.abandonReason === undefined
```

Identity, not key — the same discipline `finishInjection` already uses, and for
the same reason: after a takeover the key may belong to a replacement owner, and
a stale timer must never act on someone else's claim.

**And while `claim.injectionActive` is true it RE-ARMS for another deadline
period** rather than dying (§3, §4b). Corrected in v6.

v5 said "no-ops and is gone", which opened a hole the size of the original bug:
claim registered at t=0, turn runs 20 minutes, timer fires at 15 and finds the
injection active, no-ops, **and is gone**. The callback returns at 20 without
publishing a reply, `finishInjection` finds no `abandonReason` and returns — and
the claim renews forever. That is #28's original state **with the mechanism
installed and silent**, and it is Mode 2 by this document's own definition.

Re-arming keeps §4b's guarantee intact: still no `abandonReason` recorded while
the injection runs, so the answered-but-released corruption cannot occur, and the
claim is re-examined once the callback has exited.

**Note:** `pendingReplyClaims.get()` is an LRU touch, so a fire against a live
claim moves it to most-recently-used. Harmless at this scale and at a one-shot
timer's frequency, but recorded so nobody discovers it as a surprise.

This keeps all four removal paths untouched, which matters because each of them
was reviewed independently and none should acquire a new obligation.

## 5. No change to `disconnect()`

v1's D4 is **withdrawn entirely.** Renewal is a local SQLite write and has no
business depending on the bus connection. `run()` calls `disconnect()` on every
reconnect, so stopping renewal there would make routine churn a lease-loss event
for healthy work.

The shutdown case it was meant to serve is already handled: the plugin calls
`stop()` then `process.exit(0)` within ~2s, and the renewal timer is `unref`'d.

**Documented consequence, unchanged from today's behaviour:** a disconnected
adapter's claims are not released by disconnecting. They end when the process
ends, or when their lease lapses after it.

## 6. `AbortSignal`: optional, and the reason matters

#28's decision 1 asks whether `injectIntoSession` should gain a cancellation
contract that embedders are **required** to honour. **No.**

v1 argued this from "an injection cannot be un-injected", which conflates
retracting a delivered effect with preventing an undelivered one. The correct
argument is narrower and port-specific:

- For the **deployed TypeScript embedder** there is nothing to prevent — the
  callback is a single notification write, so a signal has no window to act in.
  It would be a no-op dressed as a safety feature.
- For the **Python port** cancellation is real and already works, through asyncio
  rather than through any contract we would define.

So the event MAY carry an `AbortSignal` that an embedder MAY honour, documented
as meaning exactly one thing: **we have stopped waiting.** No adapter correctness
may depend on it. Requiring it would be a rule with nothing able to enforce it.

## 7. "Delayed", not "different in kind"

v1 claimed that letting a lease lapse is categorically different from deleting
the row. It is not. A second consumer claiming a lapsed pending row issues a new
owner under the **original** `req_id`, overwrites `receiveLedger` and
`pendingReplyClaims` under the same key, and `BoundedLru.set` on an existing key
deletes-then-sets **without firing `onEvict`**, silently dropping the displaced
claim.

The genuine differences are narrow and worth stating accurately: **`reqId` is
preserved, so token fencing engages** — a late `publishReply` carrying the stale
token is rejected with `reply_token_mismatch` — and the row stays inspectable for
up to one lease. That is a delay with a fencing benefit, not a category.

**Corrected in v4: this section describes the LAPSE path, which §4 does not
take.** v3 claimed it was "directly load-bearing" because §4 releases. It is not.

Lapse and release differ concretely. On **lapse** the row survives with an
expired lease, a second consumer takes it by `UPDATE lease_owner`, the `req_id`
is preserved, and token fencing therefore engages. On **release** the row is
`DELETE`d, so a later same-id arrival hits `INSERT OR IGNORE` on an absent row
and succeeds with the caller's **freshly minted** `reqId`. Nothing is preserved
and nothing is fenced — **and nothing needs to be**, because with no pending
claim under the old key there is no claim for a stale attempt to corrupt.

So the fencing benefit v1 and v3 both reached for **does not exist on the path
this design chose**, and claiming it would mislead. The section is kept as an
accurate account of the lapse path, because a future revisit that reconsiders
lapse-instead-of-release needs it.

**Consequence for §9 test 3:** the takeover half is unreachable through release.
It must force a lapse to exercise the identity check — which is exactly what the
existing test at `src/fleet-bus.test.ts:892-925` does, with a raw
`UPDATE lease_until_ms = 0`.

## 8. Verified and unchanged: the late-returning callback

Traced, and it holds:

- **Late return** → `finishInjection` clears `injectionActive` on the captured
  object; a replacement owner is untouched.
- **Late throw** → `abandonReason` is set on the captured object; the identity
  check fails so the map is not mutated, and `releaseClaim(..., originalOwner)`
  deletes zero rows because the row belongs to the replacement.
- **Late reply** → token mismatch, rejected.
- **No takeover, completion after expiry** → `complete()` checks owner, not lease
  validity, so the original owner still settles correctly.

Nothing in this design changes any of that.

## 9. Tests

Rebuilt again in v6. Two more were found vacuous, one named an indistinguishable
alternative, and three were missing.

1. A claim whose age exceeds the deadline, **with `injectionActive` false**, is
   released via `abandonRepliedClaim` — entry deleted, `stopRenewing()` called,
   row released by owner.
2. **The deadline is keyed to claim age, not to the `injectIntoSession` await.**
   v5 reworded this to "registration, not callback return" — but registration and
   inject-start are the **same instant**, so that wording discriminated nothing.
   Restored to the original discriminator, with a callback of length T shorter
   than the deadline D: the timer must not be racing the callback.
3. **The timer no-ops when its claim was replaced by a takeover under the same
   `reqId`** — the identity check, not the key lookup. Setup must force a *lapse*
   (`src/fleet-bus.test.ts:892`'s raw `UPDATE lease_until_ms = 0`), since release
   deletes the row.
4. **The timer re-arms while `injectionActive` is true**, records no
   `abandonReason`, and the claim settles normally if the session then replies.
   Guards §4b's corruption.
5. **NEW — the hole v5 opened.** Callback longer than the deadline, returning
   **without** a reply: the re-armed timer must release it. Under v5's fire-once
   rule this claim renewed forever with the mechanism installed and silent.
6. **NEW — late reply after release still publishes.** The tripwire for §4's "do
   not clear `receiveLedger`", which until now had no test at all despite the
   design naming it as a property.
7. **NEW — a deadline outside `leaseMs <= d < dedupTtlMs` is refused at
   construction**, both bounds.
8. **NEW — `request()` publishes exactly once per envelope id**, per port. §4
   promised this tripwire and §9 never listed it.
9. The deadline is independent of renewal cadence. **Passes with the mechanism
   absent** — a coupling guard, not coverage.
10. A turn that replies before the deadline releases nothing. **Vacuous on its
    own** — after a settle the key is gone, so any implementation passes. Kept
    only as a pair with test 1, and labelled.
11. `disconnect()` during an active claim leaves renewal running. **Vacuous by
    construction**, since this design touches nothing in `disconnect()`. Kept as a
    regression guard for v1's withdrawn D4 and labelled as such.
12. **Python divergence.** Already covered by `yugo/test/test_fleet_bus_dedup.py`
    (parametrized `CancelledError`; cancel mid-publish). The `known_divergence`
    conformance vectors carry envelope `accept|reject` verdicts and **cannot
    represent a cancellation lifecycle** — record the divergence in SPEC §6.4
    prose instead.

Tests 9, 10 and 11 are labelled as weak on purpose. A suite that hides which of
its tests cannot fail is worse than a smaller one.

## Open question for review

**Not the deadline value. The event's name and its expected fire rate.**

v2 asked whether to default the deadline on, and answered "yes, sized so that
firing is itself a signal", at high confidence. **The §3 measurement destroys
that reasoning.** At a 74% non-settle rate the event fires on roughly three of
every four request turns. "Firing is itself a signal" is false; an alarm at that
rate is tuned out within a day.

What I now propose, and what I want argued with:

- **Default-on, still** — because the leak in §4 is real and grows linearly, and
  releasing is free on current evidence. The release should happen whether or not
  anyone reads the audit line.
- **Named for what it catches**: `claude_discord_adapter_claim_unanswered`, not
  `claim_age_exceeded` and certainly not anything with "hung" in it. It detects
  an unanswered request, which on this fleet is the *common* case.
- **Fire rate documented next to the constant**, with the measurement and its
  date, so the first person to see it in a log does not open an incident.

The separate issue filed from §3 is where the 74% itself gets fixed. This design
should not pretend to address it, and should not be blocked on it either: the
leak is worth closing regardless of why sessions do not reply.

**Confidence: moderate.** One bot, 62 samples, and Kat's and Luna's logs have not
been read. If their rates differ materially, the naming and the documented rate
both need revisiting — but not the release decision, which stands on the leak
alone.
