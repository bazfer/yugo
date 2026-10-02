/**
 * Tests for the Discord POST path.
 *
 * Exists because Ohm's review of PR #67 said plainly that the helper tests
 * "exercise neither network behavior" — the reconnect and timeout edits had no
 * coverage at all. The two that matter are the aborted POST followed by a
 * successful one, and the startup validation of the timeout.
 *
 * Run: bun test tools/fleet-bus/tap-post.test.ts
 */
import { expect, test, describe } from 'bun:test'
import { postOne, validateTimeout, type FetchLike, type PostConfig } from './tap-post'

const cfg = (over: Partial<PostConfig> = {}): PostConfig => ({
  webhook: 'https://example.invalid/webhook',
  channel: '123',
  timeoutMs: 50,
  ...over,
})

const item = { content: 'hello', username: 'deet' }

const ok: FetchLike = async () => ({ ok: true, status: 204, text: async () => '' })

describe('validateTimeout()', () => {
  test('accepts a positive integer', () => {
    expect(validateTimeout('5000', 15_000)).toBe(5000)
  })
  test('uses the fallback when unset', () => {
    expect(validateTimeout(undefined, 15_000)).toBe(15_000)
  })

  // Each of these made AbortSignal.timeout throw on EVERY queued message, which
  // drain() then logged as a Discord error — an environment typo presenting as a
  // network outage. They must fail at startup instead.
  for (const bad of ['abc', '-1', '0', '1.5', '', 'NaN', 'Infinity']) {
    test(`rejects ${JSON.stringify(bad)} at startup rather than per message`, () => {
      expect(() => validateTimeout(bad, 15_000)).toThrow(/positive integer/)
    })
  }

  test('the error names the offending value, so the fix is obvious', () => {
    expect(() => validateTimeout('abc', 15_000)).toThrow(/"abc"/)
  })
})

describe('postOne()', () => {
  test('a successful post reports ok', async () => {
    expect(await postOne(item, cfg(), ok)).toEqual({ ok: true })
  })

  test('a non-2xx response is reported, not thrown', async () => {
    const rateLimited: FetchLike = async () => ({
      ok: false, status: 429, text: async () => 'rate limited',
    })
    const out = await postOne(item, cfg(), rateLimited)
    expect(out.ok).toBe(false)
    expect(out.error).toContain('429')
  })

  test('a thrown fetch is reported, not propagated', async () => {
    const boom: FetchLike = async () => { throw new Error('socket closed') }
    const out = await postOne(item, cfg(), boom)
    expect(out.ok).toBe(false)
    expect(out.error).toContain('socket closed')
  })

  test('ABORTED POST THEN A GOOD ONE: the queue keeps moving', async () => {
    // The finding behind the timeout fix. A hung POST held `draining` true
    // forever, so every later drain() returned immediately and the queue stopped
    // silently. The abort must surface as a failed outcome and the NEXT item must
    // still go out.
    let calls = 0
    const hangThenSucceed: FetchLike = async (_url, init) => {
      calls += 1
      if (calls === 1) {
        // Never settles on its own — only the injected signal ends it.
        return await new Promise((_resolve, reject) => {
          const signal = (init as { signal?: AbortSignal }).signal
          signal?.addEventListener('abort', () => reject(new Error('The operation timed out.')))
        })
      }
      return { ok: true, status: 204, text: async () => '' }
    }

    const first = await postOne(item, cfg({ timeoutMs: 30 }), hangThenSucceed)
    expect(first.ok).toBe(false)
    expect(first.error).toMatch(/timed out|abort/i)

    const second = await postOne(item, cfg({ timeoutMs: 30 }), hangThenSucceed)
    expect(second).toEqual({ ok: true })
    expect(calls).toBe(2)
  })

  test('a hang without a timeout would never settle — the control for the test above', async () => {
    // Proves the previous test passes because the TIMEOUT fired, not because the
    // stub happened to resolve. Without a signal this promise never settles, so
    // we race it against a short timer and assert the timer wins.
    const hangForever: FetchLike = () => new Promise(() => {})
    const raced = await Promise.race([
      postOne(item, { ...cfg(), timeoutMs: 1_000_000 }, hangForever).then(() => 'post-settled'),
      new Promise((r) => setTimeout(() => r('timer-won'), 60)),
    ])
    expect(raced).toBe('timer-won')
  })

  test('uses the bot-token path when no webhook is configured', async () => {
    let seenUrl = ''
    let seenAuth = ''
    const capture: FetchLike = async (url, init) => {
      seenUrl = url
      seenAuth = (init.headers as Record<string, string>)['Authorization'] ?? ''
      return { ok: true, status: 204, text: async () => '' }
    }
    await postOne(item, cfg({ webhook: undefined, token: 'T', channel: '999' }), capture)
    expect(seenUrl).toContain('/channels/999/messages')
    expect(seenAuth).toBe('Bot T')
  })

  test('the webhook path carries the username, the bot path does not', async () => {
    let body = ''
    const capture: FetchLike = async (_u, init) => {
      body = init.body as string
      return { ok: true, status: 204, text: async () => '' }
    }
    await postOne(item, cfg(), capture)
    expect(JSON.parse(body).username).toBe('deet')

    await postOne(item, cfg({ webhook: undefined, token: 'T' }), capture)
    expect(JSON.parse(body).username).toBeUndefined()
  })
})
