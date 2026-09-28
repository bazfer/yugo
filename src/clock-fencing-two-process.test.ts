/** SPEC-26 §6 item 15a — the two-process LEASE regression.
 *
 * Two real Bun processes of different ages competing over ONE shared verified
 * store through the production claim/renew path, plus the adapter-agreement
 * half. Requires the qualified runtime (SPEC-26 §1a.2), so it is gated exactly
 * like test 13/15 — a visible skip, never a silent pass. The native CI job runs
 * this file with the gate set.
 */
import { afterAll, beforeAll, describe, expect, test } from 'bun:test'
import { mkdtempSync, rmSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import type { Subprocess } from 'bun'
import { provisionTestStore } from './dedup-test-fixtures'

// Larger than LIVE_LEASE_MS: under process-relative hrtime the older peer's
// reading must exceed the younger owner's whole deadline for the early takeover
// to fire, which is the regression this file exists to catch.
const AGE_GAP_MS = 3000
const LIVE_LEASE_MS = 1000
const SHORT_LEASE_MS = 150
const EXPIRY_WAIT_MS = 600

class Peer {
  private readonly process: Subprocess<'pipe', 'pipe', 'inherit'>
  private readonly lines: AsyncGenerator<string, void, undefined>

  constructor(directory: string, env: Record<string, string>) {
    this.process = Bun.spawn([process.execPath, join(import.meta.dir, '..', 'conformance', 'lease-clock-process.ts')], {
      cwd: directory, stdin: 'pipe', stdout: 'pipe', stderr: 'inherit', env,
    }) as Subprocess<'pipe', 'pipe', 'inherit'>
    this.lines = Peer.lineStream(this.process.stdout)
  }

  private static async *lineStream(stdout: AsyncIterable<Uint8Array>): AsyncGenerator<string, void, undefined> {
    const decoder = new TextDecoder()
    let buffer = ''
    for await (const chunk of stdout) {
      buffer += decoder.decode(chunk, { stream: true })
      let newline: number
      while ((newline = buffer.indexOf('\n')) >= 0) {
        yield buffer.slice(0, newline)
        buffer = buffer.slice(newline + 1)
      }
    }
  }

  async send(command: Record<string, unknown>): Promise<any> {
    this.process.stdin.write(JSON.stringify(command) + '\n')
    await this.process.stdin.flush()
    const line = await this.lines.next()
    if (line.done) throw new Error(`peer exited before answering ${JSON.stringify(command)}`)
    const response = JSON.parse(line.value)
    if (response.error) throw new Error(`peer refused ${command.op}: ${response.error}`)
    return response
  }

  kill(): void {
    this.process.kill()
  }
}

function pythonMonotonicNs(): bigint {
  const result = spawnSync(process.env.PYTHON ?? 'python3', ['-c', 'import time; print(time.monotonic_ns())'], { encoding: 'utf8' })
  expect(result.status, result.stderr).toBe(0)
  return BigInt(result.stdout.trim())
}

describe.skipIf(process.env.YUGO_QUALIFIED_CLOCK_TEST !== '1')('15a two-process lease regression SPEC-26', () => {
  let directory: string
  let older: Peer
  let younger: Peer

  beforeAll(async () => {
    directory = mkdtempSync(join(tmpdir(), 'two-process-lease-'))
    const path = join(directory, 'store.sqlite')
    const env = { ...process.env, YUGO_DEDUP_VERIFICATION_RECORD: provisionTestStore(path) } as Record<string, string>
    older = new Peer(directory, env)
    // Both peers open the SAME verified store; nothing else distinguishes them.
    await older.send({ op: 'open', key: 'live', path, leaseMs: LIVE_LEASE_MS })
    await older.send({ op: 'open', key: 'short', path, leaseMs: SHORT_LEASE_MS })
    await Bun.sleep(AGE_GAP_MS)
    younger = new Peer(directory, env)
    await younger.send({ op: 'open', key: 'live', path, leaseMs: LIVE_LEASE_MS })
    await younger.send({ op: 'open', key: 'short', path, leaseMs: SHORT_LEASE_MS })
  })

  afterAll(() => {
    older?.kill()
    younger?.kill()
    if (directory) rmSync(directory, { recursive: true, force: true })
  })

  test('15a(b) an older rival does not take a younger owner live lease', async () => {
    const owned = (await younger.send({ op: 'claim', key: 'live', id: 'younger-owner', reqId: 'owner-req' })).claim
    expect(owned.duplicate).toBe(false)
    const stolen = (await older.send({ op: 'claim', key: 'live', id: 'younger-owner', reqId: 'rival-req' })).claim
    expect(stolen.duplicate).toBe(true)
    expect(stolen.owner).toBeUndefined()
    expect(stolen.reqId).toBe('owner-req')
    expect((await younger.send({ op: 'row', key: 'live', id: 'younger-owner' })).row.lease_owner).toBe(owned.owner)
  })

  test('15a(b) renewal by the true owner holds across both processes', async () => {
    const owned = (await younger.send({ op: 'claim', key: 'live', id: 'renewed', reqId: 'owner-req' })).claim
    const before = (await younger.send({ op: 'row', key: 'live', id: 'renewed' })).row
    expect((await younger.send({ op: 'renew', key: 'live', id: 'renewed', owner: owned.owner })).renewed).toBe(true)
    const after = (await younger.send({ op: 'row', key: 'live', id: 'renewed' })).row
    expect(after.lease_owner).toBe(owned.owner)
    expect(after.lease_until_mono_ms).toBeGreaterThan(before.lease_until_mono_ms)
    const stolen = (await older.send({ op: 'claim', key: 'live', id: 'renewed', reqId: 'rival-req' })).claim
    expect(stolen.duplicate).toBe(true)
    expect((await older.send({ op: 'row', key: 'live', id: 'renewed' })).row.lease_owner).toBe(owned.owner)
  })

  test('15a(c) a younger rival does not take an older owner live lease', async () => {
    const owned = (await older.send({ op: 'claim', key: 'live', id: 'older-owner', reqId: 'owner-req' })).claim
    expect(owned.duplicate).toBe(false)
    const stolen = (await younger.send({ op: 'claim', key: 'live', id: 'older-owner', reqId: 'rival-req' })).claim
    expect(stolen.duplicate).toBe(true)
    expect(stolen.owner).toBeUndefined()
    expect((await older.send({ op: 'row', key: 'live', id: 'older-owner' })).row.lease_owner).toBe(owned.owner)
  })

  test('15a(d) a genuinely expired lease is taken over and the old owner loses renewal', async () => {
    const owned = (await older.send({ op: 'claim', key: 'short', id: 'expiring', reqId: 'owner-req' })).claim
    expect(owned.duplicate).toBe(false)
    await Bun.sleep(EXPIRY_WAIT_MS)
    // Refusing everything would satisfy (b) and (c) trivially; this is the case
    // that separates "correctly fenced" from "broken closed".
    const taken = (await younger.send({ op: 'claim', key: 'short', id: 'expiring', reqId: 'rival-req' })).claim
    expect(taken.duplicate).toBe(false)
    expect(taken.owner).not.toBe(owned.owner)
    expect(taken.reqId).toBe('owner-req')
    expect((await older.send({ op: 'row', key: 'short', id: 'expiring' })).row.lease_owner).toBe(taken.owner)
    expect((await older.send({ op: 'renew', key: 'short', id: 'expiring', owner: owned.owner })).renewed).toBe(false)
  })

  test('15a(a) older and younger adapter readings bracket time.monotonic_ns', async () => {
    const before = BigInt((await older.send({ op: 'mono' })).mono)
    const python = pythonMonotonicNs()
    const after = BigInt((await younger.send({ op: 'mono' })).mono)
    // Process-relative readings cannot bracket a host-monotonic one: that is the
    // exact inverse of the measurement that disproved the hrtime premise.
    expect(before).toBeLessThanOrEqual(python)
    expect(python).toBeLessThanOrEqual(after)
  })
})
