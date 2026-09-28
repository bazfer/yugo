/** Operator-attested storage verification with startup drift detection.
 * Not proof of locality, continuous enforcement, or a security boundary.
 * File-backed consumers never create/repair stores or attestation records.
 */
import { readFileSync, realpathSync, statSync, readdirSync } from 'node:fs'
import { isAbsolute } from 'node:path'
import { Database } from 'bun:sqlite'
import { readMonotonicMs } from './monotonic-clock.ts'

export const BOOT_PATH = '/proc/sys/kernel/random/boot_id'
export const TIMENS_PATH = '/proc/self/timens_offsets'
export const UUID_DIRECTORY = '/dev/disk/by-uuid'
export const PORT = 'typescript'
const HINT = 'run yugo dedup provision'
const BOOT_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
export const SUPPORTED_SCHEMA = {
  table: 'envelope_dedup_v2',
  columns: [
    { name: 'envelope_id', declared_type: 'TEXT' },
    { name: 'first_seen_ms', declared_type: 'INTEGER' },
    { name: 'req_id', declared_type: 'TEXT' },
    { name: 'state', declared_type: 'TEXT' },
    { name: 'lease_owner', declared_type: 'TEXT' },
    { name: 'lease_until_ms', declared_type: 'INTEGER' },
    { name: 'lease_boot_id', declared_type: 'TEXT' },
    { name: 'lease_until_mono_ms', declared_type: 'INTEGER' },
  ],
}

export class VerificationError extends Error {}
function requireCheck(condition: unknown, reason: string): asserts condition {
  if (!condition) throw new VerificationError(`dedup verification refused: ${reason}; ${HINT}`)
}
export function validBootId(value: unknown): value is string {
  return typeof value === 'string' && BOOT_RE.test(value)
}
export function readBootId(): string {
  const boot = readFileSync(BOOT_PATH, 'utf8').trim()
  requireCheck(validBootId(boot), 'invalid local boot identity')
  return boot
}
export function checkClockDomain(): void {
  const lines = readFileSync(TIMENS_PATH, 'utf8').replace(/\n$/, '').split('\n')
  const clocks = new Map<string, [bigint, bigint]>()
  for (const line of lines) {
    const match = /^(monotonic|boottime)\s+(-?[0-9]+)\s+(-?[0-9]+)\s*$/.exec(line)
    requireCheck(match, 'unparseable timens_offsets line')
    requireCheck(!clocks.has(match[1]!), 'duplicate timens_offsets clock')
    clocks.set(match[1]!, [BigInt(match[2]!), BigInt(match[3]!)])
  }
  requireCheck(clocks.size === 2 && clocks.has('monotonic') && clocks.has('boottime'), 'incomplete timens_offsets')
  const monotonic = clocks.get('monotonic')!
  requireCheck(monotonic[0] === 0n && monotonic[1] === 0n, 'non-zero monotonic offset')
}
function fields(value: any, keys: string[], where: string): void {
  requireCheck(value !== null && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).sort().join(',') === [...keys].sort().join(','), `invalid ${where} fields`)
}
function nonempty(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}
function timestamp(value: unknown): boolean {
  if (typeof value !== 'string') return false
  const m = /^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:[Zz]|([+-])(\d{2}):(\d{2}))$/.exec(value)
  if (!m) return false
  const [, y, mo, d, h, mi, s, , oh, om] = m
  const year = Number(y), month = Number(mo), day = Number(d)
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
  return year > 0 && month >= 1 && month <= 12 && day >= 1 && day <= days[month - 1]!
    && Number(h) < 24 && Number(mi) < 60 && Number(s) < 60
    && (oh === undefined || (Number(oh) < 24 && Number(om) < 60))
}
// Values cross the untrusted JSON boundary only through this strict validator.
export function validateRecord(record: any): any {
  fields(record, ['record_version', 'canonical_path', 'device', 'inode', 'port', 'schema_fingerprint',
    'storage_evidence', 'participant_inventory', 'attested_by', 'attested_at'], 'record')
  requireCheck(record.record_version === 1, 'record version')
  requireCheck(typeof record.canonical_path === 'string' && isAbsolute(record.canonical_path), 'canonical path')
  requireCheck(typeof record.inode === 'string' && /^[0-9]+$/.test(record.inode), 'inode decimal string')
  requireCheck(BigInt(record.inode) <= (1n << 64n) - 1n, 'inode out of range')
  requireCheck(['python', 'typescript'].includes(record.port), 'record port')
  const device = record.device
  requireCheck(device !== null && typeof device === 'object', 'device object')
  if (device.binding === 'uuid') {
    fields(device, ['binding', 'fs_uuid'], 'UUID device')
    requireCheck(nonempty(device.fs_uuid), 'filesystem UUID')
  } else {
    fields(device, ['binding', 'major', 'minor', 'attested_boot_id'], 'devno device')
    requireCheck(device.binding === 'devno', 'device binding')
    requireCheck(['major', 'minor'].every(k => Number.isSafeInteger(device[k]) && device[k] >= 0 && device[k] <= 0xffffffff), 'device integers')
    requireCheck(validBootId(device.attested_boot_id), 'attested boot identity')
  }
  const fingerprint = record.schema_fingerprint
  fields(fingerprint, ['table', 'columns'], 'fingerprint')
  requireCheck(fingerprint.table === 'envelope_dedup_v2' && Array.isArray(fingerprint.columns), 'fingerprint shape')
  for (const column of fingerprint.columns) {
    fields(column, ['name', 'declared_type'], 'column')
    requireCheck(nonempty(column.name) && nonempty(column.declared_type), 'column strings')
  }
  const evidence = record.storage_evidence
  fields(evidence, ['device_path', 'mount_point', 'fstype', 'mount_id_source', 'backing',
    'determined_by', 'uuid_resolution', 'inspected_at'], 'storage evidence')
  requireCheck(Object.values(evidence).every(nonempty), 'storage evidence strings')
  requireCheck(['local-block', 'local-virtual'].includes(evidence.backing), 'network or unknown backing')
  requireCheck(timestamp(evidence.inspected_at), 'inspection timestamp')
  requireCheck(Array.isArray(record.participant_inventory) && record.participant_inventory.length > 0, 'participant inventory')
  for (const participant of record.participant_inventory) {
    fields(participant, ['process', 'user', 'path', 'method'], 'participant')
    requireCheck(Object.values(participant).every(nonempty), 'participant strings')
  }
  requireCheck(nonempty(record.attested_by) && timestamp(record.attested_at), 'attestation')
  return record
}

