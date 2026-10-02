/**
 * The Discord POST path, extracted so the failure modes can be tested.
 *
 * drain() previously inlined this inside tap.ts, which connects to NATS at module
 * scope — so none of it was reachable from a test. Ohm's review of PR #67: "the
 * 23 tests exercise neither network behavior."
 *
 * `fetchImpl` is injected for exactly that reason. Nothing here does I/O of its
 * own beyond the call it is handed.
 */

export interface PostConfig {
  webhook?: string
  token?: string
  channel: string
  timeoutMs: number
}

export interface QueueItem {
  content: string
  username: string
}

export type FetchLike = (url: string, init: RequestInit) => Promise<{ ok: boolean; status: number; text(): Promise<string> }>

/**
 * Validate the configured timeout ONCE, at startup.
 *
 * AbortSignal.timeout throws a TypeError on NaN, negative and non-integer values.
 * Because drain() catches everything as a post error, a typo in
 * FLEET_BUS_POST_TIMEOUT_MS would silently drop EVERY message while logging what
 * looks like a Discord problem — an outage whose cause is in the environment and
 * whose symptom points at the network. (Ohm, PR #67.)
 */
export function validateTimeout(raw: string | undefined, fallbackMs: number): number {
  const ms = raw === undefined ? fallbackMs : Number(raw)
  if (!Number.isInteger(ms) || ms <= 0) {
    throw new Error(
      'FLEET_BUS_POST_TIMEOUT_MS must be a positive integer number of milliseconds, got ' +
      JSON.stringify(raw),
    )
  }
  return ms
}

export interface PostOutcome {
  ok: boolean
  /** Set when the attempt failed. Logged by the caller; never thrown. */
  error?: string
}

/**
 * Post one queue item. NEVER throws — a failed post must not take down the loop.
 *
 * The timeout matters more than it looks: drain() holds a `draining` flag for the
 * duration, so one hung POST stops the queue moving silently and indefinitely.
 */
export async function postOne(
  item: QueueItem,
  config: PostConfig,
  fetchImpl: FetchLike,
): Promise<PostOutcome> {
  try {
    const url = config.webhook
      ? config.webhook
      : `https://discord.com/api/v10/channels/${config.channel}/messages`
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    if (!config.webhook) headers['Authorization'] = `Bot ${config.token}`
    const body = config.webhook
      ? JSON.stringify({ content: item.content, username: item.username })
      : JSON.stringify({ content: item.content })
    const res = await fetchImpl(url, {
      method: 'POST',
      headers,
      body,
      signal: AbortSignal.timeout(config.timeoutMs),
    })
    if (!res.ok) {
      return { ok: false, error: `discord post failed ${res.status}: ${await res.text()}` }
    }
    return { ok: true }
  } catch (err) {
    return { ok: false, error: `discord post error: ${err}` }
  }
}
