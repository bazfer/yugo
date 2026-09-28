import { afterEach, beforeEach, describe, expect, spyOn, test } from 'bun:test'
import { Database } from 'bun:sqlite'
import * as fs from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import { DurableEnvelopeDedupStore, DEFAULT_DEDUP_TTL_MS } from './fleet-bus'
import * as monotonic from './monotonic-clock'
import * as verification from './dedup-verification'
import { provisionTestStore } from './dedup-test-fixtures'

const OTHER = '22222222-2222-4222-8222-222222222222'
let mono = 100_000
const cleanups: (() => void)[] = []
afterEach(() => { for (const cleanup of cleanups.splice(0).reverse()) cleanup() })
function clock() {
  mono = 100_000
  const mock = spyOn(monotonic, 'readMonotonicMs').mockImplementation(() => mono)
  cleanups.push(() => mock.mockRestore())
}
function store() { clock(); return new DurableEnvelopeDedupStore(':memory:') }
function db(s: DurableEnvelopeDedupStore): Database { return (s as any).db }
function row(s: DurableEnvelopeDedupStore): any { return db(s).query("SELECT * FROM envelope_dedup_v2 WHERE envelope_id='e'").get() }
function rawExpiry(s: DurableEnvelopeDedupStore) { db(s).exec('UPDATE envelope_dedup_v2 SET lease_until_mono_ms=1') }
function fileFixture() {
  const dir = fs.mkdtempSync(join(tmpdir(), 'fencing-'))
  const path = join(dir, 'store.sqlite')
  const record = provisionTestStore(path)
  const data = JSON.parse(fs.readFileSync(record, 'utf8'))
  const open = () => verification.openVerifiedStore(path, record)
  const save = () => fs.writeFileSync(record, JSON.stringify(data))
  return { path, record, data, open, save }
}
function interceptRead(fn: (path: string) => string | undefined | void) {
  const original = fs.readFileSync
  const mock = spyOn(fs, 'readFileSync').mockImplementation(((path: any, options: any) => {
    const value = fn(String(path))
    return value === undefined ? original(path, options) : value
  }) as any)
  cleanups.push(() => mock.mockRestore())
}

