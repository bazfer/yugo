---
issue: bazfer/yugo#28
status: DRAFT v2 for review — no code
date: 2026-09-25
supersedes: v1, which was returned REWORK. See "What v1 got wrong".
---

# #28 — Bounded recovery for a never-completing session callback

## Summary

**This is a TypeScript-only gap.** The Python port is already bounded, by
construction. And the bound the TypeScript port needs is **not** on the
`injectIntoSession` await — that await is milliseconds long in the deployed
embedder. It belongs on the **age of the pending claim**.

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

So on shutdown: `drain()` cancels the in-flight callback task, `CancelledError`
propagates through the turn, and the `finally` **releases the claim** because
`settled` is still False, then stops the renewer. That is cancellation with
confirmed termination followed by release — precisely what #28's decision 1 asks
for, already implemented.

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
awaited, and is settled only by `publishReply`. **Measure from registration.**

One deadline on that age bounds **both** failure modes with one timer:

- the MCP write that never returns, and
- the far likelier case where the write returns fine and the session never
  replies.

v1 needed a second mechanism for the second case and did not have one. It also
had to agonise over "hung versus slow" because it was timing a hand-off; timing
the claim's age makes the question tractable, because the claim's expected
lifetime is a turn, and turn length is something an operator can observe.

**Existing bound, for completeness:** `pendingReplyClaims` capacity eviction
(`DEFAULT_RECEIVE_LEDGER_CAP = 1000`) is a *count*, not a time. On a quiet bot it
never fires. And the renewal timer is `unref`'d, so process exit stops renewal —
**the only time bound today is process lifetime.**

## 4. On expiry: audit. Do not release.

**No same-id retry producer exists.** Verified across both ports:

- TS `request()` mints one `randomUUID()` and publishes once; on timeout it
  resolves `timed_out` with no re-send.
- The Python request path mints `uuid.uuid4()` per call.
- Zero matches for `republish|resend|retry_publish` in either file.
- Both adapters use core NATS; there is no broker redelivery.

A caller who re-asks mints a **new** envelope id and is never suppressed by the
stuck claim in the first place.

So releasing buys admission for a retry that nobody sends, while paying the
duplicate-injection cost #25 closed over three rounds. **On expiry the adapter
records an audit event — `claude_discord_adapter_claim_age_exceeded`, naming
envelope id, req id and age — and changes nothing else.** Renewal continues. The
row stays. The claim stays owned.

That is a smaller deliverable than v1 promised and it is the whole honest value:
**an 8-day silent suppression becomes an 8-day suppression that says so.**

**When to revisit:** the moment a same-id retry producer exists — durable
execution retries, a replayer, a supervisor that re-publishes with the original
id — release becomes worth its cost. The design should be revisited then, and
that producer named with a file and line rather than assumed.

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

This section is retained even though §4 removes the release path, because the
reasoning is what a future revisit will need.

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

1. A claim whose age exceeds the deadline records
   `claude_discord_adapter_claim_age_exceeded` **and keeps renewing** — mutate by
   deleting the audit call, and by making expiry stop renewal, and watch each
   fail separately.
2. The deadline is measured from **registration**, not from the callback
   returning: a fast `injectIntoSession` followed by a long silence still fires.
   This is the case v1 could not catch and is the reason the design was rewritten.
3. A turn that replies before the deadline records **no** event and settles
   normally. Only meaningful paired with test 1 — stated so nobody reads it as
   standalone coverage.
4. The deadline is independent of renewal cadence: a short lease with many
   renewal ticks and a turn shorter than the deadline still records nothing.
5. `disconnect()` during an active claim leaves renewal running and the row
   present — the regression guard for v1's withdrawn D4.
6. **Python divergence vector:** cancelling the in-flight turn releases the claim
   and stops the renewer, asserting the behaviour the TypeScript port does not
   have. Registered as `known_divergence` per #27.

Test 2 is the load-bearing one. If only test 1 is written, the suite passes with
the deadline still attached to the wrong await.

## Open question for review

**Deadline value, and default-on or opt-in.** Now that the deadline measures a
turn rather than a hand-off, an operator can reason about it: it should exceed
the longest legitimate turn by a comfortable margin.

My view: **default-on**, sized so that firing is itself a signal. With release
withdrawn, the only cost of firing is an audit line — there is no duplicate risk
left to weigh, which is what made v1's version of this question hard. That makes
default-on a much easier call than it was, and I hold it at **high** confidence
now rather than moderate.
