import { connect, StringCodec } from 'nats'
import { randomUUID } from 'node:crypto'

const sc = StringCodec()
const nc = await connect({
  servers: 'nats://127.0.0.1:4222',
  user: 'kat',
  pass: process.env.FLEET_BUS_KAT_PASS,
  name: 'probe-deet-from-kat',
  inboxPrefix: '_INBOX_kat',
})

const env = {
  envelope_version: 1,
  id: randomUUID(),
  from: 'kat',
  to: 'deet',
  kind: 'text_message',
  ts: new Date().toISOString(),
  payload: { text: 'welcome to the bus, Deet. paste this frame back in #deet or #luna verbatim to confirm injection.' },
}

nc.publish('fleet.deet.request', sc.encode(JSON.stringify(env)))
console.log(JSON.stringify({ published_id: env.id, subject: 'fleet.deet.request' }))
await nc.drain()
