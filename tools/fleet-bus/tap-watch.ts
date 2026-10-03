/**
 * Deaf-bus detection: decide when a bot has gone silent, and when it came back.
 *
 * THE PROBLEM. A bot can be present and not doing its job, with no signal. bot.py
 * catches a refused dedup store, logs "bus is DEAF", and retries forever. The heartbeat
 * starts inside connect(), so a bot that never connects emits NOTHING — no heartbeat,
 * no status, no envelope. From outside it is indistinguishable from a healthy idle bot:
 * the container is Up, Discord works, docker ps is clean. The only evidence is a log
 * line nobody reads.
 *
 * The raw signal is already on the wire. Nothing consumed it.
 *
 * WHY THIS FILE IS PURE. Everything here is a decision over a snapshot: no timers, no
 * network, no clock of its own. The caller supplies `now`. That is what makes the
 * transitions testable without waiting 90 seconds for a window to pass, and it is the
 * lesson from this tap's own history — the parts that were hard to reach were the parts
 * that stayed broken.
 */

export interface WatchState {
  /** bot name -> epoch ms of its last status message. Absent means never seen. */
  lastSeen: Map<string, number>
  /** Bots currently reported silent. Prevents repeating the alert every tick. */
  silent: Set<string>
  /** bot name -> epoch ms the last daily reminder was emitted for it. */
  reminded: Map<string, number>
  /** Whether the tap itself believes it is connected to NATS. */
  connected: boolean
  /** Epoch ms the tap started. Used for the startup grace. */
  startedAt: number
  /** Whether a "lost the bus" notice has already been emitted. */
  busLostNotified: boolean
}

export interface WatchConfig {
  /** The bots to watch. Empty means the detector is OFF. */
  expected: string[]
  /** A bot is silent after this long without a status message. */
  silenceMs: number
  /** No silence alert fires until the tap has been up this long. */
  graceMs: number
  /** While a bot stays silent, repeat at most once per this interval. */
  reminderMs: number
}

export type WatchEvent =
  | { type: 'silent'; bot: string; lastSeenMs: number | null }
  | { type: 'recovered'; bot: string; silentForMs: number | null }
  | { type: 'still-silent'; bots: string[] }
  | { type: 'bus-lost' }
  | { type: 'bus-regained' }

export function newState(now: number): WatchState {
  return {
    lastSeen: new Map(),
    silent: new Set(),
    reminded: new Map(),
    connected: true,
    startedAt: now,
    busLostNotified: false,
  }
}

/** Record that a bot published a status message. */
export function observeStatus(state: WatchState, bot: string, now: number): void {
  state.lastSeen.set(bot, now)
}

/**
 * Decide what to report, and mutate the state to match.
 *
 * Call it on a timer. It is idempotent in the sense that a second call with the same
 * inputs reports nothing new.
 */
export function tick(state: WatchState, config: WatchConfig, now: number): WatchEvent[] {
  const events: WatchEvent[] = []

  // The detector is off when nothing is expected. An empty set must never be read as
  // "every bot is silent", which would alert about the entire fleet on a misconfiguration.
  if (config.expected.length === 0) return events

  // WHILE DISCONNECTED, EVERY BOT LOOKS SILENT.
  //
  // Without this gate the first NATS restart reports the whole fleet as deaf, which is
  // the alarm that gets muted and then ignored. Report the tap's own problem instead,
  // once, and say nothing about the bots until the tap can see them again.
  if (!state.connected) {
    if (!state.busLostNotified) {
      state.busLostNotified = true
      events.push({ type: 'bus-lost' })
    }
    return events
  }
  if (state.busLostNotified) {
    state.busLostNotified = false
    events.push({ type: 'bus-regained' })
    // Reset the clock. Bots cannot be judged on a window the tap spent disconnected.
    state.startedAt = now
    return events
  }

  // STARTUP GRACE. A cold tap has seen nobody yet. Without this it reports every bot as
  // silent one tick after starting, which is true of the tap and false of the bots.
  if (now - state.startedAt < config.graceMs) return events

  for (const bot of config.expected) {
    const seen = state.lastSeen.get(bot)
    const isSilent = seen === undefined || now - seen >= config.silenceMs

    if (isSilent && !state.silent.has(bot)) {
      state.silent.add(bot)
      state.reminded.set(bot, now)
      events.push({ type: 'silent', bot, lastSeenMs: seen ?? null })
    } else if (!isSilent && state.silent.has(bot)) {
      state.silent.delete(bot)
      state.reminded.delete(bot)
      events.push({ type: 'recovered', bot, silentForMs: seen !== undefined ? now - seen : null })
    }
  }

  // A DAILY LINE WHILE SOMETHING STAYS SILENT.
  //
  // One alert can be missed entirely; a repeating alarm gets muted. The Falco channel
  // died from per-minute repeats, not from per-day ones. So: one on transition, then at
  // most one reminder per interval naming everything still out.
  const stillSilent = config.expected
    .filter((b) => state.silent.has(b))
    .filter((b) => now - (state.reminded.get(b) ?? now) >= config.reminderMs)
  if (stillSilent.length > 0) {
    for (const b of stillSilent) state.reminded.set(b, now)
    events.push({ type: 'still-silent', bots: stillSilent })
  }

  return events
}

/** Render an event for Discord. Pure, so the wording is testable. */
export function render(event: WatchEvent, now: number): string {
  switch (event.type) {
    case 'silent': {
      const when = event.lastSeenMs === null
        ? 'never seen since the tap started'
        : `last seen ${Math.round((now - event.lastSeenMs) / 1000)}s ago`
      return `🔇 **${event.bot}** is silent on the bus — ${when}. It may be up on Discord and deaf here.`
    }
    case 'recovered':
      return `🔊 **${event.bot}** is back on the bus.`
    case 'still-silent':
      return `🔇 still silent: ${event.bots.map((b) => `**${b}**`).join(', ')}`
    case 'bus-lost':
      return '⚠️ the tap lost its NATS connection — bot silence cannot be judged until it returns.'
    case 'bus-regained':
      return '✅ the tap is back on NATS. Watching resumed.'
  }
}
