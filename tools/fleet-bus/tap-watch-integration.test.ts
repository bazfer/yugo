/**
 * The detector, wired, against a real broker.
 *
 * WHY. Ohm found that REMOVING observeStatus() from tap.ts left all 84 tests passing
 * (PR 68). tap-watch.test.ts exercises the module directly; tap-integration.test.ts runs
 * with the detector OFF. So nothing tested the wiring between them.
 *
 * That is the third time this shape has appeared in this file's history: the import that
 * was never added, the entrypoint that was never run, and now the observer that was never
 * called. Each time the units passed and the thing that uses them was untested.
 */
import { expect, test, describe, beforeAll, afterAll } from 'bun:test'
import { connect, StringCodec } from 'nats'

const ENTRY = new URL('./tap.ts', import.meta.url).pathname
const NATS_PORT = 15222 + Math.floor(Math.random() * 500)
const HTTP_PORT = 19080 + Math.floor(Math.random() * 500)
const NATS_URL = `nats://127.0.0.1:${NATS_PORT}`

let natsProc: ReturnType<typeof Bun.spawn> | null = null
let tapProc: ReturnType<typeof Bun.spawn> | null = null
let httpServer: ReturnType<typeof Bun.serve> | null = null
const posted: string[] = []

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
const sawText = (frag: string) => posted.some((c) => (c || '').includes(frag))

beforeAll(async () => {
  natsProc = Bun.spawn(['nats-server', '-p', String(NATS_PORT)], { stdout: 'pipe', stderr: 'pipe' })
  httpServer = Bun.serve({
    port: HTTP_PORT,
    async fetch(req) {
      try { posted.push((await req.json()).content) } catch { posted.push('') }
      return new Response('', { status: 204 })
    },
  })
  await sleep(600)

  tapProc = Bun.spawn(['bun', ENTRY], {
    env: {
      ...process.env,
      FLEET_BUS_URL: NATS_URL,
      FLEET_BUS_CONSOLE_PASS: 'ignored-no-auth-configured',
      FLEET_BUS_WEBHOOK_URL: `http://127.0.0.1:${HTTP_PORT}/hook`,
      FLEET_BUS_POST_TIMEOUT_MS: '3000',
      // Short windows so a real transition happens inside a test, not in 90 seconds.
      FLEET_BUS_WATCH_BOTS: 'vec,ohm',
      FLEET_BUS_SILENCE_MS: '1500',
      FLEET_BUS_GRACE_MS: '1500',
      FLEET_BUS_REMINDER_MS: '86400000',
    },
    stdout: 'pipe',
    stderr: 'pipe',
  })
  await sleep(1500)
})

afterAll(async () => {
  tapProc?.kill(); natsProc?.kill(); httpServer?.stop(true)
  await sleep(200)
})

describe('the detector, wired', () => {
  test('the tap announces what it is watching', () => {
    expect(sawText('tap up, watching')).toBe(true)
    expect(sawText('vec')).toBe(true)
  })

  test('a bot that never publishes status is reported silent', async () => {
    // The failure the detector exists for: a bot that never connected, so it never
    // appears on the bus at all.
    for (let i = 0; i < 60 && !sawText('is silent on the bus'); i++) await sleep(200)
    expect(sawText('is silent on the bus')).toBe(true)
    expect(sawText('**vec**')).toBe(true)
  }, 25_000)

  test('a bot that publishes status RECOVERS, which proves observeStatus is wired', async () => {
    // THE regression for Ohm's finding. Deleting observeStatus() from tap.ts leaves the
    // unit tests and the other integration suite green; this is the only case that sees
    // it, because recovery can only happen if the status was actually observed.
    const nc = await connect({ servers: NATS_URL, name: 'watch-int' })
    const sc = StringCodec()
    for (let i = 0; i < 12 && !sawText('is back on the bus'); i++) {
      nc.publish('fleet.vec.status', sc.encode(JSON.stringify({ online: true, pid: 1, n: i })))
      await nc.flush()
      await sleep(400)
    }
    await nc.close()
    expect(sawText('is back on the bus')).toBe(true)
  }, 25_000)

  test('the tap is still alive', () => {
    expect(tapProc?.killed).toBeFalsy()
  })

  // LAST: this kills the broker, so nothing after it can use the bus.
  test('losing the broker reports the TAP, not the whole fleet', async () => {
    // The deployment blocker Ohm reproduced. Polling nc.isClosed() left watch.connected
    // true throughout an outage, because isClosed() stays false while RECONNECTING. The
    // detector then reported every bot silent — the exact false positive the gate exists
    // to prevent, firing at the worst possible moment.
    //
    // Without this case, disabling the disconnect handler leaves the whole suite green.
    const before = posted.length
    natsProc?.kill()

    for (let i = 0; i < 60 && !sawText('lost its NATS connection'); i++) await sleep(200)

    expect(sawText('lost its NATS connection')).toBe(true)

    // And it must NOT have blamed the bots for the tap's own outage. Checking only up
    // to the notice would miss blame that arrives after it, which is the more likely
    // ordering: the notice fires on the first tick after the disconnect, and any false
    // silence alert fires on the ticks that follow. (Ohm, PR 68.)
    const noticeAt = posted.findIndex((c) => (c || '').includes('lost its NATS connection'))
    await sleep(3000)
    const afterNotice = posted.slice(Math.max(before, noticeAt))
    expect(afterNotice.filter((c) => (c || '').includes('is silent on the bus'))).toEqual([])
  }, 30_000)

  test('restarting the broker reconnects and grants a fresh grace', async () => {
    // The reconnect half. Disabling the reconnect handler left all 89 tests passing,
    // because nothing ever restarted the broker. (Ohm, PR 68.)
    natsProc = Bun.spawn(['nats-server', '-p', String(NATS_PORT)], { stdout: 'pipe', stderr: 'pipe' })
    for (let i = 0; i < 60 && !sawText('back on NATS'); i++) await sleep(250)
    expect(sawText('back on NATS')).toBe(true)

    // Immediately after a reconnect the fresh grace must hold: no bot may be reported
    // silent for the gap the tap itself was absent for.
    //
    // The window MUST be shorter than FLEET_BUS_GRACE_MS (1500ms here). My first version
    // waited 2000ms and failed — correctly. Past the grace a bot that genuinely stopped
    // publishing SHOULD be reported, so a longer window asserts something false and would
    // have been "fixed" by weakening the grace.
    const mark = posted.length
    await sleep(1000)
    const after = posted.slice(mark)
    expect(after.filter((c) => (c || '').includes('is silent on the bus'))).toEqual([])
  }, 40_000)
})
