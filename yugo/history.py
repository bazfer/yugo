"""
Per-thread rolling conversation history for the native harness.

v0.2a scope (SPEC.md §15 v0.2a): in-memory per-thread store, keyed by Discord
thread id. No persistence, no summarization/compaction — that's v0.2b.

A "turn" = one user message + one assistant reply = 2 entries in the LiteLLM
`messages` array. The bound is expressed in TURNS; the store holds at most
`max_turns * 2` entries. Overflow trims the oldest entries from the front
in-place; the retained suffix is element-wise identical to the tail of the
pre-append store (see `record_turn`).

Design shape:
- Store is a `list`, not a `deque(maxlen=...)`. `maxlen` bounds ENTRIES;
  future compaction will care about turn boundaries (and, later, tokens).
  A list also lets v0.2b splice a leading summary at the head under the
  same lock the turn append uses, without a background-eviction container
  fighting the splice.
- `ALLOWED_ROLES` is parametrized. `"system"` is DELIBERATELY not allowed
  inside the store — the single system message is PERSONA, assembled
  per-turn by `build_messages` at index 0. v0.2b's summary will land as a
  `role: user` entry (see PR review — provider transforms hoist any
  `role: system` to the top-level system param regardless of position; a
  summary must NOT be hoisted, so it goes in a user slot).
  v0.4a did NOT add `"tool"`, contrary to what this comment predicted: the
  tool loop accumulates its rounds in `bot.ask_llm`'s per-turn working copy
  and records only the user text and the final reply. Persisting them would
  need `record_turn` to evict in tool-call GROUPS — the FIFO below trims
  index 0 in pairs, which can strand a `role: tool` entry whose
  `assistant`+`tool_calls` partner was evicted, and an orphan tool result is
  a provider 400 on the next turn.

Eviction is plain FIFO from index 0. v0.2a has no privileged entries to
preserve — the store contains only user/assistant turns and PERSONA lives
outside it. v0.2b's summary-as-role:user compaction slice will define its
own summary-preserving eviction under the invariant declared in SPEC §15
v0.2b (once written); it needs a summary marker that does not yet exist,
so shipping preservation scaffolding now would be a landmine (functions
that promise summary survival while summaries are indistinguishable from
ordinary user turns).

Thread key convention: the Discord caller passes `message.channel.id`. In
discord.py, `Thread` is a channel subclass with its own `.id`, so this yields
the thread id for messages inside a thread, and the parent channel id for
parent-channel messages. Today's `on_message` filter in bot.py drops thread
messages entirely, so every recorded Discord turn currently keys on
CHANNEL_ID; per-thread behavior activates once thread filtering lands
(SPEC §5 `IGNORE_THREADS`) — the filter in bot.py is what needs to change,
not this module.

v0.3b adds a SECOND kind of key. A bus-triggered turn (SPEC §15 3b) is not a
Discord turn: it has no channel, it replies bus-only (§8), and its peer is a
fleet bot rather than a human. It gets its own namespace — one store per peer
bot — built by `bus_thread_key`.

**Key-space convention (binding):** an `int` key is a Discord snowflake and
NOTHING else. Every non-Discord namespace uses a PREFIXED `str` key. No `str`
is ever `==` to an `int` in Python, so this closes the whole collision class
permanently — but only while nobody coerces a non-Discord identity to `int`
(a hash, an enumeration index, an `int(...)` of anything). A namespace built
by arithmetic on the Discord key space would be collision-free only by luck.

Both directions of that isolation are load-bearing, and the second is the
easy one to miss:

* untrusted bot-authored content must not enter a human's Discord thread
  context; and
* a human's private Discord conversation must not become context for a
  bus-triggered reply. The tap and (from v0.6) the coordinator mirror bus
  traffic fleet-wide, so a bus reply built over Discord history leaks that
  conversation outward to every watcher on the bus.
"""

from typing import Any

# Roles legal INSIDE a thread store (post-persona). System is reserved for
# the PERSONA slot that `build_messages` assembles per-turn. v0.4a considered
# and rejected adding "tool" here — see the module docstring for why tool
# rounds stay inside the turn instead of entering the store.
ALLOWED_ROLES: frozenset[str] = frozenset({"user", "assistant"})

# `int` = Discord channel/thread id. `str` = a prefixed non-Discord namespace
# key (v0.3b's bus namespace is the first). See the module docstring's
# key-space convention — the two families cannot collide by construction.
ThreadKey = int | str

BUS_THREAD_PREFIX = "bus:"

# Module-level per-thread store.
_history: dict[ThreadKey, list[dict[str, Any]]] = {}


