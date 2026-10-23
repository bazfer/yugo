import { test, expect, beforeEach, afterEach, spyOn } from 'bun:test'
import { connect, JSONCodec, type NatsConnection } from 'nats'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { FleetBus } from './fleet-bus'

import * as monotonic from './monotonic-clock'
// Subscription/broker behavior, not native-clock qualification.
let clockSpy: ReturnType<typeof spyOn>
beforeEach(() => { clockSpy = spyOn(monotonic, 'readMonotonicMs').mockImplementation(() => Math.floor(performance.now())) })
afterEach(() => clockSpy.mockRestore())

const jc = JSONCodec<any>()
const binary = Bun.which('nats-server')
const brokerTest = !binary && !process.env.CI ? test.skip : test
async function fixture(refused: string[]) {
  if (!binary) throw new Error('real nats-server required (CI must install it)')
  const dir = mkdtempSync(join(tmpdir(), 'subscribe-health-'))
  const reservation = Bun.listen({ hostname: '127.0.0.1', port: 0, socket: { data() {} } })
  const port = reservation.port
  reservation.stop(true)
  writeFileSync(join(dir, 'nats.conf'), `port: ${port}
authorization { users: [
 {user: yugo, password: test, permissions: {publish: ["fleet.>"], subscribe: {allow: ["fleet.yugo.>", "fleet.broadcast.>"], deny: ${JSON.stringify(refused)}}}},
 {user: vec, password: test}
] }`)
  let proc = Bun.spawn([binary, '-c', join(dir, 'nats.conf')], { stdout: 'ignore', stderr: 'ignore' })
  let peer: NatsConnection | undefined
  try {
    for (let i = 0; i < 100; i++) {
      try { peer = await connect({ servers: `127.0.0.1:${port}`, user: 'vec', pass: 'test', maxReconnectAttempts: 0 }); break }
      catch { await Bun.sleep(10) }
    }
    if (!peer) throw new Error('broker did not start')
    const beats: any[] = [], events: any[] = [], audits: any[] = []
    peer.subscribe('fleet.yugo.status', { callback: (_, msg) => beats.push(jc.decode(msg.data)) })
    await peer.flush()
    const bus = new FleetBus({ botName: 'yugo', user: 'yugo', password: 'test', url: `nats://127.0.0.1:${port}`,
      dedupStorePath: ':memory:', heartbeatIntervalMs: 20,
      connectFn: options => connect({ ...options, reconnectTimeWait: 20 }),
      injectIntoSession: async event => { events.push(event) },
      logger: message => { try { audits.push(JSON.parse(message.slice('FleetBus: '.length))) } catch {} },
    }, new Set(['yugo', 'vec']))
    await bus.connect()
    return { bus, peer, beats, events, audits,
      async restartWithRefusal(subject: string) {
        proc.kill(); await proc.exited
        const { readFileSync } = await import('node:fs')
        const file = join(dir, 'nats.conf')
        writeFileSync(file, readFileSync(file, 'utf8').replace('deny: []', `deny: ${JSON.stringify([subject])}`))
        proc = Bun.spawn([binary, '-c', file], { stdout: 'ignore', stderr: 'ignore' })
      },
      async close() {
      await bus.disconnect(); await peer!.close(); proc.kill(); await proc.exited
      rmSync(dir, { recursive: true, force: true })
    } }
  } catch (error) {
    await peer?.close(); proc.kill(); await proc.exited; rmSync(dir, { recursive: true, force: true }); throw error
  }
}

brokerTest('refused SUB is unhealthy, terminal and does not heartbeat', async () => {
  const f = await fixture(['fleet.yugo.request'])
  try {
    await Bun.sleep(100)
    expect((f.bus.busStatus() as any).healthy).toBe(false)
    expect((f.bus.busStatus() as any).state).toBe('unhealthy')
    expect(f.beats).toHaveLength(0)
    const drops = f.audits.filter(a => a.reason === 'claude_discord_adapter_subscribe_refused')
    expect(drops).toHaveLength(1)
    expect(drops[0]).toMatchObject({ subject: 'fleet.yugo.request', disposition: 'terminal' })
  } finally { await f.close() }
})
brokerTest('one refused of several is named and other subscriptions still deliver', async () => {
  const f = await fixture(['fleet.yugo.result'])
  try {
    expect((f.bus.busStatus() as any).subjects_refused).toEqual(['fleet.yugo.result'])
    f.bus.busStatus().subjects_refused.length = 0
    expect(f.bus.busStatus().subjects_refused).toEqual(['fleet.yugo.result'])
    f.peer.publish('fleet.yugo.request', jc.encode({ envelope_version: 1, id: 'survivor', from: 'vec', to: 'yugo',
      kind: 'text_message', ts: new Date().toISOString(), payload: {} }))
    await f.peer.flush()
    for (let i = 0; i < 100 && !f.events.length; i++) await Bun.sleep(10)
    expect(f.events.map(e => e.envelope.id)).toEqual(['survivor'])
  } finally { await f.close() }
})
brokerTest('all granted is healthy with heartbeats and an empty refusal list', async () => {
  const f = await fixture([])
  try {
    await Bun.sleep(80)
    expect(f.bus.busStatus()).toMatchObject({ state: 'connected', healthy: true, subjects_refused: [] })
    expect(f.beats.length).toBeGreaterThan(1)
    expect(f.beats[0].payload.healthy).toBe(true)
  } finally { await f.close() }
})
brokerTest('positive control: actual broker rejects denied subscription but stays connected', async () => {
  const f = await fixture([])
  const errors: any[] = []
  try {
    const nc: NatsConnection = (f.bus as any).nc
    const denied = nc.subscribe('outside.grants', { callback: error => { if (error) errors.push(error) } })
    await nc.flush()
    expect(errors).toHaveLength(1)
    expect(denied.isClosed()).toBe(true)
    expect(nc.isClosed()).toBe(false)
  } finally { await f.close() }
})

brokerTest('refused replay after reconnect stops an already-running heartbeat', async () => {
  const f = await fixture([])
  try {
    await Bun.sleep(70)
    expect(f.beats.length).toBeGreaterThan(0)
    await f.restartWithRefusal('fleet.yugo.request')
    for (let i = 0; i < 300 && f.bus.busStatus().healthy; i++) await Bun.sleep(10)
    for (let i = 0; i < 300 && f.bus.busStatus().state !== 'unhealthy'; i++) await Bun.sleep(10)
    expect(f.bus.busStatus()).toMatchObject({ healthy: false, state: 'unhealthy', subjects_refused: ['fleet.yugo.request'] })
    await Bun.sleep(100)
    const count = f.beats.length
    await Bun.sleep(100)
    expect(f.beats).toHaveLength(count)
    expect(f.audits.filter(a => a.reason === 'claude_discord_adapter_subscribe_refused')).toHaveLength(1)
  } finally { await f.close() }
}, 10000)

test('transient replay flush failure does not abandon status observation', async () => {
  let flushes = 0
  const nc = {
    async *status() { yield { type: 'disconnect' }; yield { type: 'reconnect' }; yield { type: 'reconnect' } },
    async flush() { if (++flushes === 1) throw new Error('second transport outage') },
  }
  const bus: any = Object.create(FleetBus.prototype)
  Object.assign(bus, { nc, state: 'connected', subjectsRefused: new Set(), mode: 'publish-only',
    config: { botName: 'yugo' } })
  await bus.watchConnectionStatus(nc)
  expect(flushes).toBe(2)
  expect(bus.state).toBe('connected')
})
