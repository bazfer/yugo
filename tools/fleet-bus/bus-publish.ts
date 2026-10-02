/**
 * Bus publisher — shell it out to publish an envelope on the fleet-bus while
 * we wait on Stage 4 (first-class MCP publish tools). Usage:
 *
 *   FLEET_BUS_USER=deet FLEET_BUS_TOKEN_FILE=/home/deet/.claude/fleet-bus-token-deet \
 *     bun /home/luna/fleet-bus/bus-publish.ts \
 *       --to luna \
 *       --kind text_message \
 *       --payload '{"text":"hey luna"}'
 *
 * Flags:
 *   --to <bot>          target bot name (goes into envelope + routes to fleet.<bot>.request)
 *   --kind <str>        envelope kind (default text_message)
 *   --payload <json>    JSON payload (required)
 *   --subject <str>     override target subject (default: fleet.<to>.request)
 *   --in-reply-to <id>  envelope id being replied to
 *   --broadcast         publish to fleet.broadcast.<kind> instead of a specific bot
 *
 * Reads NATS user from $FLEET_BUS_USER, password from $FLEET_BUS_TOKEN_FILE.
 * Prints the published envelope id to stdout as JSON.
 */
import { connect, StringCodec } from 'nats'
import { randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'

const sc = StringCodec()

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(`--${name}`)
  return i >= 0 && i + 1 < process.argv.length ? process.argv[i + 1] : undefined
}
const flag = (name: string) => process.argv.includes(`--${name}`)

const user = process.env.FLEET_BUS_USER
const tokenFile = process.env.FLEET_BUS_TOKEN_FILE
if (!user || !tokenFile) {
  console.error('FLEET_BUS_USER and FLEET_BUS_TOKEN_FILE required')
  process.exit(2)
}
const pass = readFileSync(tokenFile, 'utf8').trim()

const to = arg('to')
const kind = arg('kind') ?? 'text_message'
const payloadStr = arg('payload')
const inReplyTo = arg('in-reply-to')
const broadcast = flag('broadcast')
let subject = arg('subject')

if (!payloadStr) {
  console.error('--payload required (JSON string)')
  process.exit(2)
}
if (!to && !broadcast) {
  console.error('--to <bot> required unless --broadcast')
  process.exit(2)
}

if (!subject) {
  subject = broadcast ? `fleet.broadcast.${kind}` : `fleet.${to}.request`
}

const nc = await connect({
  servers: process.env.FLEET_BUS_URL || 'nats://127.0.0.1:4222',
  user,
  pass,
  name: `publish-${user}`,
  inboxPrefix: `_INBOX_${user}`,
})

const env = {
  envelope_version: 1,
  id: randomUUID(),
  from: user,
  to: broadcast ? null : to,
  kind,
  ...(inReplyTo ? { in_reply_to: inReplyTo } : {}),
  ts: new Date().toISOString(),
  payload: JSON.parse(payloadStr),
}

nc.publish(subject, sc.encode(JSON.stringify(env)))
console.log(JSON.stringify({ published_id: env.id, subject, from: user }))
await nc.drain()
