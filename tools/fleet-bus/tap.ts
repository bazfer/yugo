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
import { postOne, validateTimeout } from './tap-post'
import { newState, observeStatus, tick, render } from './tap-watch'

const sc = StringCodec()
const NATS_URL = process.env.FLEET_BUS_URL || 'nats://127.0.0.1:4222'
const PASS = process.env.FLEET_BUS_CONSOLE_PASS
const WEBHOOK = process.env.FLEET_BUS_WEBHOOK_URL
const TOKEN = process.env.DISCORD_BOT_TOKEN
const CHANNEL = process.env.FLEET_BUS_CHANNEL || '1541513867127955626'
const POST_TIMEOUT_MS = validateTimeout(process.env.FLEET_BUS_POST_TIMEOUT_MS, 15_000)

if (!PASS) throw new Error('FLEET_BUS_CONSOLE_PASS required')
if (!WEBHOOK && !TOKEN) throw new Error('FLEET_BUS_WEBHOOK_URL (preferred) or DISCORD_BOT_TOKEN required')
// POST_TIMEOUT_MS is validated above by validateTimeout(), which throws here at
// startup rather than letting AbortSignal.timeout throw per message. See
// tap-post.ts for why that distinction matters.

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

const POST_CONFIG = { webhook: WEBHOOK, token: TOKEN, channel: CHANNEL, timeoutMs: POST_TIMEOUT_MS }

async function drain() {
  if (draining) return
  draining = true
  try {
    while (queue.length > 0) {
      const item = queue.shift()!
      // postOne never throws — see tap-post.ts. The timeout inside it is what
      // stops a hung POST holding `draining` true forever, which would make every
      // later drain() return immediately and stop the queue silently.
      const outcome = await postOne(item, POST_CONFIG, fetch as never)
      if (!outcome.ok) process.stderr.write(`[tap] ${outcome.error}\n`)
      await new Promise((r) => setTimeout(r, 250))
    }
  } finally {
    // finally, not a trailing assignment: if anything above escapes anyway,
    // `draining` must not be left true — that is the stuck-queue state itself.
    draining = false
  }
}

const lastStatusHash = new Map<string, string>()

/**
 * DEAF-BUS DETECTION.
 *
 * A bot can be present and not doing its job, with no signal. bot.py catches a refused
 * dedup store, logs "bus is DEAF", and retries forever. The heartbeat starts inside
 * connect(), so a bot that never connects emits NOTHING. From outside it looks like a
 * healthy idle bot.
 *
 * The signal is already on the wire. Until now nothing consumed it.
 *
 * OFF BY DEFAULT. An unset FLEET_BUS_WATCH_BOTS means no watching and no new traffic.
 * The list cannot be read from the fleet manifest because that file is not mounted into
 * this container, and bot_names there includes entities that are not on the bus. Keep
 * this list in step with the manifest by hand.
 */
const WATCH_BOTS = (process.env.FLEET_BUS_WATCH_BOTS || '')
  .split(',').map((s) => s.trim()).filter(Boolean)
const HEARTBEAT_MS = 30_000
const WATCH_CONFIG = {
  expected: WATCH_BOTS,
  silenceMs: Number(process.env.FLEET_BUS_SILENCE_MS ?? HEARTBEAT_MS * 3),
  graceMs: Number(process.env.FLEET_BUS_GRACE_MS ?? HEARTBEAT_MS * 3),
  reminderMs: Number(process.env.FLEET_BUS_REMINDER_MS ?? 24 * 60 * 60 * 1000),
}
// How often the detector evaluates. It was pinned to the heartbeat interval, which made
// a 1.5s silence window undetectable for 30 seconds -- the evaluation period, not the
// window, bounds how fast anything is noticed. Keep it at or below the silence window.
const TICK_MS = Number(process.env.FLEET_BUS_TICK_MS ?? Math.min(HEARTBEAT_MS, WATCH_CONFIG.silenceMs))
// Validate ONCE, at startup, for the same reason the POST timeout is validated here.
// A NaN silence window makes `now - seen >= NaN` false forever, which silently DISABLES
// detection for every bot that has been seen -- a monitor that reports nothing while
// looking configured. A zero reminder interval spams. (Ohm, PR 68.)
for (const [name, value] of Object.entries({
  FLEET_BUS_SILENCE_MS: WATCH_CONFIG.silenceMs,
  FLEET_BUS_GRACE_MS: WATCH_CONFIG.graceMs,
  FLEET_BUS_REMINDER_MS: WATCH_CONFIG.reminderMs,
  FLEET_BUS_TICK_MS: TICK_MS,
})) {
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error(`${name} must be a positive integer number of milliseconds, got ${JSON.stringify(value)}`)
  }
}