describe('lease protocol SPEC-26', () => {
  test('1 forward wall step before renewal refuses', () => {
    const s = store(), first = s.claim('e', 'original', 100_000)
    expect(s.claim('e', 'rival', 1_000_000).duplicate).toBe(true)
    expect(row(s).lease_owner).toBe(first.owner)
  })
  test('2 same boot monotonic expiry takes over', () => {
    const s = store(), first = s.claim('e', 'original', 100_000)
    mono += 60_000
    const next = s.claim('e', 'rival', 100_000)
    expect(next.duplicate).toBe(false)
    expect(next.owner).not.toBe(first.owner)
    expect(next.reqId).toBe('original')
    expect(row(s).lease_until_mono_ms).toBe(220_000)
  })
  test('3 valid different boot immediately takes over', () => {
    const s = store(); s.claim('e', 'original', 100_000)
    db(s).query('UPDATE envelope_dedup_v2 SET lease_boot_id=?').run(OTHER)
    expect(s.claim('e', 'rival', 100_000).duplicate).toBe(false)
    expect(row(s).lease_boot_id).toBe(verification.readBootId())
  })
  test('4 empty boot legacy uses wall then upgrades', () => {
    const s = store()
    db(s).exec("INSERT INTO envelope_dedup_v2 VALUES ('e',100000,'original','pending','old',160000,'',0)")
    expect(s.claim('e', 'rival', 159_999).duplicate).toBe(true)
    expect(s.claim('e', 'rival', 160_000).duplicate).toBe(false)
    expect(row(s).lease_boot_id).toBe(verification.readBootId())
  })
  test('5 backward wall step does not extend hold', () => {
    const s = store(); s.claim('e', 'original', 100_000); mono += 60_000
    expect(s.claim('e', 'rival', -1000).duplicate).toBe(false)
  })
  for (const path of ['target', 'periodic', 'idle', 'direct']) {
    test(`6 pending survives every TTL path: ${path}`, () => {
      const s = store(), a = s.claim('e', 'original', 100_000)
      const completed = s.claim('done', 'done', 100_000)
      s.complete('done', completed.owner!)
      const future = DEFAULT_DEDUP_TTL_MS * 10
      if (path === 'target') expect(s.claim('e', 'rival', future).duplicate).toBe(true)
      else if (path === 'periodic') { (s as any).claims = 255; s.claim('trigger', 'trigger', future) }
      else if (path === 'idle') expect(s.pruneIdle(future)).toBe(1)
      else expect(s.prune(future)).toBe(1)
      expect(row(s).lease_owner).toBe(a.owner)
    })
  }
  test('7 serialized renewal then takeover and reverse', () => {
    clock()
    const fixture = fileFixture()
    process.env.YUGO_DEDUP_VERIFICATION_RECORD = fixture.record
    const a = new DurableEnvelopeDedupStore(fixture.path), b = new DurableEnvelopeDedupStore(fixture.path)
    const first = a.claim('e', 'original', 100_000)
    mono += 59_000
    expect(a.renew('e', first.owner!, 159_000)).toBe(true)
    mono += 2000
    expect(b.claim('e', 'rival', 161_000).duplicate).toBe(true)
    mono += 60_000
    expect(b.claim('e', 'rival', 221_000).duplicate).toBe(false)
    expect(a.renew('e', first.owner!, 221_000)).toBe(false)
  })
  for (const columns of [6, 8]) {
    test(`8 Release 1 named INSERT with ${columns} columns`, () => {
      const database = new Database(':memory:')
      verification.createMemorySchema(database)
      if (columns === 6) database.exec('ALTER TABLE envelope_dedup_v2 DROP COLUMN lease_until_mono_ms; ALTER TABLE envelope_dedup_v2 DROP COLUMN lease_boot_id')
      // Frozen Release-1 SQL. Release 2 startup intentionally rejects six columns.
      const r1 = "INSERT OR IGNORE INTO envelope_dedup_v2 (envelope_id,first_seen_ms,req_id,state,lease_owner,lease_until_ms) VALUES (?,?,?,'pending',?,?)"
      expect(database.query(r1).run('e', 100000, 'req', 'owner', 160000).changes).toBe(1)
      database.close()
    })
  }
  test('8 current named INSERT wider table tripwire issue35', () => {
    const s = store()
    // Added AFTER startup, solely to expose positional SQL. Not a live migration.
    db(s).exec("ALTER TABLE envelope_dedup_v2 ADD COLUMN future TEXT DEFAULT ''")
    expect(s.claim('e', 'req', 100_000).duplicate).toBe(false)
  })
  test('10 stop-all-accessors cutover: deleted equals never-seen', () => {
    const s = store(); s.claim('e', 'req', 100_000)
    db(s).exec('DELETE FROM envelope_dedup_v2 WHERE first_seen_ms < 1000000')
    const absent = db(s).query("SELECT * FROM envelope_dedup_v2 WHERE envelope_id='e'").all()
    const never = db(s).query("SELECT * FROM envelope_dedup_v2 WHERE envelope_id='never'").all()
    expect(JSON.stringify(absent)).toBe(JSON.stringify(never))
    expect(absent).toEqual([])
    expect(s.claim('e', 'again', 1_000_000).duplicate).toBe(false)
  })
  test('11 stop-all-accessors cutover: stale metadata matches a legitimate row', () => {
    const s = store(); s.claim('e', 'req', 100_000)
    const original = row(s)
    db(s).exec("UPDATE envelope_dedup_v2 SET lease_owner='replacement',lease_until_ms=1060000 WHERE envelope_id='e' AND lease_until_ms<=1000000")
    expect(row(s)).toEqual({ ...original, lease_owner: 'replacement', lease_until_ms: 1060000 })
    expect(Object.keys(row(s)).length).toBe(8) // No generation/owner discriminator.
    expect(s.claim('e', 'rival', 1_001_000).duplicate).toBe(true)
  })
  for (const [boot, deadline] of [[OTHER, 0], [OTHER, -1], [OTHER, 'bad'], [OTHER, 1.5], ['garbage', 160000]] as const) {
    test(`11a malformed metadata refuses ${boot} ${deadline}`, () => {
      const s = store(); s.claim('e', 'req', 100_000)
      db(s).query('UPDATE envelope_dedup_v2 SET lease_boot_id=?,lease_until_mono_ms=?').run(boot, deadline)
      const before = row(s)
      expect(() => s.claim('e', 'rival', 1_000_000)).toThrow('malformed')
      expect(row(s)).toEqual(before)
    })
  }
  test('12 Release 1 rollback works but loses safety', () => {
    const s = store(); s.claim('e', 'req', 100_000)
    const changed = db(s).query("UPDATE envelope_dedup_v2 SET lease_owner=?,lease_until_ms=? WHERE envelope_id=? AND state='pending' AND lease_until_ms<=?")
      .run('old-rival', 1060000, 'e', 1000000).changes
    expect(changed).toBe(1)
    expect(row(s).lease_owner).toBe('old-rival')
    expect(row(s).lease_until_mono_ms).toBe(160000)
  })
  test('14 unavailable boot refuses live takeover and fresh claim', () => {
    const s = store(); s.claim('e', 'req', 100_000)
    const before = row(s)
    interceptRead(path => { if (path === verification.BOOT_PATH) throw new Error('boot unreadable') })
    expect(() => s.claim('e', 'rival', 1_000_000)).toThrow('boot unreadable')
    expect(() => s.claim('fresh', 'rival', 1_000_000)).toThrow('boot unreadable')
    expect(row(s)).toEqual(before)
    expect(s.count()).toBe(1)
  })
  test('zero-row fresh INSERT cannot authorize execution', () => {
    const s = store()
    db(s).exec('CREATE TRIGGER prevent_insert BEFORE INSERT ON envelope_dedup_v2 BEGIN SELECT RAISE(IGNORE); END')
    expect(() => s.claim('e', 'req', 100_000)).toThrow('zero rows')
  })
  test('zero-row takeover cannot authorize execution', () => {
    const s = store(); s.claim('e', 'req', 100_000); rawExpiry(s)
    const before = row(s)
    db(s).exec('CREATE TRIGGER prevent_update BEFORE UPDATE ON envelope_dedup_v2 BEGIN SELECT RAISE(IGNORE); END')
    expect(s.claim('e', 'rival', 160_000).duplicate).toBe(true)
    expect(row(s)).toEqual(before)
  })
  test('commit failure never authorizes execution', () => {
    const s = store()
    db(s).exec(`PRAGMA foreign_keys=ON;
      CREATE TABLE parent(id TEXT PRIMARY KEY);
      CREATE TABLE child(id TEXT REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED);
      CREATE TRIGGER fail_commit AFTER INSERT ON envelope_dedup_v2 BEGIN INSERT INTO child VALUES ('absent'); END;`)
    expect(() => s.claim('e', 'req', 100_000)).toThrow()
    expect(row(s)).toBeNull()
  })
})

