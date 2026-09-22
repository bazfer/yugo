import {
  connect as natsConnect,
  JSONCodec,
  type ConnectionOptions,
  type NatsConnection,
  type Msg,
  type Subscription,
} from 'nats'
import { randomBytes, randomUUID } from 'node:crypto'
import { appendFileSync, chmodSync, mkdirSync, readFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { homedir } from 'node:os'
import { Database } from 'bun:sqlite'
import { parse as parseYaml } from 'yaml'

export const DEFAULT_MAX_ENVELOPE_BYTES = 1_044_480
export const DEFAULT_INFLIGHT_LEDGER_CAP = 1000
export const DEFAULT_RECEIVE_LEDGER_CAP = 1000
export const DEFAULT_EVICTED_LEDGER_CAP = 1000
export const DEFAULT_SEEN_RESULT_LEDGER_CAP = 1000
export const DEFAULT_SEEN_REQUEST_LEDGER_CAP = 1000
export const DEFAULT_REQUEST_TIMEOUT_MS = 30_000
export const DEFAULT_DEDUP_TTL_MS = 8 * 24 * 60 * 60 * 1000
export const MIN_DEDUP_TTL_MS = 7 * 24 * 60 * 60 * 1000
export const DEFAULT_DEDUP_LEASE_MS = 60_000
const DEDUP_PRUNE_EVERY = 256
const DEDUP_PRUNE_LIMIT = 100
// A single bounded batch cannot keep up: at 100 deleted per 256 admitted the
// expired backlog grows ~156 rows per 256 arrivals. `prune` loops bounded
// batches until drained or this budget is spent, and the budget is
// deliberately larger than DEDUP_PRUNE_EVERY so a steady arrival stream loses
// ground on every sweep rather than gaining it.
export const DEDUP_PRUNE_BUDGET = 4 * DEDUP_PRUNE_EVERY
// Renewal cadence for a live owner, as a fraction of the lease.
export const DEDUP_LEASE_RENEW_RATIO = 0.4
export const DEFAULT_ATTR_MAX_LEN = 1024
export const DEFAULT_PAYLOAD_BODY_MAX_BYTES = 8192

export interface Envelope<P = unknown> {
  envelope_version: 1
  id: string
  from: string
  to?: string | null
  kind: string
  in_reply_to?: string
  ts: string
  payload: P
  root_id?: string
  origin?: string
  owner?: string
  hops?: number
}

export type FleetBusMode = 'primary' | 'publish-only'

export interface FleetBusConfig {
  botName: string
  url: string
  user: string
  password: string
  subscribeBroadcast?: boolean
  maxEnvelopeBytes?: number
  heartbeatIntervalMs?: number
  pluginVersion?: string
  logger?: (message: string) => void
  auditLogPath?: string
  injectIntoSession?: (event: FleetBusSessionEvent) => Promise<void>
  mode?: FleetBusMode
  inflightLedgerCap?: number
  receiveLedgerCap?: number
  evictedLedgerCap?: number
  seenResultLedgerCap?: number
  seenRequestLedgerCap?: number
  dedupStorePath?: string
  dedupTtlMs?: number
  dedupStore?: DurableEnvelopeDedupStore
  rateLimiters?: FleetBusRateLimiters
  supervisorSleepMs?: number
  /**
   * Injected NATS connect function for testing. Defaults to `nats.connect`.
   * Signature intentionally loose to match the module's exported type.
   */
  connectFn?: (options: ConnectionOptions) => Promise<NatsConnection>
}

export interface FleetBusSessionEvent {
  envelope: Envelope
  reqId: string
  /** Set on `.result` envelopes that did not match an outstanding request. */
  unsolicited?: boolean
  /** Set when this envelope is a late reply to a request whose waiter was already evicted. */
  lateReplyEnvId?: string
}

/** Durable source of truth for envelope-id deduplication.
 *
 * Eight days exceeds the broker's seven-day max_age by one day, so every
 * possible redelivery remains covered without retaining history forever.
 * INSERT OR IGNORE against the primary key makes concurrent claims
 * deterministic: exactly one caller inserts and every loser reads its reqId.
 */
export class DurableEnvelopeDedupStore {
  private readonly db: Database
  private claims = 0

  constructor(
    path: string,
    private readonly ttlMs = DEFAULT_DEDUP_TTL_MS,
    private readonly leaseMsValue = DEFAULT_DEDUP_LEASE_MS,
  ) {
    if (path !== ':memory:' && ttlMs < MIN_DEDUP_TTL_MS) {
      throw new RangeError('durable dedup TTL must be at least the 7-day stream max_age')
    }
    if (path !== ':memory:') mkdirSync(dirname(path), { recursive: true })
    this.db = new Database(path, { create: true })
    this.db.exec('PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000')
    this.db.exec(`CREATE TABLE IF NOT EXISTS envelope_dedup_v2 (
      envelope_id TEXT PRIMARY KEY,
      first_seen_ms INTEGER NOT NULL,
      req_id TEXT NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('pending','completed')),
      lease_owner TEXT NOT NULL,
      lease_until_ms INTEGER NOT NULL
    )`)
    this.db.exec('CREATE INDEX IF NOT EXISTS envelope_dedup_v2_first_seen ON envelope_dedup_v2(first_seen_ms)')
  }

  claim(envelopeId: string, reqId: string, nowMs = Date.now()): { duplicate: boolean; reqId: string; owner?: string } {
    const owner = randomUUID()
    // `.immediate()`, not the default deferred wrapper. The first statement
    // here is a write, so SQLite would upgrade anyway — but only at that
    // statement, which leaves a window where two consumers both read before
    // either writes. Taking the write lock up front is what makes the claim
    // the concurrency arbiter it is documented to be, and it matches the
    // Python port's `BEGIN IMMEDIATE`.
    const transaction = this.db.transaction(() => {
      this.claims += 1
      if (this.claims % DEDUP_PRUNE_EVERY === 0) this.prune(nowMs)
      this.db.query('DELETE FROM envelope_dedup_v2 WHERE envelope_id=? AND first_seen_ms < ?')
        .run(envelopeId, nowMs - this.ttlMs)
      const inserted = this.db.query(
        "INSERT OR IGNORE INTO envelope_dedup_v2 VALUES (?,?,?,'pending',?,?)",
      ).run(envelopeId, nowMs, reqId, owner, nowMs + this.leaseMsValue)
      if (inserted.changes === 1) return { duplicate: false, reqId, owner }
      const row = this.db.query('SELECT req_id,state,lease_until_ms FROM envelope_dedup_v2 WHERE envelope_id=?')
        .get(envelopeId) as { req_id: string; state: string; lease_until_ms: number }
      if (row.state === 'pending' && row.lease_until_ms <= nowMs) {
        const recovered = this.db.query(
          "UPDATE envelope_dedup_v2 SET lease_owner=?,lease_until_ms=? WHERE envelope_id=? AND state='pending' AND lease_until_ms<=?",
        ).run(owner, nowMs + this.leaseMsValue, envelopeId, nowMs)
        if (recovered.changes === 1) return { duplicate: false, reqId: row.req_id, owner }
      }
      return { duplicate: true, reqId: row.req_id }
    })
    return transaction.immediate()
  }

  /**
   * Extend a live owner's lease. `false` means the lease was already lost and
   * the caller must stop doing externally-visible work: another consumer holds
   * the envelope, so anything from here on is the duplicate.
   */
  renew(envelopeId: string, owner: string, nowMs = Date.now()): boolean {
    return this.db.query(
      "UPDATE envelope_dedup_v2 SET lease_until_ms=? WHERE envelope_id=? AND lease_owner=? AND state='pending'",
    ).run(nowMs + this.leaseMsValue, envelopeId, owner).changes === 1
  }

  /**
   * Mark done. `false` means we no longer owned it, so we did NOT finish it —
   * the in-memory fast paths key off this, and promoting a stale claim would
   * suppress the real owner's result.
   */
  complete(envelopeId: string, owner: string): boolean {
    return this.db.query("UPDATE envelope_dedup_v2 SET state='completed' WHERE envelope_id=? AND lease_owner=? AND state='pending'")
      .run(envelopeId, owner).changes === 1
  }

  release(envelopeId: string, owner: string): boolean {
    return this.db.query("DELETE FROM envelope_dedup_v2 WHERE envelope_id=? AND lease_owner=? AND state='pending'")
      .run(envelopeId, owner).changes === 1
  }

  /**
   * Delete expired claims in bounded batches until drained or out of budget.
   *
   * Bounded batches keep any single statement short; the LOOP is what lets
   * cleanup outpace ingestion. At 100 deleted per 256 admitted the expired
   * backlog grew ~156 rows per 256 arrivals. A short batch means the expired
   * set is exhausted, so the loop stops without spending the rest of the budget.
   */
  prune(nowMs = Date.now(), budget = DEDUP_PRUNE_BUDGET): number {
    const cutoff = nowMs - this.ttlMs
    let deleted = 0
    while (deleted < budget) {
      const batch = Math.min(DEDUP_PRUNE_LIMIT, budget - deleted)
      const n = this.db.query(
        'DELETE FROM envelope_dedup_v2 WHERE rowid IN (SELECT rowid FROM envelope_dedup_v2 WHERE first_seen_ms < ? ORDER BY first_seen_ms LIMIT ?)',
      ).run(cutoff, batch).changes
      deleted += n
      if (n < batch) break // expired set exhausted
    }
    return deleted
  }

  /**
   * Sweep on a quiet lane, where no claim arrives to trigger the counter.
   * Without this a stream that goes quiet after a burst keeps its expired rows
   * until the next arrival — the backlog survives exactly when there is most
   * capacity to clear it.
   */
  pruneIdle(nowMs = Date.now()): number {
    return this.prune(nowMs)
  }

  /** The lease this store issues. Callers deriving a renewal cadence MUST read
   * this rather than the module default, which an injected store may not use. */
  get leaseMs(): number {
    return this.leaseMsValue
  }

  /** Row count. Test/ops introspection only — nothing on the hot path reads it. */
  count(): number {
    return (this.db.query('SELECT COUNT(*) AS n FROM envelope_dedup_v2').get() as { n: number }).n
  }

  /** Rows the TTL says should already be gone. The invariant a sweep must drive to zero. */
  countExpired(nowMs = Date.now()): number {
    return (this.db.query('SELECT COUNT(*) AS n FROM envelope_dedup_v2 WHERE first_seen_ms < ?')
      .get(nowMs - this.ttlMs) as { n: number }).n
  }

  prunePlan(): string {
    return this.db.query(
      'EXPLAIN QUERY PLAN SELECT rowid FROM envelope_dedup_v2 WHERE first_seen_ms < ? ORDER BY first_seen_ms LIMIT ?',
    ).all(0, DEDUP_PRUNE_LIMIT).map(row => JSON.stringify(row)).join(' ')
  }
}

export interface FleetBusFrameMeta {
  source: 'fleet-bus'
  authenticated: 'false'
  from_claim: string
  kind: string
  req_id: string
  env_id: string
  ts: string
  late_reply_env_id?: string
}

export interface TokenBucket {
  allow(key: string): boolean
}

export interface FleetBusRateLimiters {
  perFrom: TokenBucket
  perSubject: TokenBucket
  perSessionInject: TokenBucket
}

export interface FleetBusRequestOptions {
  to: string
  kind: string
  payload: unknown
  wait?: boolean
  timeoutMs?: number
  force?: boolean
  /** Baton lineage source — the inbound envelope currently being answered. Omit for originating requests. */
  inbound?: Envelope
}

export interface FleetBusRequestResult {
  ok: boolean
  envelope?: Envelope
  delivered_to_subscriber?: boolean
  error?: string
  reply?: Envelope
  timed_out?: boolean
}

export interface FleetBusReplyResult {
  ok: boolean
  envelope?: Envelope
  error?: 'claude_discord_adapter_req_id_unknown' | 'claude_discord_adapter_multi_instance_publish_only' | string
  req_id?: string
}

export type EnvelopeValidationResult =
  | { ok: true; envelope: Envelope }
  | { ok: false; error: string }

const BOT_NAME_PATTERN = /^[a-z0-9_-]+$/
export const BROADCAST_KIND_RE = /^[a-z0-9_-]+(\.[a-z0-9_-]+)*$/
export const RESERVED_BOT_NAMES = new Set(['broadcast'])

/** Return the canonical bus identity, or null for a non-ASCII/invalid/reserved claim. */
export function normalizeBotName(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const normalized = value.normalize('NFKC').toLowerCase()
  if (!BOT_NAME_PATTERN.test(normalized)) return null
  if (RESERVED_BOT_NAMES.has(normalized)) return null
  return normalized
}

/** Normalize a manifest bot_names list, rejecting invalid entries. */
export function normalizeAllowlist(values: Iterable<unknown>): Set<string> {
  const result = new Set<string>()
  for (const value of values) {
    const normalized = normalizeBotName(value)
    if (normalized === null) throw new TypeError(`Invalid fleet bot name: ${String(value)}`)
    result.add(normalized)
  }
  return result
}

export function loadFleetManifestAllowlist(path: string): Set<string> {
  const manifest = parseYaml(readFileSync(path, 'utf8')) as unknown
  if (typeof manifest !== 'object' || manifest === null || Array.isArray(manifest)) {
    throw new TypeError('Fleet manifest must be a YAML mapping')
  }
  const botNames = (manifest as Record<string, unknown>).bot_names
  if (!Array.isArray(botNames) || botNames.length === 0) {
    throw new TypeError('Fleet manifest bot_names must be a non-empty list')
  }
  return normalizeAllowlist(botNames)
}

export function createHeartbeatEnvelope(
  botName: string,
  pluginVersion: string,
  pid = process.pid,
  now = new Date(),
): Envelope {
  const from = normalizeBotName(botName)
  if (from === null) throw new TypeError('Invalid heartbeat bot name')
  const ts = now.toISOString()
  return {
    envelope_version: 1,
    id: randomUUID(),
    from,
    to: null,
    kind: 'status_heartbeat',
    ts,
    payload: {
      online: true,
      process_alive_ts: ts,
      session_last_response_ts: null,
      injection_delivered_ts: null,
      pid,
      plugin_version: pluginVersion,
    },
  }
}

class PayloadNotSerializableError extends Error {
  constructor(message = 'payload contains an unserializable value') {
    super(message)
    this.name = 'PayloadNotSerializableError'
  }
}

/**
 * Return true if `payload` will round-trip through `JSON.stringify` with no
 * fields silently disappearing OR silently morphing into `null`. Rejects:
 * - top-level `undefined` (JSON.stringify short-circuits without invoking the replacer)
 * - top-level `function` / `symbol` / `bigint` (same short-circuit path)
 * - ANY nested `undefined` / `function` / `symbol` / `bigint` (would be dropped from
 *   the encoded envelope; `{ok: true, data: undefined}` becomes `{"ok":true}` and
 *   the receiver's expectation quietly diverges from what the sender thinks it sent)
 * - ANY `NaN` / `Infinity` / `-Infinity` at any depth (JSON.stringify silently
 *   coerces these to `null` on the wire — a "42 becomes null" bug is worse than
 *   a clean reject at the send-side boundary)
 * - anything with a hostile `toJSON` that throws
 */
export function payloadIsJsonSerializable(payload: unknown): boolean {
  if (payload === undefined) return false
  const rootKind = typeof payload
  if (rootKind === 'function' || rootKind === 'symbol' || rootKind === 'bigint') return false
  if (rootKind === 'number' && !Number.isFinite(payload as number)) return false
  try {
    JSON.stringify(payload, (_key, value) => {
      if (
        value === undefined ||
        typeof value === 'function' ||
        typeof value === 'symbol' ||
        typeof value === 'bigint'
      ) {
        throw new PayloadNotSerializableError()
      }
      if (typeof value === 'number' && !Number.isFinite(value)) {
        throw new PayloadNotSerializableError()
      }
      return value
    })
    return true
  } catch {
    return false
  }
}

/**
 * Validate the v1 wire envelope before it reaches any bus handler.
 * Extended in Stage 3 to enforce baton-value validity on the wire.
 */
export function validateEnvelope(
  value: unknown,
  allowedFromClaims: ReadonlySet<string>,
  maxBytes = DEFAULT_MAX_ENVELOPE_BYTES,
): EnvelopeValidationResult {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return { ok: false, error: 'envelope_not_object' }
  }

  const candidate = value as Record<string, unknown>
  if (candidate.envelope_version !== 1) return { ok: false, error: 'unsupported_envelope_version' }
  if (typeof candidate.id !== 'string' || candidate.id.length === 0) return { ok: false, error: 'invalid_id' }
  if (typeof candidate.kind !== 'string' || candidate.kind.length === 0) return { ok: false, error: 'invalid_kind' }
  if (typeof candidate.ts !== 'string' || Number.isNaN(Date.parse(candidate.ts))) return { ok: false, error: 'invalid_ts' }
  if (!Object.hasOwn(candidate, 'payload')) return { ok: false, error: 'missing_payload' }
  if (candidate.to !== undefined && candidate.to !== null && typeof candidate.to !== 'string') {
    return { ok: false, error: 'invalid_to' }
  }
  if (candidate.in_reply_to !== undefined && typeof candidate.in_reply_to !== 'string') {
    return { ok: false, error: 'invalid_in_reply_to' }
  }

  const from = normalizeBotName(candidate.from)
  if (from === null || !allowedFromClaims.has(from)) return { ok: false, error: 'from_claim_rejected' }

  // Baton-value validation (v1.x additive; discipline ported from bus.py::validate_envelope).
  if (Object.hasOwn(candidate, 'root_id')) {
    if (typeof candidate.root_id !== 'string' || candidate.root_id.length === 0) {
      return { ok: false, error: 'claude_discord_adapter_invalid_root_id' }
    }
  }
  for (const field of ['origin', 'owner'] as const) {
    if (Object.hasOwn(candidate, field)) {
      const claim = candidate[field]
      const canonical = normalizeBotName(claim)
      if (canonical === null || canonical !== claim || !allowedFromClaims.has(canonical)) {
        return { ok: false, error: `claude_discord_adapter_invalid_${field}` }
      }
    }
  }
  if (Object.hasOwn(candidate, 'hops')) {
    const hops = candidate.hops
    if (!Number.isInteger(hops) || (hops as number) < 0) {
      return { ok: false, error: 'claude_discord_adapter_invalid_hops' }
    }
    // The ceiling fires on both derivation (outbound throw) AND inbound wire
    // validation — otherwise a peer publishing `hops: 100` slips past validate
    // and is only caught if the local bot tries to derive a further increment.
    if ((hops as number) >= 16) {
      return { ok: false, error: 'claude_discord_adapter_invalid_hops_ceiling' }
    }
  }

  let encodedBytes: number
  try {
    encodedBytes = Buffer.byteLength(JSON.stringify(candidate), 'utf8')
  } catch {
    return { ok: false, error: 'payload_not_serializable' }
  }
  if (encodedBytes > maxBytes) return { ok: false, error: 'envelope_too_large' }

  return { ok: true, envelope: { ...candidate, from } as unknown as Envelope }
}