/** Linux dev_t decomposition; keep all stat identity values as bigint. */
export function deviceNumbers(dev: bigint): [bigint, bigint] {
  return [((dev >> 8n) & 0xfffn) | ((dev >> 32n) & 0xfffff000n),
    (dev & 0xffn) | ((dev >> 12n) & 0xffffff00n)]
}
export function resolveUuids(device: bigint): string[] {
  const matches: string[] = []
  for (const entry of readdirSync(UUID_DIRECTORY)) {
    try {
      if (statSync(`${UUID_DIRECTORY}/${entry}`, { bigint: true }).rdev === device) matches.push(entry)
    } catch { /* An unreadable target cannot establish a match. */ }
  }
  return matches
}
function checkDevice(record: any, dev: bigint): void {
  const device = record.device
  if (device.binding === 'uuid') {
    let uuids: string[] = []
    try { uuids = resolveUuids(dev) } catch { /* Missing directory refuses, no downgrade. */ }
    requireCheck(uuids.some(v => v.toLowerCase() === device.fs_uuid.toLowerCase()), 'unresolved or changed filesystem UUID')
  } else {
    const [major, minor] = deviceNumbers(dev)
    requireCheck(major === BigInt(device.major) && minor === BigInt(device.minor), 'device mismatch')
    requireCheck(device.attested_boot_id === readBootId(), 'devno attestation is from another boot')
  }
}
export function fingerprint(db: Database) {
  return {
    table: 'envelope_dedup_v2',
    columns: (db.query('PRAGMA table_info(envelope_dedup_v2)').all() as { name: string; type: string }[])
      .map(row => ({ name: row.name, declared_type: row.type })),
  }
}
function sameSchema(a: typeof SUPPORTED_SCHEMA, b: typeof SUPPORTED_SCHEMA): boolean {
  return a.table === b.table && a.columns.length === b.columns.length
    && a.columns.every((v, i) => v.name === b.columns[i]!.name && v.declared_type === b.columns[i]!.declared_type)
}
export function checkSchema(db: Database, record: any): void {
  const live = fingerprint(db)
  requireCheck(sameSchema(live, record.schema_fingerprint), 'live/record schema mismatch')
  requireCheck(sameSchema(live, SUPPORTED_SCHEMA), 'unsupported schema for running port')
}
export function createMemorySchema(db: Database): void {
  db.exec(`CREATE TABLE envelope_dedup_v2 (
    envelope_id TEXT PRIMARY KEY, first_seen_ms INTEGER NOT NULL,
    req_id TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','completed')),
    lease_owner TEXT NOT NULL, lease_until_ms INTEGER NOT NULL,
    lease_boot_id TEXT NOT NULL DEFAULT '', lease_until_mono_ms INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX envelope_dedup_v2_first_seen ON envelope_dedup_v2(first_seen_ms)`)
}
export function openVerifiedStore(path: string, recordPath = process.env.YUGO_DEDUP_VERIFICATION_RECORD): Database {
  let db: Database | undefined
  try {
    checkClockDomain()
    readMonotonicMs() // Initialization/read failure refuses even :memory:, before writes.
    if (path === ':memory:') {
      readBootId()
      db = new Database(':memory:')
      createMemorySchema(db)
      return db
    }
    requireCheck(recordPath, 'missing verification record')
    const record = validateRecord(JSON.parse(readFileSync(recordPath, 'utf8')))
    requireCheck(record.port === PORT, 'port mismatch')
    const canonical = realpathSync(path)
    requireCheck(canonical === record.canonical_path, 'canonical path mismatch')
    checkDevice(record, statSync(path, { bigint: true }).dev)
    const before = statSync(path, { bigint: true })
    requireCheck(before.ino === BigInt(record.inode), 'inode mismatch')
    db = new Database(canonical, { readwrite: true, create: false })
    const after = statSync(path, { bigint: true })
    requireCheck(after.dev === before.dev && after.ino === before.ino, 'store changed while opening')
    requireCheck(after.ino === BigInt(record.inode), 'post-open inode mismatch')
    checkDevice(record, after.dev)
    const mode = db.query('PRAGMA journal_mode').get() as { journal_mode: string }
    requireCheck(mode.journal_mode.toLowerCase() === 'wal', 'WAL inactive')
    readBootId()
    checkSchema(db, record)
    db.exec('PRAGMA busy_timeout=5000')
    return db
  } catch (error) {
    db?.close()
    if (error instanceof VerificationError) throw error
    throw new VerificationError(`dedup verification refused: ${error}; ${HINT}`)
  }
}
