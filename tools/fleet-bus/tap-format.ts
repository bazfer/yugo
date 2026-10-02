/**
 * Pure formatting and dedupe helpers for the fleet-bus tap.
 *
 * Extracted from tap.ts so they can be imported and tested. tap.ts opens a NATS
 * connection at module scope, so importing it runs the tap — there was no way to
 * exercise these functions at all before this split.
 *
 * Everything here must stay pure: no I/O, no module-scope side effects.
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
 * Truncate an untrusted value for display.
 *
 * The Envelope fields are declared `string`, but that is a COMPILE-TIME claim about
 * a value parsed from JSON off the wire at runtime. A publisher sending `id: 42`
 * produces a number, `.slice` is not a function, and the uncaught TypeError
 * terminated the tap — one malformed envelope from any publisher took down the
 * component that is supposed to notice when things go quiet.
 *
 * Coercing rather than rejecting is deliberate: the tap's job is to show a human
 * what crossed the bus, and a malformed envelope is exactly what they need to see.
 */
export function short(value: unknown, len: number): string | null {
  if (value === undefined || value === null) return null
  const s = typeof value === 'string' ? value : String(value)
  return s.length === 0 ? null : s.slice(0, len)
}

export function normalizeUsername(name: unknown): string {
  if (name === undefined || name === null) return 'fleet-bus'
  const s = typeof name === 'string' ? name : String(name)
  if (s.length === 0) return 'fleet-bus'
  const clean = s.slice(0, 80).replace(/[^\w\s.\-]/g, '')
  return clean || 'fleet-bus'
}

export function format(subject: string, parsed: unknown): string {
  if (isEnvelope(parsed)) {
    const from = parsed.from ?? '?'
    const to = parsed.to ?? '*'
    const kind = parsed.kind ?? '?'
    const replyShort = short(parsed.in_reply_to, 8)
    const reply = replyShort ? ` ↪️ \`${replyShort}\`` : ''
    const idShort = short(parsed.id, 8)
    const idPart = idShort ? `\`${idShort}\`` : '`?`'
    let payloadStr = ''
    if (parsed.payload !== undefined) {
      try {
        const j = JSON.stringify(parsed.payload)
        payloadStr = j.length > 400 ? j.slice(0, 400) + '…' : j
      } catch {
        payloadStr = String(parsed.payload)
      }
    }
    return `**${from}** → **${to}** \`${kind}\` on \`${subject}\`${reply} ${idPart}` +
      (payloadStr ? `\n\`\`\`json\n${payloadStr}\n\`\`\`` : '')
  }
  const bot = subject.split('.')[1] ?? '?'
  const summary = typeof parsed === 'object' && parsed !== null
    ? Object.entries(parsed).slice(0, 4).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(' ')
    : String(parsed)
  return `_**${bot}** (raw on \`${subject}\`)_ ${summary}`
}

export function dedupeKey(subject: string, parsed: unknown): string {
  if (isEnvelope(parsed) && parsed.id) return `env:${parsed.id}`
  try {
    return `raw:${subject}:${JSON.stringify(parsed)}`
  } catch {
    return `raw:${subject}:${String(parsed)}`
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
  return JSON.stringify({
    subject,
    online: inner.online ?? null,
    pid: inner.pid ?? null,
    version: inner.plugin_version ?? inner.version ?? null,
    error: inner.error ?? null,
    error_state: inner.error_state ?? null,
  })
}
