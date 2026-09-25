---
issue: bazfer/yugo#28
status: DRAFT for review — no code
date: 2026-09-25
---

# #28 — Bounded recovery for a never-completing session callback

## What the issue asked, and what changed under it

#28 lists three decisions. **Decision 3 has been overtaken by SPEC §6.4**, merged
since the issue was written.

The issue frames decision 3 as whether a hung callback should eventually lose its
claim, "accepting a duplicate injection as better than an indefinitely suppressed
envelope — **which is the at-least-once-consistent choice**."

§6.4 now states the contract as **"deduplicated admission; effects may repeat;
eventual execution is not guaranteed."** Eventual execution is explicitly *not*
promised, so the contract does not oblige us to force release. It permits either
disposition.

So decision 3 is no longer "which does the contract require" but **"which do we
prefer, given it permits both."** That is a materially easier question and it
should be answered on its merits, not by appeal to a guarantee that no longer
exists.

The issue's fourth paragraph — that a forward clock step is not the only cause of
lease loss — **is already fixed**. §6.4 carries a non-exhaustive list naming
renewal failure, event-loop stalls, TTL pruning of a live pending row and
wall-clock steps. Nothing further owed there.

## The mechanism, read off the code

`onRequest` (`src/fleet-bus.ts:1542`) starts `renewWhileRunning`, registers a
`PendingReplyClaim` **before** awaiting, then awaits `injectIntoSession`.

- `renewWhileRunning` (`:1639`) is a `setInterval` at
  `max(50, leaseMs * DEDUP_LEASE_RENEW_RATIO)` that renews the lease until
  `stopRenewing()` clears it.
- The claim stays **PENDING** past the callback's return; it is settled by
  `publishReply`, not by injection completing. That is deliberate and documented
  at `:1586-1601`.
- `finishInjection` runs in a `finally`, so a callback that returns **or throws**
  releases correctly.

**A callback that never does either** never reaches that `finally`.
`stopRenewing` is never called, the interval keeps renewing, and the claim is
owned for as long as the process lives.

**One real bound already exists, and it is worth stating precisely:** the renewal
timer is `unref`'d (`:1669`). It cannot hold the event loop open, so process exit
stops renewal and the lease lapses. **The existing bound is process lifetime.**
Within a living process there is none.

`disconnect()` (`:1025`) clears the heartbeat, unsubscribes, drains NATS. It does
**not** stop renewal timers and does not cancel in-flight injections. A
disconnected adapter goes on renewing a hung claim indefinitely.

## The constraint that decides the design

**An injection cannot be un-injected.** Once the envelope has reached the
session, no signal retracts it. So cancellation cannot prevent the effect — it
can only stop *us waiting* for a completion signal.

This matters because it rules out the shape decision 1 proposes. Requiring
embedders to honour an `AbortSignal` would read as "cancellation prevents the
work," and it does not. That is the same class of overclaim §6.4 was rewritten
twice to remove.

## Proposal

### D1 — No required cancellation contract. An optional signal, narrowly scoped.

`injectIntoSession` gains **no mandatory** cancellation contract. The event MAY
carry an `AbortSignal` that an embedder MAY honour, documented as doing exactly
one thing: **telling the embedder we have stopped waiting.** It does not retract
an injection, and no part of the adapter's correctness may depend on an embedder
honouring it.

Rejected alternative: requiring embedders to honour it. It cannot be enforced, it
cannot deliver what its name implies, and an unenforceable requirement is
a rule in prose with nothing behind it.

### D2 — Bound the await, not the work.

Race `injectIntoSession` against a deadline. **On expiry:**

1. Call `stopRenewing()`.
2. Record an audit event — `claude_discord_adapter_injection_deadline_exceeded` —
   naming envelope id, req id and elapsed time. The duplicate this permits must be
   visible, matching the existing `dedup_lease_lost` precedent at `:1660`.
3. **Leave the pending row in place.** Do not delete it, do not settle it, do not
   abandon it. Its lease lapses naturally.
