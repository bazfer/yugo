/**
 * fleet-bus supervision tap — subscribes to fleet.> as the `console` NATS
 * user (read-only, deny-all publish) and mirrors every envelope to Discord
 * channel #fleet-bus so a human can watch bus traffic in real time.
 *
 * Env:
 *   FLEET_BUS_URL         nats server (default nats://127.0.0.1:4222)
 *   FLEET_BUS_CONSOLE_PASS  nats console user password (required)
 *   DISCORD_BOT_TOKEN      bot token with SendMessages in FLEET_BUS_CHANNEL (required)
 *   FLEET_BUS_CHANNEL      discord channel id (default #fleet-bus 1541513867127955626)
 *
 * Rate limit: Discord allows 5 msgs/sec/channel. We queue and drain at ≤4/sec.
 */
import { connect, StringCodec } from 'nats'

const sc = StringCodec()
const NATS_URL = process.env.FLEET_BUS_URL || 'nats://127.0.0.1:4222'
const PASS = process.env.FLEET_BUS_CONSOLE_PASS
const WEBHOOK = process.env.FLEET_BUS_WEBHOOK_URL
const TOKEN = process.env.DISCORD_BOT_TOKEN
const CHANNEL = process.env.FLEET_BUS_CHANNEL || '1541513867127955626'

if (!PASS) throw new Error('FLEET_BUS_CONSOLE_PASS required')
if (!WEBHOOK && !TOKEN) throw new Error('FLEET_BUS_WEBHOOK_URL (preferred) or DISCORD_BOT_TOKEN required')

const nc = await connect({ servers: NATS_URL, user: 'console', pass: PASS, name: 'fleet-bus-tap' })
process.stderr.write(`[tap] connected to ${NATS_URL} as console\n`)

interface Envelope {
  envelope_version?: number
  id?: string
  from?: string
  to?: string
  kind?: string
  in_reply_to?: string
  ts?: string
  payload?: unknown
}

function isEnvelope(o: unknown): o is Envelope {
  return typeof o === 'object' && o !== null && ('envelope_version' in o || ('from' in o && 'kind' in o))
}

const recent = new Map<string, number>()
const DEDUPE_WINDOW_MS = 30_000

interface QueueItem {
  content: string
  username: string
}

const queue: QueueItem[] = []
let draining = false

function normalizeUsername(name: string | undefined | null): string {
  if (!name) return 'fleet-bus'
  const clean = name.slice(0, 80).replace(/[^\w\s.\-]/g, '')
  return clean || 'fleet-bus'
}

async function drain() {
  if (draining) return
  draining = true
  while (queue.length > 0) {
    const item = queue.shift()!
    try {
      const url = WEBHOOK
        ? WEBHOOK
        : `https://discord.com/api/v10/channels/${CHANNEL}/messages`
      const headers: Record<string, string> = { 'Content-Type': 'application/json' }
      if (!WEBHOOK) headers['Authorization'] = `Bot ${TOKEN}`
      const body = WEBHOOK
        ? JSON.stringify({ content: item.content, username: item.username })
        : JSON.stringify({ content: item.content })
      const res = await fetch(url, { method: 'POST', headers, body })
      if (!res.ok) {
        process.stderr.write(`[tap] discord post failed ${res.status}: ${await res.text()}\n`)
      }
    } catch (err) {
      process.stderr.write(`[tap] discord post error: ${err}\n`)
    }
    await new Promise((r) => setTimeout(r, 250))
  }
  draining = false
}

function format(subject: string, parsed: unknown): string {
  if (isEnvelope(parsed)) {
    const from = parsed.from ?? '?'
    const to = parsed.to ?? '*'
    const kind = parsed.kind ?? '?'
    const reply = parsed.in_reply_to ? ` ↪️ \`${parsed.in_reply_to.slice(0, 8)}\`` : ''
    const idShort = parsed.id ? `\`${parsed.id.slice(0, 8)}\`` : '`?`'
    let payloadStr = ''
    if (parsed.payload !== undefined) {
      try {
        const j = JSON.stringify(parsed.payload)
        payloadStr = j.length > 400 ? j.slice(0, 400) + '…' : j
      } catch {
        payloadStr = String(parsed.payload)
      }
    }
    return `**${from}** → **${to}** \`${kind}\` on \`${subject}\`${reply} ${idShort}` +
      (payloadStr ? `\n\`\`\`json\n${payloadStr}\n\`\`\`` : '')
  }
  const bot = subject.split('.')[1] ?? '?'
  const summary = typeof parsed === 'object' && parsed !== null
    ? Object.entries(parsed).slice(0, 4).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(' ')
    : String(parsed)
  return `_**${bot}** (raw on \`${subject}\`)_ ${summary}`
}

function dedupeKey(subject: string, parsed: unknown): string {
  if (isEnvelope(parsed) && parsed.id) return `env:${parsed.id}`
  try {
    return `raw:${subject}:${JSON.stringify(parsed)}`
  } catch {
    return `raw:${subject}:${String(parsed)}`
  }
}

/**
 * For .status subjects only: compute a "meaningful state" hash that ignores
 * volatile liveness timestamps but changes on transitions the human cares
 * about (online flag, pid, plugin_version, error presence).
 */
function statusStateHash(subject: string, parsed: unknown): string | null {
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

const lastStatusHash = new Map<string, string>()

const sub = nc.subscribe('fleet.>')
process.stderr.write(`[tap] subscribed to fleet.>\n`)
for await (const msg of sub) {
  let parsed: unknown
  try {
    parsed = JSON.parse(sc.decode(msg.data))
  } catch {
    parsed = sc.decode(msg.data)
  }

  const stateHash = statusStateHash(msg.subject, parsed)
  if (stateHash !== null) {
    const prior = lastStatusHash.get(msg.subject)
    if (prior === stateHash) continue
    lastStatusHash.set(msg.subject, stateHash)
  } else {
    const key = dedupeKey(msg.subject, parsed)
    const now = Date.now()
    const last = recent.get(key) ?? 0
    if (now - last < DEDUPE_WINDOW_MS) continue
    recent.set(key, now)
    if (recent.size > 500) {
      const cutoff = now - DEDUPE_WINDOW_MS
      for (const [k, t] of recent) if (t < cutoff) recent.delete(k)
    }
  }

  const from = isEnvelope(parsed) ? parsed.from : (msg.subject.split('.')[1] ?? null)
  queue.push({ content: format(msg.subject, parsed), username: normalizeUsername(from) })
  drain().catch((e) => process.stderr.write(`[tap] drain error: ${e}\n`))
}
