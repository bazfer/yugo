/** SPEC-26 §6 item 15b — fail-closed on FFI error AFTER a successful startup.
 *
 * Test 18a proves the startup ORDERING guarantee: a clock failure refuses before
 * any database opens. This file proves the different, post-startup guarantee: a
 * consumer that started cleanly and then loses the clock refuses every fresh
 * claim, every takeover and every renewal, with no authorization and no lease
 * metadata mutation — including `:memory:`, which §1a.1 does not exempt.
 *
 * Every case runs in its own process through `conformance/clock-fault-probe.ts`,
 * which injects at the native adapter boundary rather than calling the pure
 * validation helpers. Gated on the qualified runtime (SPEC-26 §1a.2) because the
 * adapter refuses any other platform outright; a visible skip, never a silent pass.
 */
import { describe, expect, test } from 'bun:test'
import { Database } from 'bun:sqlite'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { DurableEnvelopeDedupStore, DEFAULT_DEDUP_TTL_MS } from './fleet-bus'
import { provisionTestStore } from './dedup-test-fixtures'

const LEASE_MS = 60_000
const HELD = 'held'
const OPERATIONS = ['claim', 'takeover', 'renew'] as const
type Operation = typeof OPERATIONS[number]

/** Each fault asserts its OWN refusal reason, so one broad catch cannot pass. */
const FAULTS: Record<string, string> = {
  nonzero: 'clock_gettime failed',
  'no-write': 'invalid clock_gettime timespec',
  'partial-write': 'invalid clock_gettime timespec',
  'negative-seconds': 'invalid clock_gettime timespec',
  'negative-nanoseconds': 'invalid clock_gettime timespec',
  'nanoseconds-out-of-range': 'invalid clock_gettime timespec',
  'unsafe-milliseconds': 'unsafe monotonic milliseconds',
  'deadline-overflow': 'monotonic lease deadline overflow',
}

const PROBE = join(import.meta.dir, '..', 'conformance', 'clock-fault-probe.ts')

function probe(config: Record<string, unknown>, env: Record<string, string>): any {
  const result = Bun.spawnSync([process.execPath, PROBE, JSON.stringify(config)], { env, stderr: 'pipe' })
  const stdout = result.stdout.toString().trim()
  expect(result.exitCode, result.stderr.toString()).toBe(0)
  return JSON.parse(stdout.slice(stdout.lastIndexOf('\n') + 1))
}

/** A file-backed store with a pending claim already held by another process. */
function heldStore(operation: Operation) {
  const directory = mkdtempSync(join(tmpdir(), 'clock-fault-'))
  const path = join(directory, 'store.sqlite')
  const previous = process.env.YUGO_DEDUP_VERIFICATION_RECORD
  process.env.YUGO_DEDUP_VERIFICATION_RECORD = provisionTestStore(path)
  const env = { ...process.env } as Record<string, string>
  const store = new DurableEnvelopeDedupStore(path, DEFAULT_DEDUP_TTL_MS, LEASE_MS)
  const owner = store.claim(HELD, 'owner-req').owner!
  const db = (store as any).db as Database
  if (operation === 'takeover') db.exec('UPDATE envelope_dedup_v2 SET lease_until_mono_ms=1')
  const rows = () => db.query('SELECT * FROM envelope_dedup_v2 ORDER BY envelope_id').all()
  return {
    path, env, owner, rows,
    dispose: () => {
      db.close()
      if (previous === undefined) delete process.env.YUGO_DEDUP_VERIFICATION_RECORD
      else process.env.YUGO_DEDUP_VERIFICATION_RECORD = previous
      rmSync(directory, { recursive: true, force: true })
    },
  }
}

function backing(operation: Operation, store: 'memory' | 'file') {
  if (store === 'memory') {
    // The probe builds its own pre-state in-process with the real syscall.
    return { path: ':memory:', env: process.env as Record<string, string>, owner: undefined, rows: () => undefined, dispose: () => {} }
  }
  return heldStore(operation)
}

describe.skipIf(process.env.YUGO_QUALIFIED_CLOCK_TEST !== '1')('15b post-startup clock failure SPEC-26', () => {
  for (const store of ['memory', 'file'] as const) {
    for (const [fault, expected] of Object.entries(FAULTS)) {
      for (const operation of OPERATIONS) {
        test(`15b ${fault} at ${operation} refuses with no authorization or metadata mutation (${store})`, () => {
          const fixture = backing(operation, store)
          try {
            const report = probe({ fault, operation, path: fixture.path, owner: fixture.owner }, fixture.env)
            expect(report.constructed).toBe(true) // The failure is post-startup.
            expect(report.refused).toBe(true)
            expect(report.message).toContain(expected)
            expect(report.result).toBeUndefined()
            // No fresh row, no new owner, no extended deadline: the probe's own
            // before/after snapshot, and for a file store the parent's own read.
            expect(report.after).toEqual(report.before)
            if (store === 'file') expect(fixture.rows()).toEqual(report.before)
          } finally {
            fixture.dispose()
          }
        })
      }
    }

    for (const operation of OPERATIONS) {
      test(`15b initialisation failure refuses the consumer so ${operation} never runs (${store})`, () => {
        const fixture = backing(operation, store)
        try {
          const report = probe({ fault: 'initialisation', operation, path: fixture.path, owner: fixture.owner }, fixture.env)
          // The library handle is only cached on success, so an unusable adapter
          // refuses construction and the operation has no store to act on.
          expect(report.constructed).toBe(false)
          expect(report.refused).toBe(true)
          expect(report.message).toContain('forced clock initialisation failure')
          if (store === 'file') {
            const rows = fixture.rows() as any[]
            expect(rows.length).toBe(1)
            expect(rows[0].lease_owner).toBe(fixture.owner)
          }
        } finally {
          fixture.dispose()
        }
      })
    }

    test(`15b a valid tv_sec of zero is accepted, not refused (${store})`, () => {
      const fixture = backing('claim', store)
      try {
        // Without this control a fix that refuses every native result would pass
        // every case above. SPEC-26 §1a.1: refuse tv_sec < 0 only.
        const report = probe({ fault: 'zero-seconds', operation: 'claim', path: fixture.path, owner: fixture.owner }, fixture.env)
        expect(report.constructed).toBe(true)
        expect(report.refused).toBe(false)
        expect(report.result.claim.duplicate).toBe(false)
        expect(report.after.length).toBe(report.before.length + 1)
      } finally {
        fixture.dispose()
      }
    })
  }
})
