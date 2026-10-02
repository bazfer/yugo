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
 *   FLEET_BUS_POST_TIMEOUT_MS  discord post timeout (default 15000)
 *
 * Rate limit: Discord allows 5 msgs/sec/channel. We queue and drain at ≤4/sec.
 *
 * The formatting and dedupe helpers live in ./tap-format.ts so they can be
 * imported and tested — this file connects to NATS at module scope, so importing
 * it runs the tap.
 */
import { connect, StringCodec } from 'nats'
import { isEnvelope, normalizeUsername, format, dedupeKey, statusStateHash } from './tap-format'

const sc = StringCodec()
const NATS_URL = process.env.FLEET_BUS_URL || 'nats://127.0.0.1:4222'
const PASS = process.env.FLEET_BUS_CONSOLE_PASS
const WEBHOOK = process.env.FLEET_BUS_WEBHOOK_URL
const TOKEN = process.env.DISCORD_BOT_TOKEN
const CHANNEL = process.env.FLEET_BUS_CHANNEL || '1541513867127955626'
const POST_TIMEOUT_MS = Number(process.env.FLEET_BUS_POST_TIMEOUT_MS ?? 15_000)

if (!PASS) throw new Error('FLEET_BUS_CONSOLE_PASS required')
if (!WEBHOOK && !TOKEN) throw new Error('FLEET_BUS_WEBHOOK_URL (preferred) or DISCORD_BOT_TOKEN required')

/**
 * maxReconnectAttempts: -1 — reconnect forever.
 *
 * The nats client defaults to 10 attempts at 2s. A NATS outage longer than ~20s
 * closed the client, ended the `for await` below, and exited the process. Docker
 * restarted it, so this looked survivable — but every restart discards the
 * in-memory dedupe and status state, and a supervision tap that quietly resets
 * its own memory is a supervision tap that misses transitions.
 */
const nc = await connect({
  servers: NATS_URL,
  user: 'console',
  pass: PASS,
  name: 'fleet-bus-tap',
  maxReconnectAttempts: -1,
})
process.stderr.write(`[tap] connected to ${NATS_URL} as console\n`)

const recent = new Map<string, number>()
const DEDUPE_WINDOW_MS = 30_000

interface QueueItem {
  content: string
  username: string
}

const queue: QueueItem[] = []
let draining = false

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
      // A POST with no timeout can hang forever. `draining` is still true while it
      // hangs, so every later drain() returns immediately and the queue stops
      // moving — silently, with no error and no sign anything is wrong.
      const res = await fetch(url, {
        method: 'POST',
        headers,
        body,
        signal: AbortSignal.timeout(POST_TIMEOUT_MS),
      })
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
  // format() and normalizeUsername() both coerce rather than assume string —
  // see tap-format.ts. A publisher sending a numeric `id`, `in_reply_to` or
  // `from` used to raise an uncaught TypeError here and kill the process.
  queue.push({ content: format(msg.subject, parsed), username: normalizeUsername(from) })
  drain().catch((e) => process.stderr.write(`[tap] drain error: ${e}\n`))
}
