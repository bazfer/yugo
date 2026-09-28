/** Test-only clock fault probe for SPEC-26 §6 item 15b; never used in a consumer.
 *
 * Injects at the NATIVE ADAPTER BOUNDARY, not at `validatedNanoseconds`: the
 * adapter's own `dlopen`, buffer construction, pointer alignment check, return
 * code ordering, field validation and bigint conversion all run unchanged, and
 * only the native result is controlled. A separate process per case guarantees
 * the adapter's cached library handle is cold before the spy is installed.
 *
 * Startup succeeds with the real syscall; the fault is armed only afterwards, so
 * every refusal observed here is a POST-STARTUP one — the guarantee test 18a
 * (startup ordering) does not cover.
 */
import { spyOn } from 'bun:test'
import * as ffi from 'bun:ffi'

const config = JSON.parse(process.argv[2]!) as {
  fault: string
  operation: 'claim' | 'takeover' | 'renew'
  path: string
  owner?: string
}
const LEASE_MS = 60_000
const HELD = 'held'
const FRESH = 'fresh'

const definition = {
  clock_gettime: { args: ['i32', 'ptr'], returns: 'i32' },
  gnu_get_libc_version: { args: [], returns: 'cstring' },
} as const
// Captured before the spy: the good phase calls the real syscall, so the
// pre-fault state this probe reports is produced by the production clock.
const real = ffi.dlopen('libc.so.6', definition)
let faulted = false

function timespec(address: number): BigInt64Array {
  return new BigInt64Array(ffi.toArrayBuffer(address, 0, 16))
}

function injected(address: number): number {
  const view = timespec(address)
  switch (config.fault) {
    // A perfectly valid output behind a failure code: the adapter must refuse on
    // the return code alone, without trusting the buffer.
    case 'nonzero': view[0] = 1n; view[1] = 0n; return -1
    case 'no-write': return 0
    case 'partial-write': view[0] = 0n; return 0
    case 'negative-seconds': view[0] = -1n; view[1] = 0n; return 0
    case 'negative-nanoseconds': view[0] = 0n; view[1] = -1n; return 0
    case 'nanoseconds-out-of-range': view[0] = 0n; view[1] = 1_000_000_000n; return 0
    // Nanoseconds convert to more than 2^53-1 milliseconds.
    case 'unsafe-milliseconds': view[0] = 9_007_199_254_741n; view[1] = 0n; return 0
    // Exactly 2^53-1 milliseconds, so only the lease deadline overflows.
    case 'deadline-overflow': view[0] = 9_007_199_254_740n; view[1] = 991_000_000n; return 0
    // SPEC-26 §1a.1: tv_sec == 0 is VALID and must be accepted.
    case 'zero-seconds': view[0] = 0n; view[1] = 123_000_000n; return 0
    default: throw new Error(`unknown fault ${config.fault}`)
  }
}

const load = spyOn(ffi as any, 'dlopen')
load.mockImplementation(() => {
  if (config.fault === 'initialisation') throw new Error('forced clock initialisation failure')
  return {
    symbols: {
      clock_gettime: (clockId: number, address: number) =>
        faulted ? injected(address) : real.symbols.clock_gettime(clockId, address),
      gnu_get_libc_version: () => real.symbols.gnu_get_libc_version(),
    },
    close() {},
  }
})

const { DurableEnvelopeDedupStore, DEFAULT_DEDUP_TTL_MS } = await import('../src/fleet-bus.ts')

function reason(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
function emit(report: Record<string, unknown>): never {
  console.log(JSON.stringify(report))
  process.exit(0)
}

let store: InstanceType<typeof DurableEnvelopeDedupStore>
try {
  store = new DurableEnvelopeDedupStore(config.path, DEFAULT_DEDUP_TTL_MS, LEASE_MS)
} catch (error) {
  emit({ constructed: false, refused: true, message: reason(error) })
}
const db = (store as any).db
const rows = () => db.query('SELECT * FROM envelope_dedup_v2 ORDER BY envelope_id').all()

let owner = config.owner
if (config.path === ':memory:') {
  // A per-process store has no cross-process pre-state; build it with the real
  // syscall still in force, exactly as the file-backed parent does.
  owner = store.claim(HELD, 'owner-req').owner
  if (config.operation === 'takeover') db.exec('UPDATE envelope_dedup_v2 SET lease_until_mono_ms=1')
}

const before = rows()
faulted = true
const report: Record<string, unknown> = { constructed: true, before }
try {
  const result = config.operation === 'renew'
    ? { renewed: store.renew(HELD, owner!) }
    : { claim: store.claim(config.operation === 'claim' ? FRESH : HELD, 'rival-req') }
  report.refused = false
  report.result = result
} catch (error) {
  report.refused = true
  report.message = reason(error)
}
// Row reads need no clock, so the post-operation state is observable even while
// the adapter is still refusing.
report.after = rows()
emit(report)