describe('startup interface SPEC-26', () => {
  beforeEach(clock)
  test('17 nonzero clock refuses', () => {
    interceptRead(path => path === verification.TIMENS_PATH ? 'monotonic 0 1\nboottime 0 0\n' : undefined)
    expect(() => verification.openVerifiedStore(':memory:')).toThrow('non-zero')
  })
  for (const value of [undefined, 'monotonic nonsense 0\nboottime 0 0\n', 'monotonic 0 0\nboottime 0 bad\n', 'monotonic 0 0\n']) {
    test(`18 absent or unparseable clock ${value}`, () => {
      interceptRead(path => {
        if (path !== verification.TIMENS_PATH) return
        if (value === undefined) throw new Error('absent')
        return value
      })
      expect(() => verification.openVerifiedStore(':memory:')).toThrow()
    })
  }
  for (const kind of ['missing', 'malformed', 'version', 'unknown']) {
    test(`19 invalid record ${kind}`, () => {
      const f = fileFixture()
      if (kind === 'missing') fs.unlinkSync(f.record)
      else if (kind === 'malformed') fs.writeFileSync(f.record, '{')
      else { f.data[kind === 'version' ? 'record_version' : 'extra'] = 2; f.save() }
      expect(f.open).toThrow()
    })
  }
  test('20 inode above 2pow53 exact and one-off refuses', () => {
    const f = fileFixture(), original = fs.statSync
    const high = 2n ** 53n + 1n
    f.data.inode = high.toString(); f.save()
    const mock = spyOn(fs, 'statSync').mockImplementation(((path: any, options: any) => {
      const result = original(path, options)
      return String(path) === f.path ? { ...result, ino: high } : result
    }) as any)
    cleanups.push(() => mock.mockRestore())
    f.open().close()
    f.data.inode = (high + 1n).toString(); f.save()
    expect(f.open).toThrow('inode mismatch')
  })
  test('21 port mismatch refuses', () => {
    const f = fileFixture(); f.data.port = 'python'; f.save()
    expect(f.open).toThrow('port mismatch')
  })
  test('22 missing store never created', () => {
    const f = fileFixture(); fs.unlinkSync(f.path)
    expect(f.open).toThrow()
    expect(fs.existsSync(f.path)).toBe(false)
  })
  test('23 post-open replacement refuses and closes', () => {
    const f = fileFixture(), original = fs.statSync
    let count = 0
    const mock = spyOn(fs, 'statSync').mockImplementation(((path: any, options: any) => {
      const result = original(path, options)
      if (String(path) === f.path && ++count === 3) return { ...result, ino: (result as any).ino + 1n }
      return result
    }) as any)
    cleanups.push(() => mock.mockRestore())
    const close = spyOn(Database.prototype, 'close')
    cleanups.push(() => close.mockRestore())
    expect(f.open).toThrow('changed while opening')
    expect(close).toHaveBeenCalledTimes(1)
  })
  for (const backing of ['network', 'unknown']) {
    test(`24 ${backing} backing refuses`, () => {
      const f = fileFixture(); f.data.storage_evidence.backing = backing; f.save()
      expect(f.open).toThrow('backing')
    })
  }
  test('25 six-column live vs eight-column record refuses', () => {
    const f = fileFixture(), raw = new Database(f.path)
    raw.exec('ALTER TABLE envelope_dedup_v2 DROP COLUMN lease_until_mono_ms; ALTER TABLE envelope_dedup_v2 DROP COLUMN lease_boot_id')
    raw.close()
    expect(f.open).toThrow('live/record')
  })
  test('27 memory requires no record but still checks clock and boot', () => {
    verification.openVerifiedStore(':memory:').close()
    interceptRead(path => path === verification.TIMENS_PATH ? 'monotonic 1 0\nboottime 0 0\n' : undefined)
    expect(() => verification.openVerifiedStore(':memory:')).toThrow('non-zero')
  })
  test('28 devno reboot refuses despite unchanged numbers', () => {
    const f = fileFixture(); f.data.device.attested_boot_id = OTHER; f.save()
    expect(f.open).toThrow('another boot')
  })
  test('28 uuid resolution failure cannot downgrade', () => {
    const f = fileFixture(); f.data.device = { binding: 'uuid', fs_uuid: 'unresolvable' }; f.save()
    expect(f.open).toThrow('UUID')
  })
  for (const device of [{ binding: 'uuid', fs_uuid: 'x', major: 8 }, { binding: 'devno', major: '8:1', minor: 1, attested_boot_id: OTHER }]) {
    test(`28 malformed device ${JSON.stringify(device)}`, () => {
      const f = fileFixture(); f.data.device = device; f.save()
      expect(f.open).toThrow()
    })
  }
  test('29a mountinfo is never read, regardless of representation', () => {
    const f = fileFixture()
    interceptRead(path => {
      if (/^\/proc\/(self|thread-self|[0-9]+)\/mountinfo$/.test(path)) throw new Error('must not read mountinfo')
    })
    f.open().close()
  })
  test('29b actual resolved device change refuses', () => {
    const f = fileFixture(), original = fs.statSync
    const mock = spyOn(fs, 'statSync').mockImplementation(((path: any, options: any) => {
      const result = original(path, options)
      return String(path) === f.path ? { ...result, dev: 999999n } : result
    }) as any)
    cleanups.push(() => mock.mockRestore())
    expect(f.open).toThrow('device mismatch')
  })
  test('30 matching six-column live and record still refuses', () => {
    const f = fileFixture(), raw = new Database(f.path)
    raw.exec('ALTER TABLE envelope_dedup_v2 DROP COLUMN lease_until_mono_ms; ALTER TABLE envelope_dedup_v2 DROP COLUMN lease_boot_id')
    f.data.schema_fingerprint = verification.fingerprint(raw); f.save(); raw.close()
    expect(f.open).toThrow('unsupported schema')
  })
  test('WAL must already be active; startup does not repair it', () => {
    const f = fileFixture(), raw = new Database(f.path)
    raw.exec('PRAGMA journal_mode=DELETE'); raw.close()
    expect(f.open).toThrow('WAL inactive')
  })
})

