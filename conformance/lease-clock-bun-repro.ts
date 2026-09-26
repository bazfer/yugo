import { DurableEnvelopeDedupStore, DEFAULT_DEDUP_TTL_MS } from '../src/fleet-bus.ts'
import { provisionTestStore } from '../src/dedup-test-fixtures.ts'
import { mkdtempSync } from 'node:fs'
if (process.argv[2] === 'child') {
  const store = new DurableEnvelopeDedupStore(process.argv[3], DEFAULT_DEDUP_TTL_MS, 200)
  const claim = store.claim('live', 'child')
  console.log(JSON.stringify({ claim, wall: Date.now(), mono: process.hrtime.bigint().toString() }))
  await Bun.sleep(5000) // Keep the original owner process alive.
} else {
  const path = mkdtempSync('/tmp/vec-clock-proof-') + '/db.sqlite'
  process.env.YUGO_DEDUP_VERIFICATION_RECORD = provisionTestStore(path)
  await Bun.sleep(1200)
  const child = Bun.spawn([process.execPath, import.meta.path, 'child', path], { stdout: 'pipe', env: { ...process.env } })
  const reader = child.stdout.getReader()
  const chunk = await reader.read()
  const owner = JSON.parse(new TextDecoder().decode(chunk.value))
  const rival = new DurableEnvelopeDedupStore(path, DEFAULT_DEDUP_TTL_MS, 200)
  const claim = rival.claim('live', 'rival')
  console.log(JSON.stringify({
    runtime: Bun.version, owner, rival_claim: claim,
    owner_process_still_alive: child.exitCode === null,
    wall_ms_since_owner_claim: Date.now() - owner.wall,
    rival_mono_ns: process.hrtime.bigint().toString(),
  }))
  child.kill()
  await child.exited
}
