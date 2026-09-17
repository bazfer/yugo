# yugo

**Multi-provider agent harness — Discord bot container backed by LiteLLM.**

One `MODEL` env var. Any LLM: xAI, OpenAI, Gemini, Anthropic, self-hosted via Ollama/vLLM. Same Discord bot process, same persona, same memory system — swap the model at will.

## Status

**Current: v0.6b.** The source of truth is `YUGO_VERSION` in `version.py` — the same string the `status_heartbeat` payload carries. SPEC §3 holds the roadmap, §15 the slice breakdown.

**v0.1 (shipped) — LLM in Discord.** Discord bridge + LiteLLM completion + persona. Each turn is one model call, one reply.

**v0.2a (shipped) — rolling per-thread history.** In-memory per-thread buffer bounded by `HISTORY_MAX_TURNS`; a turn is one user message plus one assistant reply. No persistence and no compaction — summary compaction is v0.2b.

**v0.3a (shipped) — fleet-bus adapter, connection slice.** NATS connect + subscribe + `status_heartbeat` + audit log, off by default. Nothing JetStream: that arrives with the fleet-wide FB-1/FB-3 migration.

**v0.3b (shipped) — session injection.** An envelope arriving on `fleet.<BOT_NAME>.request` **and addressed to this bot** (`to` is normalised and compared against `BOT_NAME`; a mismatch — including a missing `to` — is dropped and audited as `recipient_mismatch`) now drives an LLM turn instead of being dropped. It reaches the model as the fleet-wide `<channel source="fleet-bus" authenticated="false" ...>` injection frame, with only the JSON-encoded payload inside `<payload>`. The turn is **bus-only** (SPEC §8): it never speaks into Discord, and it reads its own per-peer history namespace, so a human's Discord conversation is never context for a bus reply and vice versa. The other three subscribed subjects are still audited and dropped — `fleet.broadcast.>` because SPEC §7.1 forbids ever session-injecting a broadcast, `.status` because it is presence (and our own heartbeat loops back on it), `.result` because replies now travel as `.request` envelopes with `in_reply_to`.

