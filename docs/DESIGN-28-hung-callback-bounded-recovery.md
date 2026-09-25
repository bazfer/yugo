---
issue: bazfer/yugo#28
status: DRAFT v3 for review — no code
date: 2026-09-25
supersedes: v1 (REWORK) and v2 (MORE CHANGES NEEDED). See "What v1 got wrong" and "What v2 got wrong".
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

One deadline on that age bounds **both** failure modes with one timer:

- the MCP write that never returns, and
- the far likelier case where the write returns fine and the session never
  replies.

v1 needed a second mechanism for the second case and did not have one.

**But the assumption v2 replaced it with is also wrong, and this is the measured
part.** v2 said the question becomes tractable "because the claim's expected
lifetime is a turn, and turn length is something an operator can observe." The
audit log says otherwise. Matching `.request` `in` lines to any `out` line by
`req_id` on this bot:

```
all history      : 62 claims, 11 settled, 51 never settled  — 82%
post-#25 only    : 39 claims, 10 settled, 29 never settled  — 74%
```

Measured independently twice, same result. **The distribution is bimodal with
nothing in between:** settled turns cluster at 21-65s, unsettled ones never
settle — not late, never. There is no "long turn" population for a deadline to
avoid, and any threshold from two minutes to eight days separates the two
identically.

**So the deadline does not detect a hung callback. It detects an unanswered
request**, and that is the common case rather than the exception. That finding is
filed as its own issue; it is larger than #28 and does not belong buried here.

The consequence for this design is that the event must be **named for what it
catches** — `claude_discord_adapter_claim_unanswered` — and its expected fire
rate documented, or it will be read as an alarm and tuned out within a day.

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
- a `receiveLedger` entry,
- a `pendingReplyClaims` entry.

At the rate measured in §3 that is roughly **30 leaked timers per day on one
bot**, growing linearly toward a 1000-timer floor at ~40 UPDATEs per second.

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

Release goes through **`abandonRepliedClaim(reqId, 'claude_discord_adapter_claim_unanswered')`**,
the existing three-round-reviewed path. It already does the right things:

- defers when `injectionActive` is still true, setting `abandonReason` so
  `finishInjection` performs the release the moment the callback exits;
- otherwise deletes the entry, calls `stopRenewing()`, and releases the claim by
  **owner**, so a replacement owner after a takeover is untouched.

An age deadline is one more entry in that existing class of triggers, not a new
release path. That is the strongest argument for this shape: it adds a *reason*,
not a *mechanism*.

### If a retry producer ever appears

Durable execution retries, a replayer, or a supervisor republishing with the
original id would make release start costing something. Revisit then, and **name
that producer with a file and line** rather than assuming it.

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

This section is now directly load-bearing: §4 releases, so the second consumer's
behaviour above is the behaviour the design invites. It is a delay with a fencing
benefit, and it should be defended as that rather than as a category.

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

1. A claim whose age exceeds the deadline is **released** via
   `abandonRepliedClaim` — entry deleted, `stopRenewing()` called, row released
   by owner. Mutate by removing the release and watch it fail.
2. The deadline is measured from **registration**, not from the callback
   returning: a fast `injectIntoSession` followed by a long silence still fires.
   **This is the load-bearing test** — v1 attached the deadline to the wrong
   await, and without this the suite passes with that error reintroduced.
3. The timer **no-ops on fire when the claim was already settled**, and again
   when it was **replaced by a takeover under the same `reqId`** — asserting the
   identity check, not the key lookup. The second half is the one that protects a
   replacement owner.
4. Deferral: age expires **while `injectionActive` is still true** → no immediate
   release, `abandonReason` recorded, and `finishInjection` performs the release
   when the callback exits. This is the #25 guarantee and must not regress.
5. A turn that replies before the deadline releases nothing and records nothing.
   Only meaningful paired with test 1 — stated so it is not read as standalone
   coverage.
6. The deadline is independent of renewal cadence: a short lease with many
   renewal ticks and a turn shorter than the deadline still does nothing.
7. `disconnect()` during an active claim leaves renewal running and the row
   present — the regression guard for v1's withdrawn D4.
8. **Python divergence vector.** The cancel-then-release behaviour is **already
   covered** by existing tests in `yugo/test/test_fleet_bus_dedup.py`; this work
   does not write them, it **registers the divergence** as `known_divergence` per
   #27's precedent, since the TypeScript port has no equivalent.

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