4. Leave `injectionActive` true and the `PendingReplyClaim` captured. If the
   callback later returns or throws, `finishInjection` still fires **against the
   captured object**, which is already by-identity (`:1570-1578`) and therefore
   safe after a takeover.

**Deleting the row is the one thing this must not do.** That is precisely the
premature-release defect fixed in #25 rounds 8-10, and #28 says so itself.
Letting the lease *lapse* is different in kind: the row stays, the state stays
inspectable, and takeover goes through the normal predicate rather than a special
path.

**Deadline value:** a multiple of the lease, configurable, defaulting to
something large enough that it never fires for healthy slow work. A turn that
legitimately runs long is the normal case; renewal exists to support it
(`:1626`). This deadline is for *hung*, not *slow*, and a default that catches
slow turns would convert a working feature into a duplicate generator.

### D3 — A hung callback loses its claim. Argued, not assumed.

**The strongest argument against**, which must be stated first: we cannot cancel
the first callback, so releasing the claim creates a genuine concurrent duplicate
— the exact condition #25 spent three rounds closing. If the callback is merely
slow, this is strictly worse than waiting.

That argument is answered by the deadline being for hung rather than slow work,
and by the fact that §6.4 already permits repeated effects. It is not answered by
pretending the duplicate is free.

**The argument for**, on the merits rather than by contract appeal: an
indefinitely held claim suppresses the envelope for the full TTL — **8 days** —
and a durable store means that survives restarts. The failure mode is silent and
long-lived. A lapsed lease is recoverable and audited.

**Honest limit, and it constrains how much this is worth.** In the deployed
topology there is **one consuming process per store file** — measured
2026-09-25: three accessors, three files, no sharing. So no second consumer is
waiting to take over. Releasing buys only that a *publisher retry* can be
admitted by the same process. If the session is still wedged, that retry hangs
too.

**So this is worth doing, and it is not worth overselling.** It converts a
permanent silent suppression into a recoverable audited one. It does not
guarantee the envelope ever executes — §6.4 already says nothing does.

### D4 — `disconnect()` stops renewal; claims outlive the connection.

`disconnect()` stops the renewal timer for every claim whose injection is still
active, records one audit event per claim, and **leaves the rows in place with
lapsing leases.**

Rejected: waiting with a deadline inside `disconnect()`. It makes shutdown block
on a hung callback, which is the same hang one layer up.

Rejected: deleting claims on disconnect. #28 names this as not-a-fix and it is
right — the callback is still running.

Documented consequence: **a disconnected adapter's claims are not immediately
free.** They lapse on the normal lease timeline. Anything reasoning about
post-shutdown state must account for that window rather than assuming disconnect
releases ownership.

## Tests, each mutation-verified

1. A callback that never settles → deadline fires → `stopRenewing` called, audit
   event recorded, **row still present**.
2. Same, then the callback returns late → `finishInjection` fires against the
   captured claim and does not touch a replacement owner's claim under the same
   reqId.
3. Same, then the callback *throws* late → same, with the abandon reason recorded
   on the captured object.
4. A slow-but-healthy callback finishing just under the deadline → renewal
   continues, no audit event, claim settles normally at `publishReply`.
5. `disconnect()` with an injection active → renewal stopped, audit recorded, row
   present, lease lapses on schedule.
6. After the deadline, a second consumer **can** claim the envelope once the
   lease lapses — the recovery this exists to provide.
7. The deadline does not fire for a turn shorter than it, at a lease short enough
   that renewal ticks many times — proves the deadline is independent of renewal
   cadence.

## Open question for review

**Should the deadline be enabled by default, or opt-in?**

Default-on bounds a failure mode nobody has reported. Opt-in leaves the 8-day
suppression as the shipped behaviour and means the bound exists only where
someone knew to configure it — which is the population least likely to need it.

My view: **default-on with a deadline large enough that firing is itself a
signal**, because a silent 8-day suppression is worse than an audited duplicate
in a process already known to be broken. I hold this at moderate confidence and
would take the opposite call if the duplicate risk is judged higher than I have
weighted it.