def bus_thread_key(from_claim: str) -> str:
    """Namespace key for the conversation with one fleet peer.

    Keyed on the peer's canonical `from` claim — the value
    `fleet_bus.validate_envelope` normalised and checked against the fleet
    manifest — so the key space is bounded by the manifest (a fixed handful
    of `^[a-z0-9_-]+$` names) rather than by anything a sender chooses. Two
    alternatives were considered and rejected:

    * per-envelope (no history at all) — loses the "parity with Discord-mode
      context" that SPEC §15 names as v0.3's reason for depending on v0.2a.
    * per-`root_id` — unbounded key space driven entirely by the sender. Still
      rejected at v0.3d, which reads `root_id` but stores nothing keyed on it:
      the baton spec is explicit that lifecycle state belongs to the
      originator and that the protocol is "not a work queue, no persistence".

    **What per-peer isolation does NOT buy, stated so nobody reads more into
    it than it carries:** publish permissions on the live bus are wildcard
    (`fleet.*.request`), and `from` is an allowlist-checked CLAIM, not a
    cryptographic binding (SPEC §8, `authenticated="false"`). Any credentialed
    fleet bot can therefore claim any allowlisted name and write into that
    peer's store at will. This partitions ACCIDENTS — two peers' unrelated
    conversations interleaving — not a hostile peer. Accepted for now; it
    stops being true when subject-encoded sender identity lands (fleet-bus
    task 18), at which point the claim can be checked against the subject.
    """
    return f"{BUS_THREAD_PREFIX}{from_claim}"


def is_compactable(thread_id: ThreadKey) -> bool:
    """May v0.2b's LLM-summary compaction run over this thread's store?

    False for every bus namespace, and the exclusion is not a preference.
    v0.2b compaction summarizes the oldest N entries and splices the summary
    back in as a `role: user` message (`role: system` gets hoisted by provider
    transforms — see the module docstring). Over a bus store that means an
    LLM summarizing UNTRUSTED bot-authored content and re-injecting the
    result: injected instructions get to shape their own summary, and the
    summary then OUTLIVES the FIFO eviction that would otherwise have flushed
    the raw text out of the window. A prompt-injection payload that survives
    its own eviction is a different and worse thing than one that scrolls off.

    Declared here rather than inside the (not yet written) compaction path so
    the rule exists before the code it constrains does;
    `test_history.test_compaction_arrival_must_consult_is_compactable` is the
    tripwire that fires when v0.2b lands.
    """
    return not (isinstance(thread_id, str) and thread_id.startswith(BUS_THREAD_PREFIX))


def build_messages(
    persona: str,
    thread_id: ThreadKey,
    user_text: str,
) -> list[dict[str, Any]]:
    """Build the LiteLLM `messages` payload for a new turn.

    Shape: `[system persona, ...prior turns in insertion order, current user]`.
    A brand-new thread with no history yields `[system, user]`.

    Read-only: does NOT create or mutate the thread's store. The store is
    created lazily on the first successful `record_turn`.
    """
    prior = _history.get(thread_id, ())
    return [
        {"role": "system", "content": persona},
        *[dict(m) for m in prior],   # shallow-copy each dict — protects _history from transform-side dict mutation
        {"role": "user", "content": user_text},
    ]


def record_turn(
    thread_id: ThreadKey,
    max_turns: int,
    user_text: str,
    assistant_text: str,
) -> None:
    """Append a completed user+assistant turn to the thread's store.

    Evicts oldest entries from the front until the store has room for the
    two new entries, keeping the post-append total at `max_turns * 2`. The
    role of each appended entry is validated against `ALLOWED_ROLES` — a
    guard against future callers (v0.4 tool loop) accidentally recording a
    disallowed role.

    Callers MUST only invoke this after a successful LLM completion — never
    on timeout/API-error/empty-response paths, or the failed turn poisons
    subsequent history.
    """
    turn = (
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": assistant_text},
    )
    for entry in turn:
        role = entry["role"]
        if role not in ALLOWED_ROLES:
            raise ValueError(
                f"role {role!r} not allowed in history store; "
                f"expected one of {sorted(ALLOWED_ROLES)}"
            )
    store = _history.setdefault(thread_id, [])
    # Plain FIFO eviction from index 0. See module docstring — no
    # privileged entries to preserve in v0.2a. `bound - 2` leaves room for
    # the pair we're about to append so the post-append total is `bound`.
    bound = max_turns * 2
    while len(store) > bound - 2:
        del store[0]
    store.extend(turn)


def _reset_for_tests() -> None:
    """Test helper — wipe all thread histories. Not part of the public API."""
    _history.clear()
