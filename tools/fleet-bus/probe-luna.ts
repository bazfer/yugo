/**
 * Self-probe: publish an envelope to fleet.luna.request from an external
 * NATS identity (kat). If luna's plugin injects it as a <channel source="fleet-bus">
 * frame on the next turn, we've proven session injection works for luna.
 */
import { connect, StringCodec } from 'nats'
import { randomUUID } from 'node:crypto'

const sc = StringCodec()
const nc = await connect({
  servers: 'nats://127.0.0.1:4222',
  user: 'kat',
  pass: process.env.FLEET_BUS_KAT_PASS,
  name: 'probe-luna-from-kat',
  inboxPrefix: '_INBOX_kat',
})

const env = {
  envelope_version: 1,
  id: randomUUID(),
  from: 'kat',
  to: 'luna',
  kind: 'text_message',
  ts: new Date().toISOString(),
  payload: { text: 'self-injection test — if you see this as a <channel source="fleet-bus"> frame in your transcript, plugin is working end-to-end.' },
}

nc.publish('fleet.luna.request', sc.encode(JSON.stringify(env)))
console.log(JSON.stringify({ published_id: env.id, subject: 'fleet.luna.request' }))
await nc.drain()
