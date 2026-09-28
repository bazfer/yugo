import { expect, test } from 'bun:test'
import {
  checkedDeadline, createTimespecBuffer, nanosecondsToMilliseconds,
  validatedNanoseconds,
} from './monotonic-clock'

test('success without writing either timespec field is rejected', () => {
  const output = createTimespecBuffer()
  expect([...output]).toEqual([-1n, -1n])
  expect(() => validatedNanoseconds(0, output)).toThrow('invalid')
})

for (const written of [0, 1]) {
  test(`success writing only timespec field ${written} is rejected`, () => {
    const output = createTimespecBuffer()
    output[written] = 0n
    expect(() => validatedNanoseconds(0, output)).toThrow('invalid')
  })
}

test('native failure is checked before reading any output', () => {
  const output = new Proxy(createTimespecBuffer(), {
    get() { throw new Error('output was read') },
  })
  expect(() => validatedNanoseconds(-1, output)).toThrow('clock_gettime failed')
})

test('zero seconds and zero nanoseconds are valid when actually written', () => {
  const output = createTimespecBuffer()
  output[0] = 0n
  output[1] = 0n
  expect(validatedNanoseconds(0, output)).toBe(0n)
})

for (const fields of [[-1n, 0n], [0n, -1n], [0n, 1_000_000_000n]]) {
  test(`invalid native output ${fields} is rejected`, () => {
    expect(() => validatedNanoseconds(0, new BigInt64Array(fields))).toThrow('invalid')
  })
}

test('bigint arithmetic preserves nanoseconds beyond Number integer precision', () => {
  const sec = 10_000_000n
  expect(validatedNanoseconds(0, new BigInt64Array([sec, 123_456_789n])))
    .toBe(sec * 1_000_000_000n + 123_456_789n)
  expect(nanosecondsToMilliseconds(sec * 1_000_000_000n + 123_456_789n))
    .toBe(10_000_000_123)
  expect(() => nanosecondsToMilliseconds(BigInt(Number.MAX_SAFE_INTEGER + 1) * 1_000_000n))
    .toThrow('unsafe')
  expect(() => checkedDeadline(Number.MAX_SAFE_INTEGER, 1)).toThrow('overflow')
})
