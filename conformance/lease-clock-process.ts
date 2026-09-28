/** Test-only lease peer for SPEC-26 §6 item 15a; never used in a consumer.
 *
 * One long-lived Bun process driving the PRODUCTION lease path against a shared
 * verified store. The parent spawns two of these at different wall times, so the
 * pair differ in process age — the property `process.hrtime.bigint()` exposes and
 * `clock_gettime(CLOCK_MONOTONIC)` does not.
 *
 * Newline-delimited JSON commands on stdin, one JSON response per line on stdout.
 */
import { DurableEnvelopeDedupStore, DEFAULT_DEDUP_TTL_MS } from '../src/fleet-bus.ts'
import { readMonotonicNs } from '../src/monotonic-clock.ts'

const stores = new Map<string, DurableEnvelopeDedupStore>()

function required(key: string): DurableEnvelopeDedupStore {
  const store = stores.get(key)
  if (!store) throw new Error(`no store opened under key ${key}`)
  return store
}

function handle(command: any): unknown {
  switch (command.op) {
    case 'mono':
      // The actual named API, never a mock: the adapter this release exists for.
      return { mono: readMonotonicNs().toString() }
    case 'open':
      stores.set(command.key, new DurableEnvelopeDedupStore(command.path, DEFAULT_DEDUP_TTL_MS, command.leaseMs))
      return { opened: command.key }
    case 'claim':
      return { claim: required(command.key).claim(command.id, command.reqId) }
    case 'renew':
      return { renewed: required(command.key).renew(command.id, command.owner) }
    case 'row':
      return {
        row: (required(command.key) as any).db
          .query('SELECT * FROM envelope_dedup_v2 WHERE envelope_id=?').get(command.id),
      }
    default:
      throw new Error(`unknown op ${command.op}`)
  }
}

let buffer = ''
const decoder = new TextDecoder()
for await (const chunk of Bun.stdin.stream()) {
  buffer += decoder.decode(chunk as Uint8Array, { stream: true })
  let newline: number
  while ((newline = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, newline).trim()
    buffer = buffer.slice(newline + 1)
    if (line === '') continue
    const command = JSON.parse(line)
    if (command.op === 'exit') process.exit(0)
    let response: unknown
    try {
      response = handle(command)
    } catch (error) {
      response = { error: error instanceof Error ? error.message : String(error) }
    }
    await Bun.write(Bun.stdout, JSON.stringify(response) + '\n')
  }
}