**v0.3c (shipped) — the turn answers.** A bus-triggered turn now publishes. Two separate things happen to its reply text, and they are described in full under [fleet-bus outbound](#fleet-bus-outbound-v03c) below: the answer goes back to the sender automatically, and any `<BUS to="…">…</BUS>` tag in it publishes to the peer it names.

**v0.3d (shipped) — the adapter joins the baton protocol.** `root_id`, `origin`, `owner` and `hops` are rendered on the injection frame and carried onto everything a bus turn publishes, `hops` is **incremented** on the way out, a chain that reaches hop 8 makes this bot warn `origin`, and one that arrives at hop 16 is refused before it can cost a model turn. Both v0.3c loop guards stay — see [fleet-bus baton participation](#fleet-bus-baton-participation-v03d). No baton store: lifecycle belongs to the originator.

**v0.4a (shipped) — the tool-call loop, without the tools.** A turn is no longer one completion: the model may ask for tool calls, the harness runs them, appends one `role: tool` message each, and re-enters the model with them. The only registered tool is `loop_probe`, which echoes its argument and does nothing else.

**v0.4b (shipped) — grants, and the first real tools.** Tool grants read from `$YUGO_TOOLS_FILE` and v0.4a's `## Tools` persona grammar is deleted rather than left as a second live path — a grant is capability, not persona prose. `read_file`, `write_file` and `list_dir` follow, confined to `$YUGO_WORKSPACE_PATH` by the open itself per SPEC §9.2 rather than by a check that precedes it.

**v0.4c (shipped) — bounded public HTTP GET.** `$YUGO_TOOLS_FILE` remains the sole grant source. `http_get` pins validated global-unicast addresses, carries one deadline across redirects, bounds wire and decoded bytes independently, and attaches exact-origin credentials from `$YUGO_HTTP_CREDENTIALS_FILE` without exposing their literal values.

**v0.4d (shipped) — the tool audit log.** Every tool call is recorded at `$YUGO_TOOL_AUDIT_PATH` with arguments, a result summary and duration. The path sits OUTSIDE `write_file`'s allowed scope, so the agent cannot edit its own audit trail.

**v0.4e (shipped) — the `SANDBOX_MODE` flag, and v0.4 complete.** **Upgrade note: a deployment that already sets `SANDBOX_MODE=full` will stop starting.** SPEC §9 previously told operators to flip that value to enable the expensive guards, which the code has never implemented, so any installation following that instruction has been running reduced under a full label. The abort is the point — unset the variable to run reduced deliberately. `reduced` is the default and the only implemented mode. `full` is a **startup abort**, not a silent downgrade: an operator sets it at the moment the threat model changes, and starting in reduced mode under a full label would be a lie nothing later contradicts. Unrecognised values abort for the same reason. See SPEC §9 full mode.

**v0.6a (shipped) — coordinator transport skeleton.** Dedicated NATS identity, single-instance lock, durable pull-and-forward relay, and core-NATS heartbeat. v0.6b-1 wires these pieces into the standalone coordinator process; the mandatory sqlite state store remains v0.6b-2.

**v0.6b (shipped) — standalone coordinator state.** Startup now versions the mandatory SQLite file, migrates 6b-1 deployments, and reconciles a two-state durable-consumer marker before relay traffic starts. Durable deletion, foreign ownership, generation replacement, and policy/filter drift abort with the explicit `accept-durable-reset` recovery command; HITL holds, de-duplication, and audit projection remain outside this slice.

For an intentionally replaced durable, stop the coordinator and run
`coordinator_main.py accept-durable-reset` with the coordinator environment to
re-stamp the live generation. If the durable is absent, it refuses until the
operator reruns it as `coordinator_main.py accept-durable-reset
--acknowledge-traffic-gap`. The acknowledged command clears the stale marker
and explicitly logs that the operator accepts the traffic gap: the next start
creates a `DeliverPolicy.NEW` durable, so traffic published while it was absent
is not replayed. Both success paths log the operator identity, UTC timestamp,
and acknowledged risk. The command takes the same
exclusive state lock, so it refuses while a coordinator is running. Do not
recover by deleting the state file; later slices store held work there.

## Why yugo, why not codex-container

Codex CLI's tool schema is OpenAI-native and doesn't work against xAI/Grok, Gemini, or other providers we tested — see [codex CLI + Grok compat gap notes](./docs/GROK-COMPAT-GAP.md) for details. Yugo replaces the codex-app-server dependency with LiteLLM, which handles provider quirks itself.

Codex-container is still fine when you want to stay on OpenAI. Yugo starts to matter when you want provider diversity — cost mixing, independent-judgment reviewers, provider-outage resilience.

## Quick start

Requires: Docker and Docker Compose.

```bash
git clone git@github.com:bazfer/yugo2.git
cd yugo2/yugo
cp .env.example .env                  # fill in DISCORD_BOT_TOKEN, MODEL, provider API key
cp compose.example.yml compose.yml    # Compose ignores *.example.yml; the copy is gitignored
cp IDENTITY.md persona.md             # then write your bot's persona into it
docker compose up -d --build
docker compose logs -f
```

Both copies are gitignored on purpose: `.env` holds credentials, `compose.yml` holds per-host mount paths. Skipping the `persona.md` copy is not cosmetic — Compose would create a *directory* at that bind mount, and a directory is the one unreadable-persona case `load_persona` does not fall back on.

The container is named after `BOT_NAME` (default `yugo`), so a second bot is a second clone directory with a different `BOT_NAME` — no edit to `compose.yml`.

## Config surface (`.env`)

| Var | Required | Example |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | yes | `MTA5...` |
| `CHANNEL_ID` | yes | `1234567890` |
| `MODEL` | yes | `xai/grok-code-fast-1`, `openai/gpt-5`, `anthropic/claude-sonnet-5`, `ollama/qwen2.5-coder:32b`, etc. — LiteLLM model string |
| `XAI_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / etc. | one, matching MODEL | provider-specific |
| `GUILD_ID` | optional | limits to one guild |
| `TIMEZONE` | optional | `America/Mexico_City` (default `UTC`) |
| `RESPONSE_TIMEOUT` | optional | seconds before "no response" fallback (default 120) |
| `PERSONA_FILE` | optional | path to persona.md inside container (default `/root/persona.md`) |
| `BOT_NAME` | optional | names the container in `compose.yml` (default `yugo`). Not used by `bot.py` while fleet-bus is off; becomes required and identity-bearing when the fleet-bus adapter is on — see below |
| `HISTORY_MAX_TURNS` | optional | rolling per-thread buffer size for Discord turns (default 10) |
| `BUS_HISTORY_MAX_TURNS` | optional | the same bound for fleet-bus turns, which keep a separate per-peer history namespace (default 10) |
| `TOOL_MAX_ROUNDS` | optional | how many model → tools → model rounds one turn may take (default 5) |
| `YUGO_TOOL_AUDIT_PATH` | optional | tool-call audit JSONL (default `/var/lib/yugo/tool-audit.jsonl`) |
| `YUGO_TOOLS_FILE` | optional (v0.4b+) | tool declarations, mounted `:ro` (default `/etc/yugo/tools.yaml`). Not IDENTITY.md. Grammar + fail-closed rules in SPEC §9.1; absent or empty means zero tools, everything else malformed is fatal at startup |
| `YUGO_WORKSPACE_PATH` | optional (v0.4b+) | filesystem scope for `read_file` / `write_file` (default `/var/lib/yugo/workspace/<BOT_NAME>`). Confinement is `openat2` + `RESOLVE_BENEATH` per SPEC §9.2; the tools fail closed if that syscall is unavailable |
| `YUGO_HTTP_CREDENTIALS_FILE` | optional (v0.4c+) | exact-origin credentials for `http_get` (default `/etc/yugo/http-credentials.yaml`). Must be mode `0600`/`0400` or tighter and outside the workspace; grammar in SPEC §9.4.1 |

### Tools (v0.4b, v0.4c)

Off unless declared. Yugo does not auto-discover tools (SPEC §9): a bot may call exactly what it is granted, and nothing else. No declarations means the `tools=` parameter is not sent to the provider at all — the turn is the same single completion v0.3 made. A declared name this build does not ship is **fatal at startup**, naming the tool: a bot that boots without a capability it claims is a worse outcome than one that refuses to boot.

Declarations live only in `$YUGO_TOOLS_FILE` (default `/etc/yugo/tools.yaml`, mounted `:ro`). Persona headings and bullets declare nothing. An absent or empty file grants zero tools.

The reason, since it is the kind of decision that gets re-litigated: a declaration is a **capability grant**, and the persona is free-form prose edited by whoever is tuning a bot's voice — one file meant one permission to change both. It is also prepended verbatim as the system prompt *and* parsed for grants, so the model reads its own grant list as prose and "live block or example?" comes down to fence rules a persona author has to hold in their head. v0.4a shipped two defects of exactly that kind, where documentation was executable. A separate file is bind-mounted `:ro` the way `persona.md` already is, which makes the grant tamper-proof at the kernel instead of at a path assertion — a property the audit log cannot have, because it must stay writable.

The registry contains `read_file`, `write_file`, `list_dir`, `http_get`, and the scaffold-only `loop_probe`. File tools accept only workspace-relative paths and fail closed when `openat2` confinement is unavailable. `http_get` reaches public HTTP(S) only: it connects to the address it validated rather than re-resolving the hostname, spends one deadline across every redirect hop, bounds wire and decoded bytes independently, and attaches an exact-origin credential that it drops the moment a redirect leaves that origin. Bounds and exact semantics are in SPEC §9.3.

**The loop.** Each round appends the `assistant` message carrying `tool_calls` plus one `role: tool` message per call, then re-enters the model. `TOOL_MAX_ROUNDS` (default 5) bounds the rounds; `RESPONSE_TIMEOUT` bounds the turn as a whole. Every timed await inside the turn — each completion **and each tool handler** — gets the budget remaining at that moment, recomputed per call, so a slow tool eats the same budget a slow provider does and neither can stretch a bus turn into a stalled subscription callback. A handler that outruns it is cut off, audited, and answered to the model as a tool error.

**Tool rounds do not persist.** History records the user text and the final reply, as before. The rounds live in a per-turn working copy — `history.record_turn` evicts from index 0 in pairs, so a stored tool round could lose its `assistant`+`tool_calls` message while the matching `role: tool` message survived, and an orphan tool result is a provider 400 on the next turn.

**Audit.** Every call writes one JSONL line to `$YUGO_TOOL_AUDIT_PATH` (default `/var/lib/yugo/tool-audit.jsonl`, created 0600) with the tool name, the arguments, a truncated result summary, the duration in ms, and the history namespace that drove it — so a call made on behalf of another bot's unauthenticated bus payload is distinguishable from a human's Discord turn.

Every path is audited, including the ones that never reach a handler (unknown tool, undecodable arguments, an argument shape the tool's schema refuses, no turn budget left) and including cancellation — the record is written from a `finally`, because `CancelledError` is a `BaseException` and a bot shut down mid-call would otherwise leave it unrecorded.

Arguments are model-authored and are recorded whether or not the handler accepted them, so they are bounded: the line carries `args` when the encoded object fits `AUDIT_ARGS_MAX_CHARS`, `args_truncated` + `args_truncated_flag` when it does not, or `args_raw` when decoding never produced an object — with `args_len` giving the true size either way. A tool's declared `additionalProperties: false` and `required` are enforced before the handler runs, so an undeclared key cannot smuggle megabytes into the trail.

SPEC §9 requires this path to sit outside `write_file`'s eventual scope; keep the mount clear of the v0.4b workspace.

### fleet-bus (v0.3a, v0.3b, v0.3c, v0.3d)

#### Coordinator broker role (v0.6a)

The example Compose file keeps the coordinator opt-in so the existing bot-only
quick start remains self-contained. Start it with
`docker compose --profile coordinator up -d coordinator` only after creating
the external `fleet-bus-net` network, provisioning the broker-side
`coordinator` user, writing `./coordinator-token`, and placing
`./coordinator-state` on a local filesystem (ext4, xfs, or btrfs). The default
`docker compose up` starts only the bot.

`config/nats-coordinator-authz.conf` is the broker-side role definition to
merge into the JetStream-enabled fleet `nats.conf`. Replace its example
passwords through the deployment secret mechanism; the coordinator reads its
credential from `FLEET_BUS_TOKEN_FILE` and requires
`FLEET_BUS_USER=coordinator`.

The explicit non-coordinator deny on `fleet.*.inbox` is a security boundary,
not a documentation hint. Apply it to every fleet-bot user while preserving
only that user's own migration-window subscribe grants; replace every
`fleet.fleet-bot.*` example with that user's exact identity. A wildcard
`fleet.>` subscribe grant lets a bot bypass interposition by consuming a peer's
request directly and is forbidden. Each bot also requires its own account as
shown: a service import moves its request into the `FLEET` account while
keeping the publisher-selected PubAck reply in the bot's isolated account. If
bots share `FLEET`, a crafted reply subject can reflect a broker-generated ack
into a peer's durable inbox despite the direct-publish deny. Only `coordinator`
may publish into the `FLEET` inbox; console is subscribe-only. The coordinator
gets only the JetStream API subjects needed by its pull consumer—not stream
purge/delete—but this v0.6a-2 slice does not create that consumer or start the
forward loop.

#### Coordinator relay (v0.6a)

`CoordinatorRelay` binds the sole `yugo-coordinator-request` durable pull
consumer on `FLEET_REQUEST` with `DeliverPolicy.NEW`, then republishes each
delivery byte-for-byte from `fleet.<recipient>.request` to
`fleet.<recipient>.inbox`. It acknowledges the request only after the inbox
stream returns its PubAck. A transient publish failure is delayed-nak'd for
redelivery.

Both streams use limits retention. An unacked request still expires at the
broker's seven-day `max_age`; ack state does not extend retention. The required
relationship is therefore `7d >> 15min` held timeout—never an assumption that
a held envelope survives beyond `max_age`. Policy holds, the full disposition
map, and durable envelope-id de-duplication land in their later slices.

#### Coordinator liveness (v0.6a)

The coordinator emits a `status_heartbeat` envelope every
`COORDINATOR_HEARTBEAT_MS` (default `5000`) on
`fleet.coordinator.status`. This is a plain core-NATS publish: status and
broadcast subjects must not be added to a JetStream stream.

`TapHeartbeatMonitor` consumes that exact status subject and emits one alert
per outage to `COORDINATOR_CHANNEL_ID` plus the optional paging channel once
silence is strictly greater than three heartbeat intervals. A valid heartbeat
clears the incident. The monitor deliberately has no forwarded-envelope
counter: the relay is at-least-once, and a duplicate after the inbox PubAck /
request-ack crash window is not an anomaly.

Off unless `FLEET_BUS_ENABLED=1`. While off, nats-py is never even imported.

| Var | Required when enabled | Example |
| --- | --- | --- |
| `FLEET_BUS_ENABLED` | — | `1` to turn the adapter on (default `0`) |
| `BOT_NAME` | yes | `yugo` — canonical fleet identity, `^[a-z0-9_-]+$`, must be listed in the manifest's `bot_names` |
| `FLEET_BUS_TOKEN_FILE` | yes | `/root/.claude/fleet-bus-token` — file holding the NATS password |
| `FLEET_BUS_MANIFEST_PATH` | yes | `/vault/infra/fleet-manifest.yaml` — the `from`-claim allowlist (default shown) |
| `FLEET_BUS_URL` | optional | `nats://nats:4222` (default) |
| `FLEET_BUS_USER` | optional | defaults to `BOT_NAME`; a different value is a startup abort |
| `FLEET_BUS_AUDIT_LOG` | optional | `/root/.claude/fleet-bus-log.jsonl` (default), written `0600` |

Failure split:

- **Config fault is fatal at startup** — unreadable/empty `FLEET_BUS_TOKEN_FILE`, missing/empty/unparseable manifest, bad `BOT_NAME`, or a `BOT_NAME` the manifest does not list (the allowlist gates our own `from` claim too, so an unlisted bot drops its own heartbeat). The bot refuses to run and names the variable, same as a missing persona (SPEC §10).
- **Unreachable broker is not** — the bot serves Discord normally and the supervisor retries forever, auditing each attempt. `max_reconnect_attempts=-1`, so an outage of any length still recovers without a process restart.
- **Wrong credentials are not fatal either** — same retry-forever path, but each rejection is audited as `auth_rejected` rather than a generic `error`, so an authz problem is greppable instead of hiding in reconnect noise.

Subjects subscribed: `fleet.<BOT_NAME>.request`, `.result`, `.status`, `fleet.broadcast.>`. **Not** `.inbox` — see SPEC §15 erratum E-1.

Of those four, only `fleet.<BOT_NAME>.request` can reach the model (v0.3b), and only for envelopes whose `to` matches this bot. Envelopes on the other three are validated and audited, then dropped.

### fleet-bus outbound (v0.3c)

A turn driven by an inbound envelope produces one piece of text. Two things happen to it.

**1. The sender gets an answer, automatically.** No tag, no tool call, nothing for the model to remember: the text left after tags are stripped is published to `fleet.<sender>.request` as an ordinary envelope with `in_reply_to` set to the inbound envelope's id. `.request` and not `.result` — SPEC §7 removed `.result` as a subject class, and it is also the only lane any adapter in the fleet actually processes.

**The one case that is NOT auto-answered: an inbound envelope that itself carries `in_reply_to`.** Two bots that both auto-reply are an infinite exchange — A asks, B answers, A answers the answer — so one question earns one automatic answer, and answering an answer is a decision the model has to make with a tag. (v0.3d's hop ceiling is a second, fleet-wide backstop and does **not** replace this one — see below.)

**2. `<BUS>` tags address a third party.**

```
<BUS to="vec">please rebase PR 12</BUS>
```

- `to` is required, and must name a bot listed in the manifest's `bot_names`. The tag publishes the body to `fleet.<to>.request` as a NEW envelope — fresh id, no `in_reply_to`, `kind: text_message`, `payload: {"text": "<body>"}`. It is not a reply, so the recipient's own auto-reply still fires.
- Attribute values must be quoted (`to="vec"` or `to='vec'`), with no spaces around the `=`. Attributes other than `to` are parsed and ignored.
- The tag is for OTHER bots, and that is enforced: a tag addressing THIS bot is refused. It would publish onto our own `fleet.<BOT_NAME>.request` as a fresh envelope with no `in_reply_to` — the exact field the loop guard reads — so the bot would drive itself, one model turn per hop. Answering the sender needs no tag.
- Whatever the tag's fate, its markup is stripped from the text before the answer goes out — the sender never receives raw tag syntax.

A tag that names nobody, names a bot the manifest does not list, or does not parse **publishes nothing and does not interrupt the turn**. The sender still gets their answer; the tag becomes an audit line:

| `reason` | when |
| --- | --- |
| `invalid_to` | the tag's `to` is missing, is not a canonical bot name, or is not in the manifest. A fleet-bus SPEC §5 code — same one the shared taxonomy uses for a bad `to` |
| `yugo_bus_tag_rejected` | the tag did not parse (`cause`: `malformed`, `unclosed`), had an empty body (`cause`: `empty_body`), or addressed this bot (`cause`: `self_addressed`) |
| `yugo_publish_failed` | the envelope never left — the wire refused it (no connection, publish raised), or it could not be built at all (a `baton` argument carrying keys outside `root_id`/`origin`/`owner`/`hops`) |
| `yugo_autoreply_suppressed` | no automatic answer was sent (`cause`: `in_reply_to` for the loop guard, `empty_reply` for a turn that was nothing but tags) |

The `yugo_`-prefixed reasons are adapter-local by construction: fleet-bus SPEC §8 requires a drop reason to be "a §5 code or an implementation-specific code prefixed with the harness name", so they cannot collide with a future addition to the shared taxonomy. Everything an envelope can fail on — `envelope_too_large`, `payload_not_serializable` — keeps its §5 spelling, because outbound envelopes go through the same validator as inbound ones.

### fleet-bus baton participation (v0.3d)

Four optional envelope fields — `root_id`, `origin`, `owner`, `hops` — carry a piece of work across the bots that touch it, so a completion can get home to whoever started it rather than to whoever handed it over last. yugo **participates** in the protocol; it does not merely relay it.

- **Inbound.** All four are rendered on the injection frame, after the seven attributes the fleet-bus frame contract fixes, and only when the envelope actually carries them — an envelope outside a chain produces exactly the frame v0.3b produced. Without this the model has to read the audit log to know which baton it is holding.
- **Outbound.** `root_id`, `origin` and `owner` are copied unchanged onto both outbound lanes (the automatic answer and every `<BUS>` tag). `hops` is **incremented**. A bot that forwarded a baton without incrementing would make the count too low for every bot downstream of it, and the chain would look shorter than it is to everyone including yugo's own coordinator, whose HITL policy tightens on hop count (SPEC §7A.2/§7A.3).
- **`owner` is never set to this bot.** Ownership changes only on an explicit `baton.handoff`, and yugo does not publish one. Answering a question is not taking the baton.
- **Baton fields cannot be set from `<BUS>` tag attributes.** They come from the received envelope or not at all. A model that could write `hops="0"` could reset the ceiling on every pass.
- **yugo never mints a `root_id`.** That belongs to the originator. An envelope arriving with no baton field leaves with none.
- **Warn at 8.** A received envelope at hop 8 or above makes yugo publish a warning **to `origin`** — the baton spec is explicit that this is a message, not a log line ("a warning nobody reads is decoration"). It goes out as `text_message` with `in_reply_to` set, so a peer running the reply guard above does not answer it. Not sent when `origin` is this bot, and not sent when there is no `origin`; both are audited.
- **Reject at 16.** A received envelope at hop 16 or above is refused before anything else happens — no session turn, no warning, no answer.

Not implemented here, deliberately: the **600-second abandonment timer**. The baton spec puts baton lifecycle on the originator ("the originator tracks its own open batons… Do not make this the bus's job") and says the protocol is "not a work queue, no persistence". There is no baton store in this adapter.

A present-but-malformed baton field is a reject, not an ignore. The fields are additive and clause 1 says to ignore fields you do not understand — from v0.3d yugo understands all four, and each has a consequence: a `hops` of `"16"` that got ignored would walk past the ceiling forever, an `origin` that is not a bot name has nowhere to send a warning, and `root_id`/`owner` get republished under this bot's own `from`.

| `reason` | when |
| --- | --- |
| `yugo_invalid_root_id` | `root_id` present and not a non-empty string |
| `yugo_invalid_origin` | `origin` present and not already a canonical bot name (`^[a-z0-9_-]+$` after NFKC folding) |
| `yugo_invalid_owner` | `owner` present and not already a canonical bot name |
| `yugo_invalid_hops` | `hops` present and not a non-negative integer (a JSON `true` or `8.0` counts as malformed) |
| `yugo_baton_hops_exceeded` | the envelope is well-formed but arrived at hop 16 or beyond |
| `yugo_baton_warning_suppressed` | a hop-8 warning was not sent (`cause`: `no_origin`, `self_origin`) |

Values are required to be **already** canonical rather than folded into canonical form: `root_id` is "copied unchanged into every descendant", and a field this adapter rewrites in transit is not being copied unchanged.

## LICENSE

TBD — see repo owner.
