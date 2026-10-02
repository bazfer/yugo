/**
 * Fleet bus — thin wrapper around NATS for structured bot-to-bot comms.
 *
 * Every message uses a standard envelope; every recipient is named, not
 * channel-hopped. See ~/vault/infra/agent-comms-substrate-eval.md for the
 * broader design.
 */
import { connect, StringCodec, headers, type NatsConnection, type Msg } from 'nats'
import { randomUUID } from 'node:crypto'

const sc = StringCodec()
const NATS_URL = process.env.FLEET_BUS_URL || 'nats://127.0.0.1:4222'

export interface Envelope<P = unknown> {
  id: string              // uuid, generated on publish if absent
  from: string            // bot name — 'luna', 'ohm', 'fernando' (via bridge), etc.
  to?: string             // optional; direct recipient. Absent = broadcast/topic
  kind: string            // e.g. 'pr_review_request', 'status_ping', 'text_message'
  in_reply_to?: string    // envelope id this message is a response to
  ts: string              // ISO8601
  payload: P              // arbitrary structured JSON
}

export class Bus {
  private nc!: NatsConnection
  private botName: string

  constructor(botName: string) {
    this.botName = botName
  }

  async connect(): Promise<void> {
    this.nc = await connect({ servers: NATS_URL, name: `bot-${this.botName}` })
    process.stderr.write(`[bus] ${this.botName} connected to ${NATS_URL}\n`)
  }

  async close(): Promise<void> {
    await this.nc.drain()
  }

  /** Publish an envelope to a subject. Fills in id/from/ts if absent. */
  async publish(subject: string, partial: Partial<Envelope> & Pick<Envelope, 'kind' | 'payload'>): Promise<Envelope> {
    const env: Envelope = {
      id: partial.id ?? randomUUID(),
      from: partial.from ?? this.botName,
      to: partial.to,
      kind: partial.kind,
      in_reply_to: partial.in_reply_to,
      ts: partial.ts ?? new Date().toISOString(),
      payload: partial.payload,
    }
    this.nc.publish(subject, sc.encode(JSON.stringify(env)))
    return env
  }

  /** Subscribe to a subject with wildcard support (`fleet.luna.request`, `fleet.*.status`, `fleet.>`) */
  subscribe<P = unknown>(subject: string, handler: (env: Envelope<P>, subject: string) => void | Promise<void>): () => void {
    const sub = this.nc.subscribe(subject)
    ;(async () => {
      for await (const msg of sub) {
        try {
          const env = JSON.parse(sc.decode(msg.data)) as Envelope<P>
          await handler(env, msg.subject)
        } catch (err) {
          process.stderr.write(`[bus] ${this.botName} handler error on ${msg.subject}: ${err}\n`)
        }
      }
    })().catch(err => process.stderr.write(`[bus] sub loop error: ${err}\n`))
    return () => sub.unsubscribe()
  }

  /**
   * Request-reply. Publishes to `subject`, waits up to `timeoutMs` for one
   * reply on the auto-generated inbox subject. Handlers on the other side
   * should `msg.respond(...)` — see reply() helper below.
   */
  async request<P = unknown, R = unknown>(
    subject: string,
    partial: Partial<Envelope<P>> & Pick<Envelope<P>, 'kind' | 'payload'>,
    timeoutMs = 30_000,
  ): Promise<Envelope<R>> {
    const env: Envelope<P> = {
      id: partial.id ?? randomUUID(),
      from: partial.from ?? this.botName,
      to: partial.to,
      kind: partial.kind,
      in_reply_to: partial.in_reply_to,
      ts: partial.ts ?? new Date().toISOString(),
      payload: partial.payload,
    }
    const msg = await this.nc.request(subject, sc.encode(JSON.stringify(env)), { timeout: timeoutMs })
    return JSON.parse(sc.decode(msg.data)) as Envelope<R>
  }

  /**
   * Subscribe as a request/reply responder. Handler returns the reply
   * payload; wrapper handles envelope construction + msg.respond().
   */
  respond<Q = unknown, R = unknown>(
    subject: string,
    handler: (req: Envelope<Q>, subject: string) => R | Promise<R>,
    replyKind?: string,
  ): () => void {
    const sub = this.nc.subscribe(subject)
    ;(async () => {
      for await (const msg of sub) {
        try {
          const req = JSON.parse(sc.decode(msg.data)) as Envelope<Q>
          const result = await handler(req, msg.subject)
          const reply: Envelope<R> = {
            id: randomUUID(),
            from: this.botName,
            to: req.from,
            kind: replyKind ?? `${req.kind}.reply`,
            in_reply_to: req.id,
            ts: new Date().toISOString(),
            payload: result,
          }
          msg.respond(sc.encode(JSON.stringify(reply)))
        } catch (err) {
          process.stderr.write(`[bus] ${this.botName} responder error on ${msg.subject}: ${err}\n`)
          if (msg.reply) msg.respond(sc.encode(JSON.stringify({ error: String(err) })))
        }
      }
    })().catch(err => process.stderr.write(`[bus] respond loop error: ${err}\n`))
    return () => sub.unsubscribe()
  }
}

/** Convenience — connect a named bot and return the Bus instance. */
export async function attach(botName: string): Promise<Bus> {
  const bus = new Bus(botName)
  await bus.connect()
  return bus
}
