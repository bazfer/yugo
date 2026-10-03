/**
 * Tests for the deaf-bus detector.
 *
 * Each case names the wrong implementation it rejects. The false-positive cases matter
 * more than the detection cases: an alarm that cries wolf gets muted, and a muted alarm
 * is the same as no alarm. That is how the Falco channel became unreadable.
 */
import { expect, test, describe } from 'bun:test'
import { newState, observeStatus, tick, render, type WatchConfig } from './tap-watch'

const CFG: WatchConfig = {
  expected: ['vec', 'ohm'],
  silenceMs: 90_000,
  graceMs: 90_000,
  reminderMs: 24 * 60 * 60 * 1000,
}
const T0 = 1_000_000

const types = (evs: ReturnType<typeof tick>) => evs.map((e) => e.type)

describe('the detector is OFF unless told what to watch', () => {
  test('an empty expected set reports nothing about bots', () => {
    const s = newState(T0)
    expect(tick(s, { ...CFG, expected: [] }, T0 + 10 * 60_000)).toEqual([])
  })

  test('an empty expected set silences the TAP notices too', () => {
    // This is the case that proves the guard does anything. The bot loop iterates over
    // an empty list regardless, so "no bot events" passes with the guard removed. The
    // guard's only real effect is suppressing bus-lost and bus-regained, which is what
    // "off" has to mean — otherwise disabling the detector still produces traffic.
    const s = newState(T0)
    s.connected = false
    expect(tick(s, { ...CFG, expected: [] }, T0 + 10 * 60_000)).toEqual([])
  })
})

describe('false positives, which are worse than misses', () => {
  test('nothing is reported during the startup grace', () => {
    // A cold tap has seen nobody. That is true of the tap, not of the bots.
    const s = newState(T0)
    expect(tick(s, CFG, T0 + 1000)).toEqual([])
    expect(tick(s, CFG, T0 + CFG.graceMs - 1)).toEqual([])
  })

  test('a bot heartbeating normally is never reported', () => {
    const s = newState(T0)
    let now = T0
    for (let i = 0; i < 20; i++) {
      now += 30_000
      observeStatus(s, 'vec', now)
      observeStatus(s, 'ohm', now)
      expect(tick(s, CFG, now)).toEqual([])
    }
  })

  test('a silent bot is reported ONCE, not on every tick', () => {
    const s = newState(T0)
    observeStatus(s, 'vec', T0); observeStatus(s, 'ohm', T0)
    const now = T0 + CFG.graceMs + CFG.silenceMs
    expect(types(tick(s, CFG, now))).toEqual(['silent', 'silent'])
    expect(tick(s, CFG, now + 1000)).toEqual([])
    expect(tick(s, CFG, now + 60_000)).toEqual([])
  })
})

describe('while the tap is disconnected, every bot looks silent', () => {
  test('a disconnected tap reports its OWN problem, not the fleet', () => {
    // Without this gate the first NATS restart alerts about every bot at once.
    const s = newState(T0)
    s.connected = false
    expect(types(tick(s, CFG, T0 + CFG.graceMs + CFG.silenceMs))).toEqual(['bus-lost'])
  })

  test('it says so once, not on every tick', () => {
    const s = newState(T0)
    s.connected = false
    tick(s, CFG, T0 + 100_000)
    expect(tick(s, CFG, T0 + 200_000)).toEqual([])
  })

  test('reconnecting restarts the grace, so no bot is judged on a window the tap missed', () => {
    const s = newState(T0)
    s.connected = false
    tick(s, CFG, T0 + 100_000)
    s.connected = true
    const back = T0 + 200_000
    expect(types(tick(s, CFG, back))).toEqual(['bus-regained'])
    // Immediately after, still inside the fresh grace: silence about the bots.
    expect(tick(s, CFG, back + 1000)).toEqual([])
  })
})

