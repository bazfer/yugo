# Drop dispositions (issue #12)

Both adapters still use **core NATS**, not JetStream. Dispositions and notices
are diagnostics, not ack/nak/term, guaranteed receipt, or an automatic retry
mechanism. Callers decide whether and when to retry.

- TypeScript's independent `DropReason` union and
  `Record<DropReason, Disposition>` reject missing classifications at compile
  time. Validators and baton errors return that union; drop audits go through
  `recordDrop`. The informational audit method cannot accept `dir: 'drop'`.
- Python uses `DropReason` and `Disposition` enums and an independent dictionary;
  its import-time equality assertion rejects missing/extra classifications.
  Validators and existing `REJECT_*` constants use enum members. There is no
  fallback disposition in either implementation.
- Each drop audit gains `disposition`; counters count **audit drop events**, not
  unique envelope IDs. Existing informational audits, including the three
  `fleet_manifest_*` events and the outgoing payload-truncation archive, keep
  their previous lanes. The latter's reason is classified as requested, but
  recording its archive does not itself increment drop counts.

## Sender notices

A resolvable sender is a normalizable, allowlisted peer other than this bot.
A sender is still an **unauthenticated claim**; this does not introduce identity
verification. Malformed JSON has no recoverable envelope/sender and only audits.

Notices have kind `drop_notice` and payload fields `envelope_id`, `reason`,
`disposition`, `subject`. Their raw kind is checked **before validation** so
invalid notices cannot create a notice loop. Duplicates never notify. Notice
publication failures only log; they cannot recurse. Correlation uses
`in_reply_to` when the original ID is a string. Original inbound context is
retained across claim settlement and outbound publication failures.

Multiple cleanup drop audits during one delivery must not multiply notices:
TypeScript tracks decoded objects weakly; Python coalesces outstanding notice
publications for the same context. Neither mechanism suppresses diagnosis of a
later separately decoded redelivery. Python owns its asynchronous notice tasks
and drains them before tearing down the connection.

### #10 / FB-3 ordering

Publish on **`fleet.<sender>.request`**, the current and surviving publisher
ingress. Do **not** publish on `.result` (Contract removes it), or directly to
`.inbox` (the coordinator's delivery lane). #10 revokes `.request` **subscribe**,
not publisher ingress; the relay forwards ingress to `.inbox` after the flip.
This change requires no new ordering with #10 and adds no subscriptions,
durable consumers, or delivery policy. Fleet-wide reply-correlation gates in
SPEC §6.2.1 continue to apply; this does not authorize bypassing them.

## Status output and host integration

`FleetBus.busStatus()` (TS) and `FleetBus.bus_status()` (Python) return
`{drop_counts: {terminal, transient, throttled, duplicate}}`, all initialized to
zero and copied for callers. Both heartbeat payloads include that same object.

**Repository boundary:** this checkout has no `bus_status` command/router and
no host rate-limit counter renderer. Hosts should merge this snapshot beside
that renderer's existing rate-limit counters. The adapter API and heartbeat
output are implemented here; wiring a sibling host's command is outside this
repository, not silently claimed as implemented.

## Additional reasons on current main

The issue comment's list predates several codes present on the pulled main.
They are classified explicitly rather than covered by a default:

- Terminal: TS invalid origin/owner and invalid baton bot/handoff recipient or
  exhausted baton hops; Python invalid root/origin/owner/hops, rejected BUS tag,
  exhausted baton hops, and intentional auto-reply/warning suppressions. These
  suppressions retain their existing drop audits; they report that no further
  output is owed/permitted, not that already-run effects were cancelled.
- Transient: TS generic baton derivation failure, reply undelivered, pending
  claim deadline release; Python publish failure.

All classifications explicitly listed in the issue comment are preserved.
The three manifest event names are absent from both classification maps.

## Verification

```sh
bun test src/fleet-bus.test.ts src/drop-disposition.test.ts
node_modules/.bin/tsc --noEmit
FLEET_BUS_ENABLED=0 python -m pytest yugo/test/test_drop_disposition.py -q
python test/gates/drop-disposition.py
```

The mutation gate uses isolated copies and requires green unmodified controls.
It then requires **red** results for deleting a map entry, adding an
unclassified union/enum member, removing the notice loop guard, notifying a
duplicate, silencing a transient notice, misclassifying a manifest event, and
silencing each of the three manifest audit events. Wrong expected payloads in
both languages are additional positive controls proving assertion failures are
detected. Tests exercise real inbound validation, actual injection failures,
original-sender propagation on publish failure, and cleanup coalescing as well
as the focused record-drop helpers. No broker is needed for these controls.
