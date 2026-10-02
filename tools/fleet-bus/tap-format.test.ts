/**
 * Tests for the tap's pure helpers.
 *
 * The three crash tests below are the point of this file. Each one FAILS against
 * the pre-fix implementation with an uncaught TypeError — `.slice is not a
 * function` — which is what terminated the live tap. Each names the field that
 * carried the bad value, so a regression says which one came back.
 *
 * Run: bun test tools/fleet-bus/tap-format.test.ts
 */
import { expect, test, describe } from 'bun:test'
import { short, normalizeUsername, format, dedupeKey, statusStateHash, isEnvelope } from './tap-format'

const env = (over: Record<string, unknown> = {}) => ({
  envelope_version: 1,
  id: 'abcdef0123456789',
  from: 'deet',
  to: 'vec',
  kind: 'text_message',
  ...over,
})

describe('a non-string value cannot crash the tap', () => {
  // Pre-fix these three threw TypeError and killed the process. The Envelope
  // interface declares these fields `string`, but that is compile-time only —
  // the value is parsed from JSON off the wire.

  test('numeric id is coerced, not sliced blindly', () => {
    expect(() => format('fleet.vec.request', env({ id: 42 }))).not.toThrow()
    expect(format('fleet.vec.request', env({ id: 42 }))).toContain('`42`')
  })

  test('numeric in_reply_to is coerced', () => {
    expect(() => format('fleet.vec.request', env({ in_reply_to: 7 }))).not.toThrow()
    expect(format('fleet.vec.request', env({ in_reply_to: 7 }))).toContain('`7`')
  })

  test('numeric from does not crash username normalisation', () => {
    // The third site, reached via tap.ts's `normalizeUsername(parsed.from)`.
    expect(() => normalizeUsername(42)).not.toThrow()
    expect(normalizeUsername(42)).toBe('42')
  })

  test('object and boolean values are survivable too', () => {
    expect(() => format('fleet.vec.request', env({ id: { a: 1 }, in_reply_to: true }))).not.toThrow()
    expect(() => normalizeUsername({ a: 1 })).not.toThrow()
  })
})

describe('short()', () => {
  test('truncates to the requested length', () => {
    expect(short('abcdef0123456789', 8)).toBe('abcdef01')
  })
  test('null and undefined give null, so callers render the ? placeholder', () => {
    expect(short(null, 8)).toBeNull()
    expect(short(undefined, 8)).toBeNull()
  })
  test('empty string gives null rather than an empty backtick pair', () => {
    expect(short('', 8)).toBeNull()
  })
  test('zero is NOT treated as absent — it is a real id', () => {
    // `parsed.id ? ...` was falsy for 0 and dropped it. String(0) is '0'.
    expect(short(0, 8)).toBe('0')
  })
})

describe('normalizeUsername()', () => {
  test('absent name falls back', () => {
    expect(normalizeUsername(undefined)).toBe('fleet-bus')
    expect(normalizeUsername(null)).toBe('fleet-bus')
    expect(normalizeUsername('')).toBe('fleet-bus')
  })
  test('strips characters Discord rejects', () => {
    expect(normalizeUsername('deet!!@#$')).toBe('deet')
  })
  test('a name that is entirely stripped falls back rather than going empty', () => {
    expect(normalizeUsername('!!!')).toBe('fleet-bus')
  })
  test('caps at 80 characters', () => {
    expect(normalizeUsername('x'.repeat(200)).length).toBe(80)
  })
})

describe('format() — unchanged behaviour', () => {
  test('renders an envelope the same way as before the refactor', () => {
    expect(format('fleet.vec.request', env())).toBe(
      '**deet** → **vec** `text_message` on `fleet.vec.request` `abcdef01`',
    )
  })
  test('absent id renders the ? placeholder', () => {
    expect(format('fleet.vec.request', env({ id: undefined }))).toContain('`?`')
  })
  test('payload over 400 chars is truncated with an ellipsis', () => {
    const out = format('fleet.vec.request', env({ payload: { big: 'y'.repeat(600) } }))
    expect(out).toContain('…')
    expect(out.length).toBeLessThan(700)
  })
  test('a non-envelope falls back to the raw summary', () => {
    expect(format('fleet.vec.status', { online: true })).toContain('(raw on')
  })
})

describe('dedupeKey()', () => {
  test('envelopes key on id', () => {
    expect(dedupeKey('fleet.vec.request', env())).toBe('env:abcdef0123456789')
  })
  test('non-envelopes key on subject and body', () => {
    expect(dedupeKey('fleet.vec.status', { online: true })).toContain('raw:fleet.vec.status')
  })
})

describe('statusStateHash()', () => {
  test('non-status subjects are not hashed', () => {
    expect(statusStateHash('fleet.vec.request', env())).toBeNull()
  })
  test('the same meaningful state hashes identically despite a changed timestamp', () => {
    const a = statusStateHash('fleet.vec.status', { online: true, pid: 1, ts: '1' })
    const b = statusStateHash('fleet.vec.status', { online: true, pid: 1, ts: '2' })
    expect(a).toBe(b)
  })
  test('a changed online flag changes the hash', () => {
    const a = statusStateHash('fleet.vec.status', { online: true, pid: 1 })
    const b = statusStateHash('fleet.vec.status', { online: false, pid: 1 })
    expect(a).not.toBe(b)
  })
})

describe('isEnvelope()', () => {
  test('accepts an envelope_version or a from+kind pair', () => {
    expect(isEnvelope({ envelope_version: 1 })).toBe(true)
    expect(isEnvelope({ from: 'deet', kind: 'text_message' })).toBe(true)
  })
  test('rejects null, primitives and bare objects', () => {
    expect(isEnvelope(null)).toBe(false)
    expect(isEnvelope('x')).toBe(false)
    expect(isEnvelope({ online: true })).toBe(false)
  })
})
