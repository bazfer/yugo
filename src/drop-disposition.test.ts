import { test, expect } from 'bun:test'
import { FleetBus } from './fleet-bus'
import { DROP_DISPOSITION } from './drop-disposition'

function harness() {
  const published: any[] = [], audits: any[] = []
  const bus: any = Object.create(FleetBus.prototype)
  Object.assign(bus, {
    notifiedDrops: new WeakSet(),
    config: { botName: 'kat' }, allowedFromClaims: new Set(['deet', 'kat']),
    dropCounts: { terminal: 0, transient: 0, throttled: 0, duplicate: 0 },
    nc: { publish: (subject: string, data: Uint8Array) => published.push({ subject, envelope: JSON.parse(new TextDecoder().decode(data)) }) },
    writeAudit: (entry: any) => audits.push(entry),
  })
  return { bus, published, audits }
}
const envelope = { id: 'original', from: 'deet', kind: 'text_message' }
test('transient drop reports exactly once on request lane with correlation and counters', () => {
  const { bus, published, audits } = harness()
  bus.recordDrop({ dir: 'drop', subject: 'fleet.kat.request', reason: 'injection_failed' }, envelope)
  expect(published).toHaveLength(1)
  expect(published[0].subject).toBe('fleet.deet.request')
  expect(published[0].envelope.kind).toBe('drop_notice')
  expect(published[0].envelope.payload).toEqual({ envelope_id: 'original', reason: 'injection_failed', disposition: 'transient', subject: 'fleet.kat.request' })
  expect(audits[0].disposition).toBe('transient')
  expect(bus.busStatus().drop_counts.transient).toBe(1)
})
test('dropping a notice cannot produce a second notice, even if invalid', () => {
  const { bus, published } = harness()
  bus.recordDrop({ dir: 'drop', subject: 'fleet.kat.request', reason: 'invalid_id' }, { ...envelope, kind: 'drop_notice', id: null })
  expect(published).toHaveLength(0)
})
test('duplicates never notify', () => {
  const { bus, published } = harness()
  bus.recordDrop({ dir: 'drop', subject: 'fleet.kat.request', reason: 'yugo_duplicate_envelope' }, envelope)
  expect(published).toHaveLength(0)
})
test('manifest events are not drop reasons', () => {
  for (const reason of ['fleet_manifest_loaded', 'fleet_manifest_load_failed', 'fleet_manifest_reload_failed']) {
    expect(Object.hasOwn(DROP_DISPOSITION, reason)).toBe(false)
  }
})

test('all three manifest events still emit without disposition', async () => {
  const { mkdtempSync, writeFileSync, readFileSync, rmSync } = await import('node:fs')
  const { tmpdir } = await import('node:os')
  const { join } = await import('node:path')
  const { FleetManifestAllowlist } = await import('./fleet-bus')
  const dir = mkdtempSync(join(tmpdir(), 'drop-manifest-'))
  const manifest = join(dir, 'manifest.yaml'), audit = join(dir, 'audit.jsonl')
  try {
    expect(() => new FleetManifestAllowlist(manifest, audit)).toThrow()
    writeFileSync(manifest, 'bot_names: [kat, deet]')
    const allowed = new FleetManifestAllowlist(manifest, audit, undefined, 0)
    writeFileSync(manifest, 'bot_names: [')
    allowed.refresh()
    const entries = readFileSync(audit, 'utf8').trim().split('\n').map(line => JSON.parse(line))
    expect(entries.map(entry => entry.reason)).toEqual(['fleet_manifest_load_failed', 'fleet_manifest_loaded', 'fleet_manifest_reload_failed'])
    expect(entries.every(entry => entry.disposition === undefined && entry.dir !== 'drop')).toBe(true)
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('a published notice rejected by the next receiver produces no notice back', async () => {
  const first = harness()
  first.bus.recordDrop({ dir: 'drop', subject: 'fleet.kat.request', reason: 'injection_failed' }, envelope)
  expect(first.published).toHaveLength(1)
  const second = harness()
  second.bus.config = { botName: 'deet', maxEnvelopeBytes: 1 }
  second.bus.codec = { decode: (data: Uint8Array) => JSON.parse(new TextDecoder().decode(data)) }
  second.bus.rateLimiters = { perSubject: { allow: () => true } }
  await second.bus.onRequest({ subject: 'fleet.deet.request', data: new TextEncoder().encode(JSON.stringify(first.published[0].envelope)) })
  expect(second.audits[0].reason).toBe('envelope_too_large')
  expect(second.published).toHaveLength(0)
})

test('cleanup drops count but do not send two notices for one decoded delivery', () => {
  const { bus, published } = harness()
  const delivery = { ...envelope }
  bus.recordDrop({ dir: 'drop', subject: 'fleet.kat.request', reason: 'injection_failed' }, delivery)
  bus.recordDrop({ dir: 'drop', subject: 'fleet.kat.request', reason: 'claude_discord_adapter_reply_undelivered' }, delivery)
  expect(published).toHaveLength(1)
  expect(bus.busStatus().drop_counts.transient).toBe(2)
  // A later redelivery gets its own diagnosis, not a process-lifetime ID suppression.
  bus.recordDrop({ dir: 'drop', subject: 'fleet.kat.request', reason: 'injection_failed' }, { ...envelope })
  expect(published).toHaveLength(2)
})
