/**
 * Entrypoint smoke tests for tap.ts.
 *
 * WHY THIS FILE EXISTS.
 *
 * tap.ts used postOne() and validateTimeout() without importing them. Everything I
 * checked passed anyway:
 *
 *   - 57 unit tests passed. They import tap-format and tap-post DIRECTLY and never
 *     load tap.ts.
 *   - `bun build tap.ts` exited 0. Bundling does not resolve a module-scope reference.
 *
 * The real entrypoint died on its first line of work with
 * `ReferenceError: validateTimeout is not defined`. Ohm found it by running the file.
 *
 * Extracting code to make it testable moved the tested surface away from the thing that
 * actually runs. These tests run the entrypoint. (Ohm, PR 67.)
 */
import { expect, test, describe } from 'bun:test'

const ENTRY = new URL('./tap.ts', import.meta.url).pathname

/** Run tap.ts as a real process and return what it printed and how it exited. */
async function runTap(env: Record<string, string>, ms = 4000) {
  const proc = Bun.spawn(['bun', ENTRY], {
    env: { ...process.env, ...env },
    stdout: 'pipe',
    stderr: 'pipe',
  })
  const timer = setTimeout(() => proc.kill(), ms)
  const [stdout, stderr] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
  ])
  await proc.exited
  clearTimeout(timer)
  return { stdout, stderr, code: proc.exitCode }
}

describe('tap.ts loads and reaches its configuration checks', () => {
  test('no ReferenceError: every identifier it uses is imported', async () => {
    // The regression. Unresolved module-scope identifiers surface here and nowhere else.
    const { stderr } = await runTap({
      FLEET_BUS_CONSOLE_PASS: 'x',
      FLEET_BUS_WEBHOOK_URL: 'http://127.0.0.1:1/never',
      FLEET_BUS_URL: 'nats://127.0.0.1:1',
    })
    expect(stderr).not.toContain('ReferenceError')
    expect(stderr).not.toContain('is not defined')
  })

  test('a missing password is refused by name', async () => {
    const { stderr } = await runTap({
      FLEET_BUS_CONSOLE_PASS: '',
      FLEET_BUS_WEBHOOK_URL: 'http://127.0.0.1:1/never',
    })
    expect(stderr).toContain('FLEET_BUS_CONSOLE_PASS required')
  })

  test('a missing destination is refused by name', async () => {
    const { stderr } = await runTap({
      FLEET_BUS_CONSOLE_PASS: 'x',
      FLEET_BUS_WEBHOOK_URL: '',
      DISCORD_BOT_TOKEN: '',
    })
    expect(stderr).toContain('FLEET_BUS_WEBHOOK_URL')
  })

  test('a bad timeout is refused AT STARTUP, naming the offending value', async () => {
    // Without the startup check this surfaces per message, logged as a Discord error —
    // an environment typo presenting as a network outage.
    const { stderr } = await runTap({
      FLEET_BUS_CONSOLE_PASS: 'x',
      FLEET_BUS_WEBHOOK_URL: 'http://127.0.0.1:1/never',
      FLEET_BUS_POST_TIMEOUT_MS: 'abc',
      FLEET_BUS_URL: 'nats://127.0.0.1:1',
    })
    expect(stderr).toContain('positive integer')
    expect(stderr).toContain('"abc"')
  })

  test('a valid config gets PAST the checks and tries to reach NATS', async () => {
    // The control for the three refusals above. Without it they all pass against a file
    // that refuses everything, which proves nothing about the good path.
    const { stderr } = await runTap({
      FLEET_BUS_CONSOLE_PASS: 'x',
      FLEET_BUS_WEBHOOK_URL: 'http://127.0.0.1:1/never',
      FLEET_BUS_POST_TIMEOUT_MS: '5000',
      FLEET_BUS_URL: 'nats://127.0.0.1:1',
    })
    expect(stderr).not.toContain('FLEET_BUS_CONSOLE_PASS required')
    expect(stderr).not.toContain('positive integer')
    expect(stderr).not.toContain('ReferenceError')
  })
})
