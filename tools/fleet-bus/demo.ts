/**
 * fleet-bus demo — proves round-trip pub/sub + request/reply between two
 * simulated bots on the same NATS server.
 *
 * Run: bun demo.ts
 * Requires NATS reachable at $FLEET_BUS_URL (default nats://127.0.0.1:4222).
 */
import { attach, type Envelope } from './bus.ts'

async function main() {
  // Simulate Ohm — responds to pr_review_request with a verdict
  const ohm = await attach('ohm')
  ohm.respond<{ repo: string; pr: number }, { verdict: 'clean' | 'issues'; note: string }>(
    'fleet.ohm.request',
    (req) => {
      console.log(`[ohm]  received from ${req.from}:`, req.kind, req.payload)
      // Fake review: PR # even → clean, odd → issues
      const clean = req.payload.pr % 2 === 0
      return {
        verdict: clean ? 'clean' : 'issues',
        note: clean ? `PR #${req.payload.pr} looks good` : `PR #${req.payload.pr} has 1 finding`,
      }
    },
    'pr_review_result',
  )

  // Simulate Luna — subscribes to a broadcast + issues a request to Ohm
  const luna = await attach('luna')
  luna.subscribe<{ msg: string }>('fleet.broadcast.>', (env, subj) => {
    console.log(`[luna] broadcast on ${subj} from ${env.from}:`, env.payload.msg)
  })

  // Wait a beat for subscriptions to register
  await new Promise((r) => setTimeout(r, 100))

  // === Scenario 1: pub/sub broadcast ===
  console.log('\n--- scenario 1: broadcast ---')
  await luna.publish('fleet.broadcast.ping', {
    kind: 'status_ping',
    payload: { msg: 'anyone alive?' },
  })
  await new Promise((r) => setTimeout(r, 100))

  // === Scenario 2: request/reply ===
  console.log('\n--- scenario 2: request/reply ---')
  const reply = await luna.request<{ repo: string; pr: number }, { verdict: string; note: string }>(
    'fleet.ohm.request',
    { kind: 'pr_review_request', payload: { repo: 'artifice-ia/claude-container', pr: 42 } },
    5000,
  )
  console.log(`[luna] got reply from ${reply.from}:`, reply.payload, `(in_reply_to=${reply.in_reply_to})`)

  const reply2 = await luna.request<{ repo: string; pr: number }, { verdict: string; note: string }>(
    'fleet.ohm.request',
    { kind: 'pr_review_request', payload: { repo: 'artifice-ia/claude-container', pr: 43 } },
    5000,
  )
  console.log(`[luna] got reply from ${reply2.from}:`, reply2.payload)

  console.log('\n--- demo complete ---')
  await luna.close()
  await ohm.close()
}

main().catch((err) => {
  console.error('demo failed:', err)
  process.exit(1)
})
