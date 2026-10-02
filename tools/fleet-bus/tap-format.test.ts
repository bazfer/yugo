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
import { short, safeText, normalizeUsername, format, dedupeKey, statusStateHash, isEnvelope } from './tap-format'

/**
 * Ordinary wire JSON that cannot be converted to a primitive.
 *
 * JSON.parse is used deliberately rather than an object literal: the point is that
 * a PUBLISHER can produce this, with no proxies and no functions. Shadowing
 * toString with null leaves String(x) and `${x}` throwing "No default value".
 */
const hostile = () => JSON.parse('{"toString":null}')

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

describe('wire JSON that cannot be converted to a primitive (Ohm, PR #67)', () => {
  // String() and template interpolation BOTH throw on these. The earlier {a:1}
  // case passed because an intact inherited toString renders '[object Object]' —
  // it exercised the wrong thing while the real case still killed the tap.

  test('the fixture really is unconvertible, or these tests prove nothing', () => {
    expect(() => String(hostile())).toThrow()
    expect(() => `${hostile()}`).toThrow()
    // and the escape hatch the fix relies on still works
    expect(JSON.stringify(hostile())).toBe('{"toString":null}')
  })

  for (const field of ['id', 'in_reply_to', 'from', 'to', 'kind'] as const) {
    test(`a hostile ${field} does not throw in format()`, () => {
      expect(() => format('fleet.vec.request', env({ [field]: hostile() }))).not.toThrow()
    })
  }

  test('a hostile id does not throw in dedupeKey', () => {
    // dedupeKey interpolated the id BEFORE its try block, so this killed the
    // request loop before format() was ever reached.
    expect(() => dedupeKey('fleet.vec.request', env({ id: hostile() }))).not.toThrow()
  })

  test('a hostile from does not throw in normalizeUsername', () => {
    expect(() => normalizeUsername(hostile())).not.toThrow()
  })

  test('a hostile value in a raw (non-envelope) body does not throw', () => {
    expect(() => format('fleet.vec.status', { online: hostile() })).not.toThrow()
  })

  test('a hostile value on a .status subject does not throw in statusStateHash', () => {
    expect(() => statusStateHash('fleet.vec.status', { online: hostile() })).not.toThrow()
  })

  test('malformed then valid: the next message still renders correctly', () => {
    // The sequence that matters. Rendering the bad one must not corrupt or
    // prevent the good one — the loop has to keep observing.
    expect(() => format('fleet.vec.request', env({ id: hostile() }))).not.toThrow()
    expect(format('fleet.vec.request', env())).toBe(
      '**deet** → **vec** `text_message` on `fleet.vec.request` `abcdef01`',
    )
  })
})

describe('safeText() is total', () => {
  test('passes strings through', () => {
    expect(safeText('abc')).toBe('abc')
  })
  test('renders primitives', () => {
    expect(safeText(42)).toBe('42')
    expect(safeText(true)).toBe('true')
    expect(safeText(null)).toBe('null')
    expect(safeText(undefined)).toBe('undefined')
  })
  test('renders an unconvertible object via JSON rather than coercion', () => {
    expect(safeText(hostile())).toBe('{"toString":null}')
  })
  test('survives a circular structure', () => {
    const circular: Record<string, unknown> = { a: 1 }
    circular.self = circular
    expect(() => safeText(circular)).not.toThrow()
    expect(safeText(circular)).toBe('[unrenderable]')
  })
  test('survives a throwing toJSON', () => {
    const bomb = { toJSON() { throw new Error('boom') } }
    expect(() => safeText(bomb)).not.toThrow()
    expect(safeText(bomb)).toBe('[unrenderable]')
  })
  test('survives a symbol, which JSON.stringify returns undefined for', () => {
    expect(safeText(Symbol('s'))).toBe('[unrenderable]')
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