const watch = newState(Date.now())

function announce(text: string) {
  queue.push({ content: text, username: 'fleet-bus-tap' })
  drain().catch((e) => process.stderr.write(`[tap] drain error: ${e}\n`))
}

if (WATCH_BOTS.length > 0) {
  // The tap's own liveness, on the surface a human reads. Without it, a tap that never
  // starts is indistinguishable from a quiet fleet -- the same defect it exists to find.
  announce(`👁️ tap up, watching ${WATCH_BOTS.length} bots: ${WATCH_BOTS.join(', ')}`)

  // TRACK CONNECTION EVENTS, DO NOT POLL isClosed().
  //
  // isClosed() stays FALSE while the client is reconnecting, so polling it left
  // watch.connected true through an outage and the detector reported every bot silent
  // -- the exact false positive the gate exists to prevent. Reproduced against a real
  // broker by Ohm (PR 68).
  //
  // The event stream also catches an outage that begins AND ends between two ticks. A
  // poll cannot see that at all, and it is the case that matters: a brief reconnect
  // leaves a gap in status messages that would otherwise read as silence.
  ;(async () => {
    for await (const s of nc.status()) {
      if (s.type === 'disconnect') {
        watch.connected = false
        process.stderr.write('[tap] watch: nats disconnect\n')
      } else if (s.type === 'reconnect') {
        watch.connected = true
        process.stderr.write('[tap] watch: nats reconnect\n')
      }
    }
  })().catch((e) => process.stderr.write(`[tap] status stream error: ${e}\n`))

  setInterval(() => {
    for (const ev of tick(watch, WATCH_CONFIG, Date.now())) {
      announce(render(ev, Date.now()))
      process.stderr.write(`[tap] watch: ${JSON.stringify(ev)}\n`)
    }
  }, TICK_MS).unref?.()
}

const sub = nc.subscribe('fleet.>')
process.stderr.write(`[tap] subscribed to fleet.>\n`)
for await (const msg of sub) {
  // Error boundary. Every helper below is written to be total, but this loop is
  // the tap's only thread of observation: if ANYTHING in here escapes, the
  // for-await ends and the tap stops watching the bus entirely. Belt and braces,
  // because the cost of being wrong is the component that notices silence going
  // silent. `continue` inside the try still continues this loop.
  try {
  let parsed: unknown
  try {
    parsed = JSON.parse(sc.decode(msg.data))
  } catch {
    parsed = sc.decode(msg.data)
  }

  // Observe BEFORE the dedupe below. A bot whose status is unchanged is still alive, and
  // the dedupe deliberately drops those -- so recording after it would read a healthy,
  // steady bot as silent.
  const statusBot = msg.subject.endsWith('.status') ? msg.subject.split('.')[1] : null
  if (statusBot) observeStatus(watch, statusBot, Date.now())

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
  // The helpers never rely on primitive conversion — see safeText in
  // tap-format.ts. A numeric `id`, or an object that shadows toString, used to
  // raise an uncaught TypeError here and kill the process.
  queue.push({ content: format(msg.subject, parsed), username: normalizeUsername(from) })
  drain().catch((e) => process.stderr.write(`[tap] drain error: ${e}\n`))
  } catch (err) {
    // Drop this one message, keep observing. Log the subject, because the
    // envelope is by assumption the thing we could not render — the subject is
    // what identifies the offending publisher.
    process.stderr.write(`[tap] dropped a message on ${msg.subject}: ${err}\n`)
  }
}
