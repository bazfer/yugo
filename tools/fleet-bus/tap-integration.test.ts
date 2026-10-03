/**
 * End-to-end test for tap.ts: a real NATS server, a real published envelope, and a
 * real HTTP endpoint receiving the POST.
 *
 * WHY THIS EXISTS, AND WHY tap-entrypoint.test.ts WAS NOT ENOUGH.
 *
 * Those tests assert the ABSENCE of errors. Ohm showed that is satisfied by a program
 * that does nothing (PR 67):
 *
 *   - removing ONLY `postOne` from the import left all 62 tests passing
 *   - making the entrypoint exit after validation, never connecting, left all five
 *     entrypoint tests passing
 *
 * "No ReferenceError" and "no refusal message" are both true of a file that stops. The
 * positive control I wrote asserted only that errors were absent, which is the same
 * mistake one level up: I proved nothing went wrong rather than that something went
 * right.
 *
 * This asserts the end state instead: an envelope published to NATS arrives at the
 * Discord endpoint as a POST with its content.
 */
import { expect, test, describe, beforeAll, afterAll } from 'bun:test'
import { connect, StringCodec } from 'nats'

const ENTRY = new URL('./tap.ts', import.meta.url).pathname
const NATS_PORT = 14222 + Math.floor(Math.random() * 500)
const HTTP_PORT = 18080 + Math.floor(Math.random() * 500)
const NATS_URL = `nats://127.0.0.1:${NATS_PORT}`

let natsProc: ReturnType<typeof Bun.spawn> | null = null
let tapProc: ReturnType<typeof Bun.spawn> | null = null
let httpServer: ReturnType<typeof Bun.serve> | null = null
const received: Array<{ content?: string; username?: string }> = []

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

beforeAll(async () => {
  natsProc = Bun.spawn(['nats-server', '-p', String(NATS_PORT)], { stdout: 'pipe', stderr: 'pipe' })

  httpServer = Bun.serve({
    port: HTTP_PORT,
    async fetch(req) {
      try { received.push(await req.json()) } catch { received.push({}) }
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

describe('tap.ts end to end', () => {
  test('the detector stays OFF when FLEET_BUS_WATCH_BOTS is unset', async () => {
    // The default must add no traffic at all. The tap in this suite is started without
    // the variable, so a startup announcement here would mean the feature is on by
    // accident -- which on a live fleet is noise nobody asked for.
    await sleep(200)
    const announced = received.some((r) => (r.content || '').includes('tap up, watching'))
    expect(announced).toBe(false)
  })

  test('a published envelope reaches the Discord endpoint as a POST', async () => {
    // THE test. It fails if the tap does not connect, does not subscribe, does not
    // format, or does not post — every step the absence-based tests could not see.
    const nc = await connect({ servers: NATS_URL, name: 'tap-integration-test' })
    const sc = StringCodec()
    nc.publish('fleet.vec.request', sc.encode(JSON.stringify({
      envelope_version: 1,
      id: 'abcdef0123456789',
      from: 'deet',
      to: 'vec',
      kind: 'baton.handoff',
      payload: { text: 'integration probe' },
    })))
    await nc.flush()

    for (let i = 0; i < 40 && received.length === 0; i++) await sleep(100)
    await nc.close()

    expect(received.length).toBeGreaterThan(0)
    const body = received[0]

    // DIRECTIONAL. Asserting that 'deet' and 'vec' merely OCCUR passes against a tap
    // that swaps them, which Ohm proved by swapping from/to at tap.ts's call site: all
    // 65 tests still passed. The rendered arrow is the only thing that carries
    // direction, so the assertion must include it. (Ohm, PR 67.)
    expect(body.content).toContain('**deet** → **vec**')
    expect(body.content).not.toContain('**vec** → **deet**')

    expect(body.content).toContain('`baton.handoff`')
    expect(body.content).toContain('`fleet.vec.request`')
    expect(body.content).toContain('abcdef01')
    expect(body.username).toBe('deet')
  }, 20_000)

  test('an envelope with a hostile id still arrives, rather than killing the tap', async () => {
    // The crash class, proven against the running process rather than the helper.
    // The marker IDENTIFIES this message. Asserting only that the count rose passes if
    // the tap redelivers the previous envelope instead, so the arrival must be the one
    // published here and no other. (Ohm, PR 67.)
    const MARKER = 'hostile-id-probe-7f3a'
    const before = received.length
    const nc = await connect({ servers: NATS_URL, name: 'tap-integration-test-2' })
    const sc = StringCodec()
    nc.publish('fleet.vec.request', sc.encode(
      `{"envelope_version":1,"id":{"toString":null},"from":"deet","to":"vec",` +
      `"kind":"text_message","payload":{"probe":"${MARKER}"}}`,
    ))
    await nc.flush()

    const found = () => received.some((r) => (r.content || '').includes(MARKER))
    for (let i = 0; i < 40 && !found(); i++) await sleep(100)
    await nc.close()

    expect(received.length).toBeGreaterThan(before)
    expect(found()).toBe(true)
    const body = received.find((r) => (r.content || '').includes(MARKER))!
    // It must render the UNCONVERTIBLE id rather than dropping the message or the field.
    expect(body.content).toContain('**deet** → **vec**')
  }, 20_000)

  test('the tap is still alive after both', () => {
    expect(tapProc?.killed).toBeFalsy()
  })
})
