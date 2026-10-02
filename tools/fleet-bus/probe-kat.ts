/**
 * GATE 1 probe: publish a text_message envelope from Luna to Kat.
 * Kat's Claude Code plugin should inject it as a <channel source="fleet-bus"> frame.
 */
import { connect, StringCodec } from 'nats'
import { randomUUID } from 'node:crypto'

const sc = StringCodec()
const url = process.env.FLEET_BUS_URL || 'nats://127.0.0.1:4222'
const user = process.env.FLEET_BUS_USER || 'luna'
const pass = process.env.FLEET_BUS_PASS

if (!pass) throw new Error('FLEET_BUS_PASS required')

const nc = await connect({ servers: url, user, pass, name: `probe-${user}` })
process.stderr.write(`[probe] connected as ${user}\n`)

const env = {
  envelope_version: 1,
  id: randomUUID(),
  from: 'luna',
  to: 'kat',
  kind: 'text_message',
  ts: new Date().toISOString(),
  payload: {
    text: 'GATE 1 probe from Luna. If this shows up in your transcript with a <channel source="fleet-bus"> frame, reply with envelope kind="text_message" to fleet.luna.request, payload {text: "ack from kat, in_reply_to your id"}, and set in_reply_to to my id. My id is above.',
  },
}

nc.publish('fleet.kat.request', sc.encode(JSON.stringify(env)))
process.stderr.write(`[probe] published id=${env.id}\n`)
console.log(JSON.stringify({ published_id: env.id, subject: 'fleet.kat.request' }))
await nc.drain()
