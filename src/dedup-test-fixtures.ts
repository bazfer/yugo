/** Test-only provisioning, never an operator attestation for production. */
import { Database } from 'bun:sqlite'
import { existsSync, statSync, realpathSync, writeFileSync } from 'node:fs'
import { createMemorySchema, deviceNumbers, readBootId, SUPPORTED_SCHEMA } from './dedup-verification'

export function provisionTestStore(path: string): string {
  if (!existsSync(path)) {
    const db = new Database(path, { create: true })
    db.exec('PRAGMA journal_mode=WAL')
    createMemorySchema(db)
    db.close()
  }
  const st = statSync(path, { bigint: true })
  const [major, minor] = deviceNumbers(st.dev)
  const recordPath = path + '.verification.json'
  writeFileSync(recordPath, JSON.stringify({
    record_version: 1, canonical_path: realpathSync(path), inode: st.ino.toString(),
    port: 'typescript', schema_fingerprint: SUPPORTED_SCHEMA,
    device: { binding: 'devno', major: Number(major), minor: Number(minor), attested_boot_id: readBootId() },
    storage_evidence: {
      device_path: 'test fixture', mount_point: 'unresolved', fstype: 'unresolved',
      mount_id_source: 'unresolved', backing: 'local-virtual', determined_by: 'test fixture only',
      uuid_resolution: 'test devno binding', inspected_at: '2026-09-24T19:00:00Z',
    },
    participant_inventory: [{ process: 'test', user: 'test', path, method: 'test fixture' }],
    attested_by: 'test', attested_at: '2026-09-24T19:00:00Z',
  }))
  return recordPath
}
