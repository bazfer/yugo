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
  exhausted baton hops; Python invalid root/origin/owner/hops and rejected BUS
  tags.
- Transient: TS generic baton derivation failure, reply undelivered, pending
  claim deadline release; Python publish failure.

All classifications explicitly listed in the issue comment are preserved.
The three manifest event names are absent from both classification maps.

### Corrected success boundary (review 5629515112)

`yugo_autoreply_suppressed`, `yugo_baton_warning_suppressed`, and
`yugo_baton_hops_exceeded` are **not drops**. The corrected brief excludes
successful deliberate non-replies and the exhausted-baton no-publication
boundary. They use a separate `SuppressionReason` enum and informational `dir: in, event: suppression` audit
helper (no new audit lane), keeping reason/cause/correlation while never classifying a failure,
incrementing drop counters, or publishing a notice. Their names are removed
from **both** drop maps/unions. This is not a fifth disposition.

Real-broker safety assertions still require zero publications at the ceiling,
zero empty/all-tags auto-answers, no reply-to-reply, and exactly `[yugo, vec]`
for the two-bot exchange. Only genuine output failures gain failure notices.

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

### Review repair: justification for changed test expectations

Reproduced all **12** reported broker-test failures before editing. Moving the
five deliberate non-publication sites to informational audits fixed the five
regression cases without relaxing their wire/turn assertions. Only then were
the seven genuine-failure expectations updated:

- `test_a_tag_we_cannot_honour_publishes_nothing_and_says_why` (four parameters):
  invalid recipient/unparseable tag is failed output, so the sender now gets
  exactly one terminal notice **plus** the existing stripped-text answer.
  Still zero publication to stranger/third-party subjects; exact notice reason,
  original envelope ID and subject are checked for each parameter.
- `test_a_broken_tag_costs_one_tag_not_the_turn_or_the_subscription`: a malformed
  tag genuinely failed. Allow exactly its one terminal notice, retain both
  original text answers and their correlations, forbid third-party delivery.
  Wait for two messages for the first turn and three total so the notice can
  never be mistaken for the second turn's recovery answer.
- `test_a_fault_in_the_dispatcher_does_not_wedge_the_subscription`: the first
  dispatch really failed locally. Require its one transient notice, and still
  require exactly the subsequent successful text answer. Wait for both.
- `test_an_envelope_the_validator_refuses_never_reaches_the_wire`: oversized
  output must still never leave. Only its one bounded terminal notice may
  publish; require its exact payload and a size below the envelope limit.

Other expectation changes concern **audit classification only**, not accepting
new wire traffic: the ceiling and two warning-suppression rows in
`test_fleet_bus_request_audit.py` now require informational `in` instead of `drop` (with a `suppression` event).
Baton/reply/all-tags/ping-pong tests read these records through `suppressions`,
which additionally requires no disposition; genuine failure assertions through
`drops` now require a drop lane **and** a disposition. Original zero-publication
and exactly-two-turn assertions stay unchanged; successful all-tags, ceiling,
and two-bot cases additionally require zero drop counters.

Five focused boundary cases (reply guard, all-tags success, exhausted hops,
missing warning origin, self warning origin) assert zero notice publications,
zero counters, and informational-only audit metadata. A new mutation redirects
the informational helper into a classified failure notice and must turn these
tests red. No fifth disposition is introduced.
