/**
 * Pure formatting and dedupe helpers for the fleet-bus tap.
 *
 * Extracted from tap.ts so they can be imported and tested. tap.ts opens a NATS
 * connection at module scope, so importing it runs the tap — there was no way to
 * exercise these functions at all before this split.
 *
 * Everything here must stay pure: no I/O, no module-scope side effects.
 *
 * EVERY function here must be TOTAL. These inputs come off the wire from any
 * publisher, so "this value will be a string" is never available as an assumption.
 */

export interface Envelope {
  envelope_version?: number
  id?: string
  from?: string
  to?: string
  kind?: string
  in_reply_to?: string
  ts?: string
  payload?: unknown
}

export function isEnvelope(o: unknown): o is Envelope {
  return typeof o === 'object' && o !== null && ('envelope_version' in o || ('from' in o && 'kind' in o))
}

/**
 * Render ANY value as a string, without ever throwing.
 *
 * `String(value)` is not safe here and neither is template interpolation.
 * `JSON.parse('{"toString":null}')` shadows Object.prototype.toString, and such an
 * object cannot be converted to a primitive at all:
 *
 *     String(hostile)   -> TypeError: No default value        (Bun)
 *     `${hostile}`      -> TypeError: No default value
 *     JSON.stringify(hostile) -> '{"toString":null}'          (fine)
 *
 * That is ORDINARY WIRE JSON — no proxies, no functions, nothing exotic. Any
 * publisher can send it, and before this it terminated the tap.
 *
 * Found by Ohm reviewing PR #67. The first version of this file used String()
 * and its test used `{a: 1}` — an object with an INTACT inherited toString, which
 * renders as '[object Object]' and passes. The test passed for the wrong reason
 * while the real case still killed the process.
 *
 * So: never rely on primitive conversion for a value that came off the wire.
 * Dispatch on typeof, and reach for JSON.stringify — which cannot be shadowed
 * this way — for everything else.
 */
export function safeText(value: unknown): string {
  const t = typeof value
  if (t === 'string') return value as string
  if (t === 'number' || t === 'boolean' || t === 'bigint') return String(value)
  if (value === null) return 'null'
  if (value === undefined) return 'undefined'
  try {
    const j = JSON.stringify(value)
    // JSON.stringify returns undefined for symbols and functions.
    return typeof j === 'string' ? j : '[unrenderable]'
  } catch {
    // Circular structures, and throwing toJSON implementations.
    return '[unrenderable]'
  }
}

/**
 * Truncate an untrusted value for display. Returns null when there is nothing to
 * show, so callers render their own placeholder.
 */
export function short(value: unknown, len: number): string | null {
  if (value === undefined || value === null) return null
  const s = safeText(value)
  return s.length === 0 ? null : s.slice(0, len)
}

export function normalizeUsername(name: unknown): string {
  if (name === undefined || name === null) return 'fleet-bus'
  const s = safeText(name)
  if (s.length === 0) return 'fleet-bus'
  const clean = s.slice(0, 80).replace(/[^\w\s.\-]/g, '')
  return clean || 'fleet-bus'
}

export function format(subject: string, parsed: unknown): string {
  if (isEnvelope(parsed)) {
    // `to` and `kind` are interpolated into the template below, so they need the
    // same treatment as id/in_reply_to — interpolation is a primitive conversion.
    const from = parsed.from === undefined ? '?' : safeText(parsed.from)
    const to = parsed.to === undefined ? '*' : safeText(parsed.to)
    const kind = parsed.kind === undefined ? '?' : safeText(parsed.kind)
    const replyShort = short(parsed.in_reply_to, 8)
    const reply = replyShort ? ` ↪️ \`${replyShort}\`` : ''
    const idShort = short(parsed.id, 8)
    const idPart = idShort ? `\`${idShort}\`` : '`?`'
    let payloadStr = ''
    if (parsed.payload !== undefined) {
      try {
        const j = JSON.stringify(parsed.payload)
        payloadStr = typeof j === 'string' && j.length > 400 ? j.slice(0, 400) + '…' : (j ?? '')
      } catch {
        payloadStr = safeText(parsed.payload)
      }
    }
    return `**${from}** → **${to}** \`${kind}\` on \`${subject}\`${reply} ${idPart}` +
      (payloadStr ? `\n\`\`\`json\n${payloadStr}\n\`\`\`` : '')
  }
  const bot = subject.split('.')[1] ?? '?'
  let summary: string
  if (typeof parsed === 'object' && parsed !== null) {
    try {
      summary = Object.entries(parsed).slice(0, 4).map(([k, v]) => `${k}=${safeText(v)}`).join(' ')
    } catch {
      summary = '[unrenderable]'
    }
  } else {
    summary = safeText(parsed)
  }
  return `_**${bot}** (raw on \`${subject}\`)_ ${summary}`
}

export function dedupeKey(subject: string, parsed: unknown): string {
  // The id was interpolated here BEFORE the try block below, so a hostile id
  // killed the request loop before format() was ever reached (Ohm, PR #67).
  if (isEnvelope(parsed) && parsed.id) return `env:${safeText(parsed.id)}`
  try {
    const j = JSON.stringify(parsed)
    return `raw:${subject}:${typeof j === 'string' ? j : safeText(parsed)}`
  } catch {
    return `raw:${subject}:${safeText(parsed)}`
  }
}

/**
 * For .status subjects only: a "meaningful state" hash that ignores volatile
 * liveness timestamps but changes on transitions the human cares about
 * (online flag, pid, plugin_version, error presence).
 */
export function statusStateHash(subject: string, parsed: unknown): string | null {
  if (!subject.endsWith('.status')) return null
  if (typeof parsed !== 'object' || parsed === null) return null
  const p = parsed as Record<string, unknown>
  const inner = isEnvelope(parsed) && typeof p.payload === 'object' && p.payload !== null
    ? (p.payload as Record<string, unknown>)
    : p
  try {
    const j = JSON.stringify({
      subject,
      online: inner.online ?? null,
      pid: inner.pid ?? null,
      version: inner.plugin_version ?? inner.version ?? null,
      error: inner.error ?? null,
      error_state: inner.error_state ?? null,
    })
    // A field holding a circular or unserialisable value would otherwise throw
    // here and kill the loop on a .status message.
    return typeof j === 'string' ? j : null
  } catch {
    return null
  }
}
