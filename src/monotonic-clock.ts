/**
 * Linux x86_64 LP64, glibc 2.39, Bun 1.3.12 ONLY.
 * Deployment qualification target: Ubuntu 24.04 host (SPEC-26 §1a.2).
 * bun:ffi is experimental: this is a release risk, not a fallback invitation.
 * conformance/clock-abi.c asserts the actual headers' ABI in CI.
 */
import { dlopen, ptr, type Library } from 'bun:ffi'

export class MonotonicClockError extends Error {}
export const CLOCK_MONOTONIC = 1
const definition = {
  clock_gettime: { args: ['i32', 'ptr'], returns: 'i32' },
  gnu_get_libc_version: { args: [], returns: 'cstring' },
} as const

// Retained for the lifetime of every native function reference. Never close a
// successfully initialized library while a consumer can call it.
let library: Library<typeof definition> | undefined

function nativeLibrary(): Library<typeof definition> {
  if (library) return library
  if (process.platform !== 'linux' || process.arch !== 'x64' || process.versions.bun !== '1.3.12') {
    throw new MonotonicClockError('unqualified clock ABI/runtime: require Linux x86_64, glibc 2.39, Bun 1.3.12')
  }
  const loaded = dlopen('libc.so.6', definition)
  try {
    if (String(loaded.symbols.gnu_get_libc_version()) !== '2.39') {
      throw new MonotonicClockError('unqualified clock libc: require glibc 2.39')
    }
    library = loaded
    return loaded
  } catch (error) {
    loaded.close()
    throw error
  }
}

/** Pure native-output boundary, separately exercised with adversarial results. */
export function validatedNanoseconds(result: number, output: BigInt64Array): bigint {
  // MUST precede any output read; C does not promise initialized output on error.
  if (result !== 0) throw new MonotonicClockError('clock_gettime failed')
  const sec = output[0], nsec = output[1]
  if (typeof sec !== 'bigint' || typeof nsec !== 'bigint' || sec < 0n || nsec < 0n || nsec >= 1_000_000_000n) {
    throw new MonotonicClockError('invalid clock_gettime timespec')
  }
  return sec * 1_000_000_000n + nsec
}

/** Invalid in both fields, so success-with-partial-output cannot look like zero. */
export function createTimespecBuffer(): BigInt64Array {
  return new BigInt64Array([-1n, -1n])
}

export function readMonotonicNs(): bigint {
  const native = nativeLibrary()
  // BigInt64Array gives a 16-byte, 8-byte-aligned timespec on the qualified ABI.
  // Keep the view alive across the synchronous native call and subsequent read.
  const output = createTimespecBuffer()
  const address = ptr(output)
  if (address % 8 !== 0 || output.byteLength !== 16) {
    throw new MonotonicClockError('unaligned or incorrectly sized timespec')
  }
  const result = native.symbols.clock_gettime(CLOCK_MONOTONIC, address)
  return validatedNanoseconds(result, output)
}

export function nanosecondsToMilliseconds(ns: bigint): number {
  if (ns < 0n) throw new MonotonicClockError('negative monotonic reading')
  // DIVIDE IN BIGINT before Number conversion, including beyond 104.2 days.
  const ms = ns / 1_000_000n
  if (ms > BigInt(Number.MAX_SAFE_INTEGER)) throw new MonotonicClockError('unsafe monotonic milliseconds')
  return Number(ms)
}

export function readMonotonicMs(): number {
  return nanosecondsToMilliseconds(readMonotonicNs())
}

export function checkedDeadline(mono: number, leaseMs: number): number {
  const deadline = mono + Math.ceil(leaseMs)
  if (!Number.isSafeInteger(mono) || mono < 0 || !Number.isSafeInteger(deadline) || deadline <= mono) {
    throw new MonotonicClockError('monotonic lease deadline overflow')
  }
  return deadline
}
