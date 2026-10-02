/**
 * GATE 1 listener: subscribe as luna, tee any received envelope to stdout + log file.
 */
import { connect, StringCodec } from 'nats'

const sc = StringCodec()
const url = process.env.FLEET_BUS_URL || 'nats://127.0.0.1:4222'
const user = 'luna'
const pass = process.env.FLEET_BUS_PASS
if (!pass) throw new Error('FLEET_BUS_PASS required')

const nc = await connect({ servers: url, user, pass, name: `listen-${user}` })
process.stderr.write(`[listen] connected as ${user}\n`)

const sub = nc.subscribe('fleet.luna.request')
for await (const msg of sub) {
  const body = sc.decode(msg.data)
  const line = `[${new Date().toISOString()}] ${msg.subject} :: ${body}\n`
  process.stdout.write(line)
  await Bun.write(Bun.file('/tmp/luna-bus-inbox.log'), line, { createPath: true })
}
