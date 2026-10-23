# Subscription admission health (#89)

Core NATS has no positive SUB acknowledgement. A returned handle is not proof
of admission: **nats-py 2.15.0 keeps refused subscriptions in its local `_subs`
registry**, whereas nats.js closes the refused handle. Inspecting a handle or
connection alone therefore recreates the bug.

## Admission fence and refusal tracking

Both adapters install error observation before subscribing, keep every intended
subject's returned handle, and check the intended set **after a PING/PONG
protocol fence**. This requires no incoming application message, probe publish,
additional subscription, JetStream, durable consumer, or delivery policy.

- Python recognizes the broker's subscription permission errors in `error_cb`
  and remembers the exact intended subject. It combines that evidence with
  handle subject/closed state and registry identity at verification time.
- **Python ordering detail:** the pinned library writes PING directly, while
  SUB commands can still be queued. `_flush_pending(force_flush=True)` must
  precede `flush()`; otherwise PONG can acknowledge the ping *before* the SUB
  was sent. The private APIs (`_subs`, `_id`, `_closed`, `_flush_pending`) are
  deliberately version-bound to the existing nats-py 2.15.0 pin and exercised
  against a real broker. Registry membership alone is explicitly tested as an
  insufficient check. No fallback for an incompatible client shape is added.
- TypeScript watches structured `permissionContext` events and the subscription
  iterators, then checks every intended handle's subject, closed state, and
  presence in the live subscription set after `nc.flush()`. The status watcher
  is registered before the first SUB. A refused handle cannot disappear from
  the intended inventory just because its live iterator ended.

Connection/reconnection flush failures cannot reach the healthy transition.
Both ports reverify after library-managed replay on reconnect; neither manually
resubscribes. Ordinary network reconnects pause heartbeat publication until
verification. A replay refusal is covered by real restart/authz-change tests.

This is admission evidence, **not** a guarantee of future message delivery or
an assertion that wildcard permissions cover every possible message.

## Terminal is not a reconnect retry

An intended refusal sets sticky `state: unhealthy`, `healthy: false`, stops
heartbeat publication immediately, and writes one classified drop per refused
subject with `terminal` disposition using #12's existing helper and counters:

- `claude_discord_adapter_subscribe_refused`
- `yugo_subscribe_refused`

There is no envelope/sender to notify. Repeated error/callback/fence observation
of the same refusal does not double-count. The connection and **granted** lanes
remain usable for diagnosis/other traffic; transport-connected does not mean
healthy. An unhealthy connection does not get a successful `connected` audit.
No heartbeat is published to claim liveness while deaf. Refusal remains sticky
across transport reconnects and is not retried by the adapter supervisor after
closure. An explicit fresh connection/operator restart begins a new attempt.

`busStatus()` / `bus_status()` snapshots expose `state`, `healthy`, copied/sorted
`subjects_refused`, and existing `drop_counts`; healthy heartbeat payloads carry
the same fields. This repository still has no sibling host's `bus_status` UI
router, as documented for #12; these are the adapter/heartbeat output boundaries.
Publish-only TS mode has an intentionally empty required subscription set.

## Scope and doc correction

The intended subject sets are unchanged. **No `.inbox` consumption flip** is
included. The Python `subjects` docstring now correctly says live authz grants
each bot its own inbox subscribe, while retaining the warning about a refused
subscription leaving a transport-connected client deaf without these checks.

## Caused tests, including the remove-the-check control

Both new test modules use real NATS 2.10.22 with per-user allow/deny configuration:

1. Refuse request SUB: connection remains up, adapter becomes unhealthy, zero
   heartbeats, exact subject/reason/terminal audit (once).
2. Refuse one of several SUBs: exact copied refusal list; another granted lane
   still delivers a real envelope.
3. All granted: healthy, empty refusal list, repeated healthy heartbeats.
4. Raw-client positive control: broker actually emits a permission failure while
   retaining the connection (and the Python local handle).
5. Restart broker with changed authz: a replay refusal stops an already-running
   heartbeat and remains terminal. A separate TS control throws during the replay
fence and proves status observation continues through a later successful replay.

The three initial health tests in each language failed against main; the raw
broker control passed. `python test/gates/subscribe-health.py` then runs isolated
source copies and:

- requires the unmodified five-case suites green;
- removes the terminal refusal/health check (leaving handle verification intact) and requires the safety
  test red;
- demands the **legacy wrong outcome** (healthy + connected + heartbeating
  despite the real refusal) and requires that witness **green with the checks
  removed**;
- restores the checks and requires the deliberately wrong witness red.

This proves the check changes observed broker behavior, not just an assertion
or a mocked error. The TS CI job now installs the broker instead of silently
skipping these tests. TS's unrelated native-clock seam is injected exactly as
in existing behavioral tests; these are not native-ABI qualification claims.

## Review 5630640882: repair the heartbeat-stop proof, not production

The original replay tests had an invalid observation window: they measured
silence before their peer's default ~2-second reconnect completed. Reproduced
this exactly: the heartbeat-only mutant passed **all six TS tests** and the
Python replay test. The broad refusal-method mutant was insufficient evidence
for physical heartbeat cessation because state/audit assertions caught it first.

**Production adapter files are unchanged by this repair.** Both replay tests now:

1. Observe the peer's actual reconnect event (with a failure deadline, not a
   guessed recovery sleep).
2. Publish a fresh unique `observer_witness` envelope and require actual receipt
   on the **same existing `fleet.yugo.status` subscription** that counts beats.
   The valid test envelope is not counted as a heartbeat.
3. Only then snapshot the count and observe the existing multi-period silence
   window. Require another freshly received nonce to bookend that window before
   checking the count, so a blinded observer cannot manufacture a green result.

These are test-only traffic/witnesses; production admission still depends on
protocol fences, errors, and subscription handles, not application traffic.
The TS test deadline accommodates default observer reconnect plus a bounded
witness deadline; no production reconnect intervals or stop logic change.
Python's initial-refusal test body is unchanged (verified by AST comparison).

The existing broad controls remain. New gate controls leave unhealthy/refusal/
terminal-audit reporting intact and remove **only** physical heartbeat stopping:

- TS: remove timer cancellation inside `refuseSubject` and the publication state
  guard. Initial refusal reporting stays green, but replay must fail specifically
  with `heartbeat continued after observer recovery`, including actual counts.
- Python: remove only the publication state guard. Both replay and the unchanged
  initial-refusal test must fail on actual forbidden heartbeat observations.
- Discard only witness delivery in each test harness, retaining reconnect events
  and correct production code: replay must fail specifically for missing fresh
  delivery. Reconnect alone is therefore insufficient to accept silence.

The gate checks the specific runtime assertion/error text for these narrow
mutants, not merely any non-zero exit. Unmodified suites pass with the same
witness requirements; no silence/state/audit expectation is relaxed.
