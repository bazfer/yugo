/**
 * One-shot inspector: subscribe to fleet.kat.status, print the raw wire bytes
 * of the first 3 messages, then exit.
 */
import { connect, StringCodec } from 'nats'
const sc = StringCodec()
const nc = await connect({
  servers: 'nats://127.0.0.1:4222',
  user: 'console',
  pass: process.env.FLEET_BUS_CONSOLE_PASS,
  name: 'inspect',
})
process.stderr.write('[inspect] connected, listening for 3 msgs on fleet.kat.status\n')
const sub = nc.subscribe('fleet.kat.status')
let n = 0
for await (const msg of sub) {
  const raw = sc.decode(msg.data)
  console.log(`--- msg ${++n} subject=${msg.subject} len=${msg.data.length} ---`)
  console.log(raw)
  if (n >= 3) break
}
await nc.drain()