function pythonClock(input: object): any {
  const result = spawnSync(process.env.PYTHON ?? 'python3', ['conformance/lease-clock.py'], {
    input: JSON.stringify(input), encoding: 'utf8',
    env: { ...process.env, PYTHONPATH: 'yugo' },
  })
  expect(result.status, result.stderr).toBe(0)
  return JSON.parse(result.stdout)
}

test('9 cross-port same normalized behavior', () => {
  clock()
  const s = new DurableEnvelopeDedupStore(':memory:')
  const steps: [number, number][] = [[100000, 100000], [1000000, 100000], [-1000, 160000], [1000000, 160000]]
  const outputs = steps.map(([wall, value], index) => {
    mono = value
    const result = s.claim('e', index === 0 ? 'original' : 'rival', wall)
    const r = row(s)
    return [result.duplicate, result.reqId, r.state, r.lease_boot_id, r.lease_until_mono_ms]
  })
  expect(pythonClock({ mode: 'parity', steps })).toEqual(outputs)
})

test.skipIf(process.env.YUGO_QUALIFIED_CLOCK_TEST !== '1')('13 and 15 cross-port expiry agrees using actual named clock APIs', () => {
  // The ports MUST NOT share a SQLite file. Transfer the common lease metadata
  // for one logical row into each port's own schema, not the wall-clock columns.
  const s = new DurableEnvelopeDedupStore(':memory:')
  s.claim('e', 'original', 100000)
  const r = row(s)
  const before = monotonic.readMonotonicNs()
  const live = pythonClock({ mode: 'actual', boot: r.lease_boot_id, deadline: r.lease_until_mono_ms })
  const after = monotonic.readMonotonicNs()
  expect(BigInt(live.before)).toBeGreaterThanOrEqual(before)
  expect(BigInt(live.after)).toBeLessThanOrEqual(after)
  expect(live.boot).toBe(r.lease_boot_id)
  expect(live.duplicate).toBe(true)
  expect(s.claim('e', 'rival', 100000).duplicate).toBe(true)
  const expired = Number(before / 1_000_000n) - 1
  db(s).query('UPDATE envelope_dedup_v2 SET lease_until_mono_ms=?').run(expired)
  expect(pythonClock({ mode: 'actual', boot: r.lease_boot_id, deadline: expired }).duplicate).toBe(false)
  expect(s.claim('e', 'rival', 100000).duplicate).toBe(false)
})