/* -------------------------------------------------------------------------- */
/* Baton derivation                                                            */
/* -------------------------------------------------------------------------- */

export class BatonDerivationError extends Error {
  readonly reason: string
  constructor(reason: string, message: string) {
    super(message)
    this.reason = reason
    this.name = 'BatonDerivationError'
  }
}

export class BatonHopsExhausted extends BatonDerivationError {
  constructor(message = 'baton hop ceiling reached') {
    super('claude_discord_adapter_baton_hops_exhausted', message)
    this.name = 'BatonHopsExhausted'
  }
}

export interface BatonFields {
  root_id: string
  origin: string
  owner: string
  hops: number
}

export interface DeriveBatonInput {
  envelopeId: string
  botName: string
  inbound?: Envelope
  kind: string
  recipient?: string | null
}

/**
 * Derive baton fields for an outbound envelope. Port of
 * `codex-container/bus.py::derive_baton_fields` — port the shape, not the lines.
 * Baton discipline is settled after three review rounds; do not redesign.
 */
export function deriveBatonFields(input: DeriveBatonInput): BatonFields {
  const canonicalBot = normalizeBotName(input.botName)
  if (canonicalBot === null) {
    throw new BatonDerivationError(
      'claude_discord_adapter_baton_bot_name_invalid',
      `invalid baton origin bot name: ${String(input.botName)}`,
    )
  }
  const { envelopeId, inbound, kind, recipient } = input
  let fields: BatonFields
  if (inbound === undefined) {
    fields = { root_id: envelopeId, origin: canonicalBot, owner: canonicalBot, hops: 0 }
  } else {
    const inboundRecord = inbound as unknown as Record<string, unknown>
    const rawRootId = inboundRecord.root_id ?? inboundRecord.id
    const rawOrigin = inboundRecord.origin ?? inboundRecord.from
    const rawOwner = inboundRecord.owner ?? inboundRecord.from
    const rawHops = inboundRecord.hops
    // Number.isInteger deliberately (typeof === 'number' would accept NaN).
    const prevHops = Number.isInteger(rawHops) ? (rawHops as number) : 0
    fields = {
      root_id: rawRootId as string,
      origin: rawOrigin as string,
      owner: rawOwner as string,
      hops: prevHops + 1,
    }
  }
  if (kind === 'baton.handoff') {
    const canonicalRecipient = normalizeBotName(recipient)
    if (canonicalRecipient === null) {
      throw new BatonDerivationError(
        'claude_discord_adapter_baton_handoff_recipient_invalid',
        'baton handoff requires a direct canonical recipient',
      )
    }
    fields.owner = canonicalRecipient
  }
  if (fields.hops >= 16) {
    throw new BatonHopsExhausted()
  }
  return fields
}