describe('detection and recovery', () => {
  test('a bot that stops is reported silent', () => {
    const s = newState(T0)
    observeStatus(s, 'vec', T0)
    observeStatus(s, 'ohm', T0)
    const now = T0 + CFG.graceMs + CFG.silenceMs
    observeStatus(s, 'ohm', now) // ohm is fine
    const evs = tick(s, CFG, now)
    expect(evs).toHaveLength(1)
    expect(evs[0]).toMatchObject({ type: 'silent', bot: 'vec' })
  })

  test('a bot never seen at all is reported, not skipped', () => {
    // Keying on who has spoken would make a bot that never connected invisible, which
    // is exactly the failure this detector exists to catch.
    const s = newState(T0)
    observeStatus(s, 'ohm', T0 + 1000)
    const now = T0 + CFG.graceMs + CFG.silenceMs
    observeStatus(s, 'ohm', now)
    const evs = tick(s, CFG, now)
    expect(evs).toHaveLength(1)
    expect(evs[0]).toMatchObject({ type: 'silent', bot: 'vec', lastSeenMs: null })
  })

  test('a bot that comes back is reported recovered, once', () => {
    const s = newState(T0)
    observeStatus(s, 'vec', T0); observeStatus(s, 'ohm', T0)
    const t1 = T0 + CFG.graceMs + CFG.silenceMs
    tick(s, CFG, t1)
    observeStatus(s, 'vec', t1 + 1000); observeStatus(s, 'ohm', t1 + 1000)
    expect(types(tick(s, CFG, t1 + 1000))).toEqual(['recovered', 'recovered'])
    expect(tick(s, CFG, t1 + 2000)).toEqual([])
  })
})

describe('the daily reminder', () => {
  test('nothing repeats before the interval', () => {
    const s = newState(T0)
    observeStatus(s, 'vec', T0); observeStatus(s, 'ohm', T0)
    const t1 = T0 + CFG.graceMs + CFG.silenceMs
    tick(s, CFG, t1)
    expect(tick(s, CFG, t1 + CFG.reminderMs - 1000)).toEqual([])
  })

  test('one line names everything still silent, after the interval', () => {
    const s = newState(T0)
    observeStatus(s, 'vec', T0); observeStatus(s, 'ohm', T0)
    const t1 = T0 + CFG.graceMs + CFG.silenceMs
    tick(s, CFG, t1)
    const evs = tick(s, CFG, t1 + CFG.reminderMs)
    expect(evs).toHaveLength(1)
    expect(evs[0]).toMatchObject({ type: 'still-silent', bots: ['vec', 'ohm'] })
  })

  test('and it does not then repeat immediately', () => {
    const s = newState(T0)
    observeStatus(s, 'vec', T0); observeStatus(s, 'ohm', T0)
    const t1 = T0 + CFG.graceMs + CFG.silenceMs
    tick(s, CFG, t1)
    tick(s, CFG, t1 + CFG.reminderMs)
    expect(tick(s, CFG, t1 + CFG.reminderMs + 1000)).toEqual([])
  })
})

describe('render()', () => {
  test('a silent bot names itself and how long it has been quiet', () => {
    const out = render({ type: 'silent', bot: 'vec', lastSeenMs: T0 }, T0 + 120_000)
    expect(out).toContain('**vec**')
    expect(out).toContain('120s ago')
  })
  test('a never-seen bot says so rather than printing a bogus duration', () => {
    expect(render({ type: 'silent', bot: 'vec', lastSeenMs: null }, T0)).toContain('never seen')
  })
  test('the still-silent line lists every bot', () => {
    const out = render({ type: 'still-silent', bots: ['vec', 'ohm'] }, T0)
    expect(out).toContain('**vec**')
    expect(out).toContain('**ohm**')
  })
  test('the tap reports its own disconnection distinctly from a bot going quiet', () => {
    expect(render({ type: 'bus-lost' }, T0)).toContain('tap')
    expect(render({ type: 'bus-lost' }, T0)).not.toContain('🔇')
  })
})