/* -------------------------------------------------------------------------- */
/* Frame escape / caps                                                         */
/* -------------------------------------------------------------------------- */

const XML_ESCAPE_MAP: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&apos;',
}
/** Zero-width and bidi-control characters — parity with bus.py `_ZERO_WIDTH_RE`. */
const ZERO_WIDTH_RE = /[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]/g

function xmlEscape(value: string): string {
  return value.replace(/[&<>"']/g, char => XML_ESCAPE_MAP[char] ?? char)
}

/**
 * Sanitize an identifier field for use as an XML attribute value.
 * Identifier fields (env_id, req_id, root_id) are XML-escaped but NOT
 * zero-width stripped — they must remain byte-identical for audit
 * correlation across the fleet.
 */
export function escapeFrameIdentifier(value: unknown, maxLen = DEFAULT_ATTR_MAX_LEN): string {
  const escaped = xmlEscape(String(value))
  if (escaped.length > maxLen) throw new RangeError(`frame identifier exceeds ${maxLen} chars (${escaped.length})`)
  return escaped
}

/**
 * Sanitize a human-visible content field for use as an XML attribute value.
 * Zero-width characters are stripped BEFORE escaping to prevent hidden
 * injection surface from RTL overrides or invisible spacers.
 */
export function escapeFrameContent(value: unknown, maxLen = DEFAULT_ATTR_MAX_LEN): string {
  const stripped = String(value).replace(ZERO_WIDTH_RE, '')
  const escaped = xmlEscape(stripped)
  if (escaped.length > maxLen) throw new RangeError(`frame content exceeds ${maxLen} chars (${escaped.length})`)
  return escaped
}

/**
 * Trim a trailing partial XML entity (e.g. `...&am` cut mid-entity) back to
 * the last completed character so the resulting slice is well-formed XML.
 */
function trimTrailingPartialEntity(value: string): string {
  const lastAmp = value.lastIndexOf('&')
  if (lastAmp === -1) return value
  const tail = value.slice(lastAmp)
  if (tail.includes(';')) return value
  return value.slice(0, lastAmp)
}

/**
 * JSON-encode a payload for the frame body with an 8KB cap + truncation
 * marker. The cap is measured on the ESCAPED output — a payload full of
 * `& < > " '` expands 1 byte to 5 (`&` → `&amp;`), so measuring raw bytes
 * could yield a ~5× overrun of the guaranteed injection surface.
 */
export function buildFleetBusFramePayloadBody(
  envelope: Envelope,
  maxBytes = DEFAULT_PAYLOAD_BODY_MAX_BYTES,
): { body: string; truncated: boolean } {
  const encoded = JSON.stringify(envelope.payload) ?? 'null'
  const escaped = xmlEscape(encoded)
  const escapedSize = Buffer.byteLength(escaped, 'utf8')
  if (escapedSize <= maxBytes) return { body: escaped, truncated: false }
  // env_id is XML-escaped so a hostile id can't break out of the <payload> tag.
  const marker = `\n[...truncated 8KB max, full envelope in audit log env_id=${xmlEscape(String(envelope.id))}]`
  const truncatedBytes = Buffer.from(escaped, 'utf8').subarray(0, maxBytes).toString('utf8')
  // Guard against splitting a multi-byte entity (`&amp;` etc.) at the cap.
  const safeBody = trimTrailingPartialEntity(truncatedBytes)
  return { body: safeBody + marker, truncated: true }
}

export function buildFleetBusFrameMeta(event: FleetBusSessionEvent): FleetBusFrameMeta {
  const { envelope, reqId, lateReplyEnvId } = event
  const meta: FleetBusFrameMeta = {
    source: 'fleet-bus',
    authenticated: 'false',
    from_claim: escapeFrameContent(envelope.from),
    kind: escapeFrameContent(envelope.kind),
    req_id: escapeFrameIdentifier(reqId),
    env_id: escapeFrameIdentifier(envelope.id),
    ts: escapeFrameIdentifier(envelope.ts),
  }
  if (lateReplyEnvId !== undefined) {
    meta.late_reply_env_id = escapeFrameIdentifier(lateReplyEnvId)
  }
  return meta
}

/** Build a complete `<channel>` injection frame for the given session event. */
export function buildFleetBusFrame(event: FleetBusSessionEvent): string {
  const meta = buildFleetBusFrameMeta(event)
  const { envelope } = event
  const attrParts = [
    `source="${meta.source}"`,
    `authenticated="${meta.authenticated}"`,
    `from_claim="${meta.from_claim}"`,
    `kind="${meta.kind}"`,
    `env_id="${meta.env_id}"`,
    `req_id="${meta.req_id}"`,
    `ts="${meta.ts}"`,
  ]
  if (meta.late_reply_env_id !== undefined) {
    attrParts.push(`late_reply_env_id="${meta.late_reply_env_id}"`)
  }
  for (const field of ['root_id', 'origin', 'owner', 'hops'] as const) {
    const value = envelope[field]
    if (value === undefined || value === null) continue
    attrParts.push(`${field}="${escapeFrameIdentifier(value)}"`)
  }
  const { body } = buildFleetBusFramePayloadBody(envelope)
  return `<channel ${attrParts.join(' ')}>\n<payload>${body}</payload>\n</channel>`
}

/* -------------------------------------------------------------------------- */
/* Default rate limiter (fixed-window per key)                                 */
/* -------------------------------------------------------------------------- */

/** Simple per-key fixed-window rate limiter. Sufficient for the 30/min-class limits. */
export class FixedWindowBucket implements TokenBucket {
  private readonly windows = new Map<string, { count: number; expiresAt: number }>()
  constructor(
    public readonly capacity: number,
    public readonly windowMs: number,
    private readonly clock: () => number = () => Date.now(),
  ) {}

  allow(key: string): boolean {
    const now = this.clock()
    const window = this.windows.get(key)
    if (window === undefined || window.expiresAt <= now) {
      this.windows.set(key, { count: 1, expiresAt: now + this.windowMs })
      return true
    }
    if (window.count >= this.capacity) return false
    window.count += 1
    return true
  }
}

function envInt(name: string, fallback: number): number {
  const raw = process.env[name]
  if (!raw) return fallback
  const parsed = Number.parseInt(raw, 10)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback
}

export function defaultRateLimiters(): FleetBusRateLimiters {
  const window = envInt('FLEET_BUS_RATE_WINDOW_MS', 60_000)
  return {
    perFrom: new FixedWindowBucket(envInt('FLEET_BUS_RATE_PER_FROM', 30), window),
    perSubject: new FixedWindowBucket(envInt('FLEET_BUS_RATE_PER_SUBJECT', 120), window),
    perSessionInject: new FixedWindowBucket(envInt('FLEET_BUS_RATE_PER_SESSION_INJECT', 30), window),
  }
}

/* -------------------------------------------------------------------------- */
/* Bounded LRU (Map insertion order + delete-on-touch)                         */
/* -------------------------------------------------------------------------- */

class BoundedLru<K, V> {
  private readonly store = new Map<K, V>()
  constructor(
    public readonly capacity: number,
    private readonly onEvict?: (key: K, value: V) => void,
  ) {}

  get size(): number {
    return this.store.size
  }

  has(key: K): boolean {
    return this.store.has(key)
  }

  get(key: K): V | undefined {
    const value = this.store.get(key)
    if (value === undefined) return undefined
    // Touch — re-insert to move to the end.
    this.store.delete(key)
    this.store.set(key, value)
    return value
  }

  set(key: K, value: V): void {
    if (this.store.has(key)) this.store.delete(key)
    this.store.set(key, value)
    while (this.store.size > this.capacity) {
      const oldest = this.store.keys().next().value
      if (oldest === undefined) break
      const evicted = this.store.get(oldest)!
      this.store.delete(oldest)
      this.onEvict?.(oldest, evicted)
    }
  }

  delete(key: K): boolean {
    return this.store.delete(key)
  }
}

/* -------------------------------------------------------------------------- */
/* Inflight ledger types                                                       */
/* -------------------------------------------------------------------------- */

interface InflightEntry {
  envelope: Envelope
  expectedFrom: string
  /** Local publish-side req_id — preserved so the matched-reply audit can
   * correlate the reply back to the original request in the log. */
  reqId: string
  resolve: (result: FleetBusRequestResult) => void
  timerId: ReturnType<typeof setTimeout>
}

/* -------------------------------------------------------------------------- */
/* FleetBus                                                                    */
/* -------------------------------------------------------------------------- */

/**
 * Session-owned NATS transport. Stage 3 adds request/reply, supervisor loop,
 * suppression-aware reply inject, and per-key rate limiting.
 */
export class FleetBus {
  private nc?: NatsConnection
  private readonly subscriptions = new Set<Subscription>()
  private heartbeatTimer?: ReturnType<typeof setInterval>
  private readonly codec = JSONCodec<unknown>()
  private readonly mode: FleetBusMode
  private readonly outboundLedger: BoundedLru<string, InflightEntry>
  private readonly receiveLedger: BoundedLru<string, Envelope>
  private readonly evictedLedger: BoundedLru<string, true>
  // Envelope-id dedup ledgers (round-8 P2): peers can retry the same wire
  // envelope; without this both the .result and .request handlers happily
  // re-inject each retry as a fresh session turn. Storing the reqId used at
  // first-handle lets the drop audit correlate back to the original turn.
  private readonly seenResultEnvelopes: BoundedLru<string, { reqId: string; ts: number }>
  private readonly seenRequestEnvelopes: BoundedLru<string, { reqId: string; ts: number }>
  private readonly durableDedup: DurableEnvelopeDedupStore
  private readonly dedupTtlMs: number
  private readonly rateLimiters: FleetBusRateLimiters
  private readonly connectFn: (options: ConnectionOptions) => Promise<NatsConnection>
  private supervisorStopping = false
  private closedResolver?: () => void

  constructor(
    private readonly config: FleetBusConfig,
    private readonly allowedFromClaims: ReadonlySet<string>,
  ) {
    this.mode = config.mode ?? 'primary'
    this.connectFn = config.connectFn ?? natsConnect
    this.rateLimiters = config.rateLimiters ?? defaultRateLimiters()
    this.outboundLedger = new BoundedLru<string, InflightEntry>(
      config.inflightLedgerCap ?? DEFAULT_INFLIGHT_LEDGER_CAP,
      (_key, entry) => this.onInflightEvict(entry),
    )
    this.receiveLedger = new BoundedLru<string, Envelope>(config.receiveLedgerCap ?? DEFAULT_RECEIVE_LEDGER_CAP)
    this.evictedLedger = new BoundedLru<string, true>(config.evictedLedgerCap ?? DEFAULT_EVICTED_LEDGER_CAP)
    this.seenResultEnvelopes = new BoundedLru<string, { reqId: string; ts: number }>(
      config.seenResultLedgerCap ?? DEFAULT_SEEN_RESULT_LEDGER_CAP,
    )
    this.seenRequestEnvelopes = new BoundedLru<string, { reqId: string; ts: number }>(
      config.seenRequestLedgerCap ?? DEFAULT_SEEN_REQUEST_LEDGER_CAP,
    )
    this.dedupTtlMs = config.dedupTtlMs ?? DEFAULT_DEDUP_TTL_MS
    this.durableDedup = config.dedupStore ?? new DurableEnvelopeDedupStore(
      config.dedupStorePath ?? `${homedir()}/.claude/fleet-bus-dedup-${config.botName}.sqlite`,
      this.dedupTtlMs,
    )
  }

  /**
   * Promote a claim, surviving a store fault and reporting owner loss.
   *
   * Every durable operation on this path is contained per message: a SQLite
   * fault here must not reject the handler, which was the whole point of the
   * claim-fault guard. Completion is the same class of risk and was not
   * covered. Returns whether WE completed it — a lost owner must not populate
   * the in-memory fast paths as a successful completion.
   */
  private completeClaim(subject: string, envelopeId: string, reqId: string, owner: string): boolean {
    let won: boolean
    try {
      won = this.durableDedup.complete(envelopeId, owner)
    } catch (error) {
      this.recordAudit({
        dir: 'drop', subject, reason: 'claude_discord_adapter_dedup_complete_failed',
        envelope_id: envelopeId, req_id: reqId, note: String(error),
      })
      return false
    }
    if (!won) {
      this.recordAudit({
        dir: 'drop', subject, reason: 'claude_discord_adapter_dedup_owner_lost',
        envelope_id: envelopeId, req_id: reqId,
      })
    }
    return won
  }

  private releaseClaim(subject: string, envelopeId: string, reqId: string, owner: string): boolean {
    try {
      return this.durableDedup.release(envelopeId, owner)
    } catch (error) {
      this.recordAudit({
        dir: 'drop', subject, reason: 'claude_discord_adapter_dedup_release_failed',
        envelope_id: envelopeId, req_id: reqId, note: String(error),
      })
      return false
    }
  }

  private claimDedup(subject: string, envelopeId: string, reqId: string) {
    try {
      return this.durableDedup.claim(envelopeId, reqId)
    } catch (error) {
      this.recordAudit({ dir: 'drop', subject, reason: 'claude_discord_adapter_dedup_store_failed', envelope_id: envelopeId, req_id: reqId, error: String(error) })
      return undefined
    }
  }

  private freshSeen(
    ledger: BoundedLru<string, { reqId: string; ts: number }>,
    envelopeId: string,
  ): { reqId: string; ts: number } | undefined {
    const seen = ledger.get(envelopeId)
    if (seen !== undefined && Date.now() - seen.ts >= this.dedupTtlMs) {
      ledger.delete(envelopeId)
      return undefined
    }
    return seen
  }

  async connect(): Promise<void> {
    if (this.nc) return

    const botName = normalizeBotName(this.config.botName)
    const user = normalizeBotName(this.config.user)
    if (botName === null || user === null || botName !== user) {
      throw new Error('FleetBus botName and user must be the same canonical fleet identity')
    }

    const nc = await this.connectFn({
      servers: this.config.url,
      user,
      pass: this.config.password,
      inboxPrefix: `_INBOX_${botName}`,
      maxReconnectAttempts: -1,
    })

    try {
      this.nc = nc
      if (this.mode !== 'publish-only') {
        this.subscribe(`fleet.${botName}.request`, message => this.onRequest(message))
        this.subscribe(`fleet.${botName}.result`, message => this.onResult(message))
        this.subscribe(`fleet.${botName}.status`, message => this.onStatus(message))
        if (this.config.subscribeBroadcast) {
          this.subscribe('fleet.broadcast.>', message => this.onBroadcast(message))
        }
        this.publishHeartbeat()
        this.heartbeatTimer = setInterval(
          () => this.publishHeartbeat(),
          this.config.heartbeatIntervalMs ?? 30_000,
        )
      }
      this.log(`connected as ${botName}${this.mode === 'publish-only' ? ' [publish-only]' : ''}`)
      void this.watchConnectionStatus(nc)
    } catch (error) {
      this.nc = undefined
      await nc.close()
      throw error
    }
  }

  async disconnect(): Promise<void> {
    if (this.heartbeatTimer) clearInterval(this.heartbeatTimer)
    this.heartbeatTimer = undefined
    for (const subscription of this.subscriptions) subscription.unsubscribe()
    this.subscriptions.clear()

    const nc = this.nc
    this.nc = undefined
    if (nc && !nc.isClosed()) await nc.drain()
    const resolver = this.closedResolver
    this.closedResolver = undefined
    resolver?.()
  }

  /**
   * Run the supervisor loop: connect, wait for CLOSED, disconnect (clears
   * subscriptions/heartbeat/nc reference), sleep, retry. Closes only when
   * `stop()` is called.
   */
  async run(): Promise<void> {
    const sleepMs = this.config.supervisorSleepMs ?? 2_000
    this.supervisorStopping = false
    while (!this.supervisorStopping) {
      try {
        await this.connect()
      } catch (error) {
        this.log(`supervisor: connect failed: ${String(error)}`)
        await this.disconnect().catch(() => {})
        if (this.supervisorStopping) break
        await this.sleep(sleepMs)
        continue
      }
      // Race: stop() may have fired while connect() was in flight. Recheck
      // BEFORE waitClosed / subscribe traffic so the caller isn't left with
      // a live connection they thought was torn down.
      if (this.supervisorStopping) {
        await this.disconnect().catch(() => {})
        break
      }
      await this.waitClosed()
      if (this.supervisorStopping) break
      this.log('supervisor: connection closed, reconnecting after backoff')
      await this.disconnect().catch(() => {})
      await this.sleep(sleepMs)
    }
    await this.disconnect().catch(() => {})
  }

  async stop(): Promise<void> {
    this.supervisorStopping = true
    const resolver = this.closedResolver
    this.closedResolver = undefined
    resolver?.()
    await this.disconnect()
  }

  private waitClosed(): Promise<void> {
    if (!this.nc) return Promise.resolve()
    const closedByServer = this.nc.closed().then(() => undefined).catch(() => undefined)
    const explicit = new Promise<void>(resolve => {
      this.closedResolver = resolve
    })
    return Promise.race([closedByServer, explicit])
  }

  private sleep(ms: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, ms))
  }

  async request(options: FleetBusRequestOptions): Promise<FleetBusRequestResult> {
    if (this.mode === 'publish-only' && options.wait === true) {
      return { ok: false, error: 'claude_discord_adapter_multi_instance_publish_only' }
    }
    if (!this.nc || this.nc.isClosed()) return { ok: false, error: 'claude_discord_adapter_fleet_bus_not_connected' }

    const canonicalTo = normalizeBotName(options.to)
    if (canonicalTo === null) return { ok: false, error: 'claude_discord_adapter_invalid_recipient' }
    if (!payloadIsJsonSerializable(options.payload)) {
      this.recordAudit({
        dir: 'drop', subject: `fleet.${canonicalTo}.request`,
        reason: 'claude_discord_adapter_payload_not_json_serializable',
      })
      return { ok: false, error: 'claude_discord_adapter_payload_not_json_serializable' }
    }
    const canonicalBot = normalizeBotName(this.config.botName)!
    const envelopeId = randomUUID()
    let baton: BatonFields
    try {
      baton = deriveBatonFields({
        envelopeId,
        botName: canonicalBot,
        inbound: options.inbound,
        kind: options.kind,
        recipient: options.to,
      })
    } catch (error) {
      const reason = error instanceof BatonDerivationError ? error.reason : 'claude_discord_adapter_baton_derivation_failed'
      this.recordAudit({ dir: 'drop', subject: `fleet.${canonicalTo}.request`, reason, envelope_id: envelopeId })
      return { ok: false, error: reason }
    }
    const envelope: Envelope = {
      envelope_version: 1,
      id: envelopeId,
      from: canonicalBot,
      to: canonicalTo,
      kind: options.kind,
      ts: new Date().toISOString(),
      payload: options.payload,
      ...baton,
    }
    const validation = validateEnvelope(envelope, this.allowedFromClaims, this.config.maxEnvelopeBytes ?? DEFAULT_MAX_ENVELOPE_BYTES)
    if (!validation.ok) {
      this.recordAudit({ dir: 'drop', subject: `fleet.${canonicalTo}.request`, reason: validation.error, envelope_id: envelopeId })
      return { ok: false, error: validation.error, envelope }
    }
    const subject = `fleet.${canonicalTo}.request`
    // Local publish-side req_id per SPEC §8 — outbound audits must carry both
    // envelope_id and req_id for cross-line correlation (mirrors bus.py:341).
    const localReqId = randomBytes(16).toString('hex')
    try {
      this.nc.publish(subject, this.codec.encode(envelope))
    } catch (error) {
      this.recordAudit({ dir: 'drop', subject, reason: 'claude_discord_adapter_publish_failed', envelope_id: envelopeId, req_id: localReqId, error: String(error) })
      return { ok: false, error: 'claude_discord_adapter_publish_failed', envelope }
    }
    this.recordAudit({ dir: 'out', subject, envelope_id: envelopeId, req_id: localReqId })

    if (options.wait !== true) {
      // Core NATS publish does not confirm subscriber receipt; omit
      // `delivered_to_subscriber` entirely so the caller can't misread
      // "we published" as "someone consumed it". True is reserved for the
      // wait: true path where a matching .result reply arrives.
      return { ok: true, envelope }
    }

    const timeoutMs = options.timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS
    return new Promise<FleetBusRequestResult>(resolve => {
      const timerId = setTimeout(() => {
        // Timeout fires — evict from ledger, remember for late-reply tagging, resolve as timed_out.
        if (this.outboundLedger.has(envelopeId)) {
          this.outboundLedger.delete(envelopeId)
          this.evictedLedger.set(envelopeId, true)
          this.recordAudit({ dir: 'drop', subject, reason: 'claude_discord_adapter_request_timeout', envelope_id: envelopeId, req_id: localReqId })
          resolve({ ok: false, timed_out: true, envelope })
        }
      }, timeoutMs)
      this.outboundLedger.set(envelopeId, {
        envelope,
        expectedFrom: canonicalTo,
        reqId: localReqId,
        resolve,
        timerId,
      })
    })
  }

  publishReply(reqId: string, payload: unknown, kind = 'result'): FleetBusReplyResult {
    if (this.mode === 'publish-only') {
      return { ok: false, error: 'claude_discord_adapter_multi_instance_publish_only', req_id: reqId }
    }
    if (!this.nc || this.nc.isClosed()) return { ok: false, error: 'claude_discord_adapter_fleet_bus_not_connected', req_id: reqId }
    const inbound = this.receiveLedger.get(reqId)
    if (inbound === undefined) return { ok: false, error: 'claude_discord_adapter_req_id_unknown', req_id: reqId }
    if (!payloadIsJsonSerializable(payload)) {
      this.recordAudit({
        dir: 'drop', subject: `fleet.${inbound.from}.result`,
        reason: 'claude_discord_adapter_payload_not_json_serializable', req_id: reqId,
      })
      return { ok: false, error: 'claude_discord_adapter_payload_not_json_serializable', req_id: reqId }
    }
    const canonicalBot = normalizeBotName(this.config.botName)!
    const envelopeId = randomUUID()
    let baton: BatonFields
    try {
      baton = deriveBatonFields({
        envelopeId,
        botName: canonicalBot,
        inbound,
        kind,
        recipient: inbound.from,
      })
    } catch (error) {
      const reason = error instanceof BatonDerivationError ? error.reason : 'claude_discord_adapter_baton_derivation_failed'
      this.recordAudit({ dir: 'drop', subject: `fleet.${inbound.from}.result`, reason, envelope_id: envelopeId })
      return { ok: false, error: reason, req_id: reqId }
    }
    const envelope: Envelope = {
      envelope_version: 1,
      id: envelopeId,
      from: canonicalBot,
      to: inbound.from,
      kind,
      // Wire correlation uses the inbound WIRE id, not the consumer-local reqId nonce (round-2 P1).
      in_reply_to: inbound.id,
      ts: new Date().toISOString(),
      payload,
      ...baton,
    }
    const validation = validateEnvelope(envelope, this.allowedFromClaims, this.config.maxEnvelopeBytes ?? DEFAULT_MAX_ENVELOPE_BYTES)
    if (!validation.ok) {
      this.recordAudit({ dir: 'drop', subject: `fleet.${inbound.from}.result`, reason: validation.error, envelope_id: envelopeId })
      return { ok: false, error: validation.error, req_id: reqId, envelope }
    }
    const subject = `fleet.${inbound.from}.result`
    try {
      this.nc.publish(subject, this.codec.encode(envelope))
    } catch (error) {
      this.recordAudit({ dir: 'drop', subject, reason: 'claude_discord_adapter_publish_failed', envelope_id: envelopeId, error: String(error) })
      return { ok: false, error: 'claude_discord_adapter_publish_failed', req_id: reqId, envelope }
    }
    this.recordAudit({ dir: 'out', subject, envelope_id: envelopeId, req_id: reqId })
    return { ok: true, envelope, req_id: reqId }
  }

  protected async onRequest(message: Msg): Promise<void> {
    const subject = message.subject
    if (!this.rateLimiters.perSubject.allow(subject)) {
      this.recordAudit({ dir: 'drop', subject, reason: 'claude_discord_adapter_rate_limited_per_subject' })
      return
    }
    let decoded: unknown
    try {
      decoded = this.codec.decode(message.data)
    } catch {
      this.recordAudit({ dir: 'drop', subject, reason: 'claude_discord_adapter_malformed_json' })
      return
    }
    const result = validateEnvelope(
      decoded,
      this.allowedFromClaims,
      this.config.maxEnvelopeBytes ?? DEFAULT_MAX_ENVELOPE_BYTES,
    )
    if (!result.ok) {
      this.recordAudit({ dir: 'drop', subject, reason: result.error })
      return
    }
    if (normalizeBotName(result.envelope.to) !== normalizeBotName(this.config.botName)) {
      this.recordAudit({
        dir: 'drop',
        subject,
        reason: 'recipient_mismatch',
        envelope_id: result.envelope.id,
      })
      return
    }
    if (!this.rateLimiters.perFrom.allow(result.envelope.from)) {
      this.recordAudit({
        dir: 'drop',
        subject,
        reason: 'claude_discord_adapter_rate_limited_per_from',
        envelope_id: result.envelope.id,
      })
      return
    }

    // Expand-phase correlation: migrated peers may send a reply on the
    // request lane before publication flips away from `.result`. Correlation
    // is subject-independent, so consult the existing outbound waiter ledger.
    // Use the result-envelope dedup ledger as well: the same reply can arrive
    // on both lanes during migration and must resolve exactly once.
    const inReplyTo = result.envelope.in_reply_to
    if (inReplyTo !== undefined) {
      const seenResult = this.freshSeen(this.seenResultEnvelopes, result.envelope.id)
      if (seenResult !== undefined) {
        this.recordAudit({
          dir: 'drop',
          subject,
          reason: 'claude_discord_adapter_duplicate_result_envelope',
          envelope_id: result.envelope.id,
          req_id: seenResult.reqId,
        })
        return
      }

      const match = this.outboundLedger.get(inReplyTo)
      if (match !== undefined) {
        if (result.envelope.from === match.expectedFrom) {
          const claim = this.claimDedup(subject, result.envelope.id, match.reqId)
          if (claim === undefined) return
          if (claim.duplicate) {
            this.recordAudit({ dir: 'drop', subject, reason: 'claude_discord_adapter_duplicate_envelope', envelope_id: result.envelope.id, req_id: claim.reqId })
            return
          }
          const owner = claim.owner!
          clearTimeout(match.timerId)
          this.outboundLedger.delete(inReplyTo)
          this.seenResultEnvelopes.set(result.envelope.id, { reqId: match.reqId, ts: Date.now() })
          this.recordAudit({
            dir: 'in', subject, envelope_id: result.envelope.id,
            req_id: match.reqId, note: 'claude_discord_adapter_ledger_matched',
          })
          match.resolve({
            ok: true,
            envelope: match.envelope,
            delivered_to_subscriber: true,
            reply: result.envelope,
          })
          this.completeClaim(subject, result.envelope.id, claim.reqId, owner)
          return
        }
        // A matching id is not sufficient: only the addressed bot may answer
        // the waiter. Preserve request-lane behavior by auditing the mismatch
        // and falling through to ordinary fresh-turn injection.
        this.recordAudit({
          dir: 'drop',
          subject,
          reason: 'claude_discord_adapter_reply_from_mismatch',
          envelope_id: result.envelope.id,
          req_id: match.reqId,
          expected_from: match.expectedFrom,
        })
      }
    }
    // Envelope-id dedup (round-8 P2, class-widened from onResult). Peers can
    // retry `.request` frames for the same reasons they retry `.result` —
    // transport re-send, publisher restart, back-off retries. Without this
    // gate the retry mints a fresh reqId and injects as a second session
    // turn. Separate ledger from seenResultEnvelopes so a peer with the same
    // wire id on both subjects (protocol violation, but possible) can't
    // silently swallow one side.
    const seenRequest = this.freshSeen(this.seenRequestEnvelopes, result.envelope.id)
    if (seenRequest !== undefined) {
      this.recordAudit({
        dir: 'drop',
        subject,
        reason: 'claude_discord_adapter_duplicate_request_envelope',
        envelope_id: result.envelope.id,
        req_id: seenRequest.reqId,
      })
      return
    }
    if (!this.rateLimiters.perSessionInject.allow('session')) {
      this.recordAudit({
        dir: 'drop',
        subject,
        reason: 'claude_discord_adapter_rate_limited_per_session_inject',
        envelope_id: result.envelope.id,
      })
      return
    }

    let reqId = randomBytes(16).toString('hex')
    const claim = this.claimDedup(subject, result.envelope.id, reqId)
    if (claim === undefined) return
    if (claim.duplicate) {
      this.recordAudit({ dir: 'drop', subject, reason: 'claude_discord_adapter_duplicate_envelope', envelope_id: result.envelope.id, req_id: claim.reqId })
      return
    }
    reqId = claim.reqId
    this.receiveLedger.set(reqId, result.envelope)
    // Single dir:in audit per received envelope (dedup fix, round-3 P2).
    this.recordAudit({ dir: 'in', subject, envelope_id: result.envelope.id, req_id: reqId })
    // Hold the lease for as long as the turn actually runs. Without this a
    // turn longer than the lease is handed to a second consumer WHILE THE
    // FIRST IS STILL EXECUTING — the one duplication window that is closable
    // at this boundary. The post-effect/pre-commit window is not closable
    // here; it is documented as at-least-once (SPEC, and yugo#24).
    const stopRenewing = this.renewWhileRunning(subject, result.envelope.id, claim.reqId, claim.owner!)
    try {
      await this.injectIntoSession({ envelope: result.envelope, reqId })
      stopRenewing()
      // Only a claim we still OWNED counts as completed. Populating the
      // in-memory fast path on a stale or failed completion would suppress
      // the real owner's delivery.
      if (this.completeClaim(subject, result.envelope.id, claim.reqId, claim.owner!)) {
        this.seenRequestEnvelopes.set(result.envelope.id, { reqId, ts: Date.now() })
      }
    } catch (error) {
      stopRenewing()
      this.releaseClaim(subject, result.envelope.id, claim.reqId, claim.owner!)
      this.recordAudit({ dir: 'drop', subject, reason: 'claude_discord_adapter_injection_failed', envelope_id: result.envelope.id, req_id: reqId, error: String(error) })
    }
  }

  /**
   * Keep a live owner's lease fresh until the returned stop function is called.
   *
   * The cadence runs off a timer, so it cannot be stretched or skipped by a
   * wall-clock step; the STORED deadline stays wall-clock because competing
   * consumers in other processes compare it. What renewal buys is that a
   * healthy owner keeps pushing its own deadline forward, so neither a slow
   * turn nor a forward clock jump hands its envelope to a second worker.
   */
  private renewWhileRunning(subject: string, envelopeId: string, reqId: string, owner: string): () => void {
    // The STORE's lease, not the module default. An embedder can inject a
    // store with its own lease (the third constructor parameter), and reading
    // the default gave a 24s cadence against a 5s lease — renewal firing four
    // leases late and fencing nothing, silently.
    // Floor is a guard against a pathological zero, NOT a minimum cadence: a
    // 1s floor silently disabled fencing for any lease under 2.5s, because
    // renewal then fired after the lease had already lapsed. Matches the
    // Python port's floor.
    const intervalMs = Math.max(50, this.durableDedup.leaseMs * DEDUP_LEASE_RENEW_RATIO)
    const timer = setInterval(() => {
      let held: boolean
      try {
        held = this.durableDedup.renew(envelopeId, owner)
      } catch (error) {
        clearInterval(timer)
        this.recordAudit({
          dir: 'drop', subject, reason: 'claude_discord_adapter_dedup_renew_failed',
          envelope_id: envelopeId, req_id: reqId, note: String(error),
        })
        return
      }
      if (!held) {
        clearInterval(timer)
        // Nothing here can cancel the in-flight turn, so record it: under the
        // at-least-once contract this duplicate must be visible, not silent.
        this.recordAudit({
          dir: 'drop', subject, reason: 'claude_discord_adapter_dedup_lease_lost',
          envelope_id: envelopeId, req_id: reqId,
        })
      }
    }, intervalMs)
    // Never hold the event loop open for a lease timer.
    if (typeof timer === 'object' && timer !== null && 'unref' in timer) (timer as { unref: () => void }).unref()
    return () => clearInterval(timer)
  }

  protected async onResult(message: Msg): Promise<void> {
    const subject = message.subject
    if (!this.rateLimiters.perSubject.allow(subject)) {
      this.recordAudit({ dir: 'drop', subject, reason: 'claude_discord_adapter_rate_limited_per_subject' })
      return
    }
    let decoded: unknown
    try {
      decoded = this.codec.decode(message.data)
    } catch {
      this.recordAudit({ dir: 'drop', subject, reason: 'claude_discord_adapter_malformed_json' })
      return
    }
    const validation = validateEnvelope(
      decoded,
      this.allowedFromClaims,
      this.config.maxEnvelopeBytes ?? DEFAULT_MAX_ENVELOPE_BYTES,
    )
    if (!validation.ok) {
      this.recordAudit({ dir: 'drop', subject, reason: validation.error })
      return
    }
    const envelope = validation.envelope
    const canonicalBot = normalizeBotName(this.config.botName)
    // Preserve the raw-null distinction (Ohm round-7 P1). A legitimate
    // broadcast reply has `to == null` (loose equality: null or undefined) and
    // is allowed through. Any *present* non-null recipient MUST canonicalize to
    // this bot; otherwise it is either misrouted or an invalid recipient
    // string, both of which drop as misrouted_reply. Do NOT collapse null and
    // an invalid `normalizeBotName()` return into the same "recipient absent"
    // branch — that lets `.result` frames with `to: "not.a.bot"` slip past the
    // gate and resolve waiters in the outbound ledger.
    if (envelope.to != null) {
      const canonicalTo = normalizeBotName(envelope.to)
      if (canonicalTo !== canonicalBot) {
        this.recordAudit({
          dir: 'drop',
          subject,
          reason: 'claude_discord_adapter_misrouted_reply',
          envelope_id: envelope.id,
        })
        return
      }
    }
    if (!this.rateLimiters.perFrom.allow(envelope.from)) {
      this.recordAudit({
        dir: 'drop',
        subject,
        reason: 'claude_discord_adapter_rate_limited_per_from',
        envelope_id: envelope.id,
      })
      return
    }

    // Envelope-id dedup (round-8 P2): peers can retry a `.result` for many
    // reasons (transport re-send, publisher restart). Without this gate, the
    // first copy resolves the waiter + drops out of the inflight ledger, then
    // the second copy hits the no-match branch and injects as a fresh
    // unsolicited turn with a NEW reqId. The evictedLedger doesn't help — it
    // was already consumed by the first arrival. Bounded envelope-id ledger
    // catches both the ledger-matched retry AND the unsolicited retry AND the
    // late-reply retry (which would otherwise re-inject without the late-reply
    // tag on the second arrival). Runs AFTER validate/recipient/rate-limit
    // gates so a flood of duplicates from a bad peer still hits per-from/
    // per-subject limits first.
    const seenResult = this.freshSeen(this.seenResultEnvelopes, envelope.id)
    if (seenResult !== undefined) {
      this.recordAudit({
        dir: 'drop',
        subject,
        reason: 'claude_discord_adapter_duplicate_result_envelope',
        envelope_id: envelope.id,
        req_id: seenResult.reqId,
      })
      return
    }

    const inReplyTo = envelope.in_reply_to
    if (inReplyTo !== undefined) {
      const match = this.outboundLedger.get(inReplyTo)
      if (match !== undefined) {
        if (envelope.from === match.expectedFrom) {
          const claim = this.claimDedup(subject, envelope.id, match.reqId)
          if (claim === undefined) return
          if (claim.duplicate) {
            this.recordAudit({ dir: 'drop', subject, reason: 'claude_discord_adapter_duplicate_envelope', envelope_id: envelope.id, req_id: claim.reqId })
            return
          }
          const owner = claim.owner!
          // Ledger-matched .result: resolve the outstanding waiter and BYPASS
          // perSessionInject — this resolves an already-running tool call
          // rather than initiating a new session turn (P2-5). Preserve the
          // ORIGINAL request's req_id on the audit line so log correlation
          // (reply-to-request) survives.
          clearTimeout(match.timerId)
          this.outboundLedger.delete(inReplyTo)
          // Record BEFORE resolve so a synchronous consumer that re-enters
          // handleResult on the same envelope sees the dedup gate armed.
          this.seenResultEnvelopes.set(envelope.id, { reqId: match.reqId, ts: Date.now() })
          this.recordAudit({
            dir: 'in', subject, envelope_id: envelope.id,
            req_id: match.reqId, note: 'claude_discord_adapter_ledger_matched',
          })
          match.resolve({ ok: true, envelope: match.envelope, delivered_to_subscriber: true, reply: envelope })
          this.completeClaim(subject, envelope.id, claim.reqId, owner)
          return
        }
        // From mismatch — anti-hijack (P2-1). Do NOT resolve the waiter; treat
        // as unsolicited and let the model see it if inject budget allows.
        this.recordAudit({
          dir: 'drop',
          subject,
          reason: 'claude_discord_adapter_reply_from_mismatch',
          envelope_id: envelope.id,
          req_id: match.reqId,
          expected_from: match.expectedFrom,
        })
        await this.injectUnsolicited(subject, envelope)
        return
      }
    }

    // No ledger match — inject as unsolicited. Tag as late-reply if we
    // remember evicting a request with this id (P2-2).
    const lateReplyEnvId = inReplyTo !== undefined && this.evictedLedger.has(inReplyTo) ? inReplyTo : undefined
    if (lateReplyEnvId !== undefined) this.evictedLedger.delete(lateReplyEnvId)
    await this.injectUnsolicited(subject, envelope, lateReplyEnvId)
  }

  private async injectUnsolicited(subject: string, envelope: Envelope, lateReplyEnvId?: string): Promise<void> {
    if (!this.rateLimiters.perSessionInject.allow('session')) {
      this.recordAudit({
        dir: 'drop',
        subject,
        reason: 'claude_discord_adapter_rate_limited_per_session_inject',
        envelope_id: envelope.id,
      })
      return
    }
    let reqId = randomBytes(16).toString('hex')
    const claim = this.claimDedup(subject, envelope.id, reqId)
    if (claim === undefined) return
    if (claim.duplicate) {
      this.recordAudit({ dir: 'drop', subject, reason: 'claude_discord_adapter_duplicate_envelope', envelope_id: envelope.id, req_id: claim.reqId })
      return
    }
    reqId = claim.reqId
    // Round-8 P2: any inject path that reaches this line has committed a
    // session turn to this envelope; a duplicate arriving later must be
    // deduped so we don't burn another turn on the same wire id.
    this.receiveLedger.set(reqId, envelope)
    this.recordAudit({
      dir: 'in',
      subject,
      envelope_id: envelope.id,
      req_id: reqId,
      note: lateReplyEnvId !== undefined ? 'claude_discord_adapter_late_reply' : 'claude_discord_adapter_unsolicited_reply',
      ...(lateReplyEnvId !== undefined ? { late_reply_env_id: lateReplyEnvId } : {}),
    })
    // Same lease discipline as `onRequest`. This path was missed in the first
    // pass, which left the exact P1 it claimed to fix alive on the sibling
    // route: an unsolicited or late `.result` whose turn outruns the lease
    // could be reclaimed and re-injected while the first turn still ran.
    const stopRenewing = this.renewWhileRunning(subject, envelope.id, claim.reqId, claim.owner!)
    try {
      await this.injectIntoSession({ envelope, reqId, unsolicited: true, lateReplyEnvId })
      stopRenewing()
      // Gated for the reason `completeClaim`'s contract states: a lost owner
      // must not arm the in-memory fast path, or the real owner's result is
      // dropped here as a duplicate when it arrives.
      if (this.completeClaim(subject, envelope.id, claim.reqId, claim.owner!)) {
        this.seenResultEnvelopes.set(envelope.id, { reqId, ts: Date.now() })
      }
    } catch (error) {
      stopRenewing()
      this.releaseClaim(subject, envelope.id, claim.reqId, claim.owner!)
      this.recordAudit({ dir: 'drop', subject, reason: 'claude_discord_adapter_injection_failed', envelope_id: envelope.id, req_id: reqId, error: String(error) })
    }
  }

  protected onStatus(message: Msg): void {
    this.log(`received ${message.subject}`)
  }

  protected onBroadcast(message: Msg): void {
    this.log(`received ${message.subject}`)
  }

  private onInflightEvict(entry: InflightEntry): void {
    // LRU eviction of a pending waiter — reject with ledger_overflow AND clear
    // the timer (not a silent limp-to-timeout, per round-3 P2-3).
    clearTimeout(entry.timerId)
    this.evictedLedger.set(entry.envelope.id, true)
    this.recordAudit({
      dir: 'drop',
      subject: `fleet.${entry.envelope.to}.request`,
      reason: 'claude_discord_adapter_ledger_overflow',
      envelope_id: entry.envelope.id,
      req_id: entry.reqId,
    })
    entry.resolve({ ok: false, error: 'claude_discord_adapter_ledger_overflow', envelope: entry.envelope })
  }

  private subscribe(subject: string, handler: (message: Msg) => void | Promise<void>): void {
    if (!this.nc) throw new Error('FleetBus is not connected')
    const subscription = this.nc.subscribe(subject)
    this.subscriptions.add(subscription)
    void (async () => {
      try {
        for await (const message of subscription) {
          // Contained PER MESSAGE. With the catch outside this loop, one
          // rejected handler — a transient SQLITE_BUSY after the five-second
          // timeout, a single bad envelope — ended the lane permanently on a
          // healthy connection and heartbeat, so the bot looked alive and
          // silently received nothing.
          try {
            await handler(message)
          } catch (error) {
            this.log(`subscription ${subject} handler failed, lane continues: ${String(error)}`)
          }
        }
      } catch (error) {
        // Reaching here means the ITERATOR itself failed — the subscription is
        // gone, not one message. That genuinely ends this lane.
        if (!this.nc?.isClosed()) this.log(`subscription ${subject} failed: ${String(error)}`)
      } finally {
        this.subscriptions.delete(subscription)
      }
    })()
  }

  private publishHeartbeat(): void {
    // Piggyback the quiet-lane sweep on the heartbeat. `prune` is otherwise
    // only reached from `claim`, so a lane that goes silent after a burst
    // keeps its expired rows until the next arrival — the backlog survives
    // exactly when there is most capacity to clear it. Contained: a store
    // fault must never stop heartbeats, which are this bot's liveness signal.
    try {
      this.durableDedup.pruneIdle()
    } catch (error) {
      this.log(`idle dedup prune failed, heartbeat continues: ${String(error)}`)
    }

    if (!this.nc || this.nc.isClosed()) return
    this.nc.publish(
      `fleet.${this.config.botName}.status`,
      this.codec.encode(createHeartbeatEnvelope(
        this.config.botName,
        this.config.pluginVersion ?? '0.4.0',
      )),
    )
  }

  private async watchConnectionStatus(nc: NatsConnection): Promise<void> {
    for await (const status of nc.status()) {
      if (status.type === 'disconnect' || status.type === 'reconnect' || status.type === 'error') {
        this.log(`${status.type}: ${String(status.data)}`)
      }
    }
  }

  /**
   * Dispatch the envelope to the session callback. Deliberately does NOT
   * write the primary `dir: in` audit here — every caller writes exactly
   * one `dir: in` BEFORE calling this method, so unsolicited/late replies
   * can carry their `note` field without producing a duplicate audit entry
   * (P2 round 3).
   *
   * DOES persist the full payload as a `dir: out` audit line WHEN the
   * standard 8KB body cap would truncate — from FleetBus's perspective the
   * frame is outbound to the session. The injection frame's marker
   * (`[...full envelope in audit log env_id=<id>]`) is only a truthful
   * promise if the full envelope is actually somewhere in the audit log
   * (P2 round 4). Uses `dir: 'out'` per SPEC §8's audit-dir schema
   * (`in|out|drop`); the distinctive `reason` field distinguishes this
   * from NATS-outbound publishes (P2 round 6).
   */
  private async injectIntoSession(event: FleetBusSessionEvent): Promise<void> {
    const { truncated } = buildFleetBusFramePayloadBody(event.envelope)
    if (truncated) {
      this.recordAudit({
        dir: 'out',
        reason: 'claude_discord_adapter_payload_body_truncated',
        envelope_id: event.envelope.id,
        req_id: event.reqId,
        payload_full: JSON.stringify(event.envelope.payload) ?? 'null',
      })
    }
    if (!this.config.injectIntoSession) {
      this.log(`received ${event.envelope.kind} from ${event.envelope.from}; session injection is not configured`)
      return
    }
    await this.config.injectIntoSession(event)
  }

  private recordAudit(entry: Record<string, unknown>): void {
    if (!this.config.auditLogPath) {
      this.log(JSON.stringify(entry))
      return
    }
    try {
      mkdirSync(dirname(this.config.auditLogPath), { recursive: true, mode: 0o700 })
      appendFileSync(this.config.auditLogPath, `${JSON.stringify({ ts: new Date().toISOString(), ...entry })}\n`, {
        encoding: 'utf8',
        mode: 0o600,
      })
      chmodSync(this.config.auditLogPath, 0o600)
    } catch (error) {
      this.log(`audit write failed: ${String(error)}`)
    }
  }

  private log(message: string): void {
    this.config.logger?.(`FleetBus: ${message}`)
  }
}

/**
 * Convenience: build a bus and start its supervisor loop.
 * Returns `{ bus, done }` — await `done` to block until `bus.stop()` is called,
 * or ignore it and call `bus.stop()` at shutdown.
 */
export function runSupervisor(
  config: FleetBusConfig,
  allowedFromClaims: ReadonlySet<string>,
): { bus: FleetBus; done: Promise<void> } {
  const bus = new FleetBus(config, allowedFromClaims)
  const done = bus.run()
  return { bus, done }
}
