"""
yugo — multi-provider agent harness.

v0.1 scope: Discord bridge + LiteLLM completion + persona.
v0.2a scope: rolling per-thread conversation history (see history.py).
v0.2b (planned): opt-in LLM-summary compaction.
v0.3a scope: fleet-bus connect + subscribe + heartbeat + audit (fleet_bus.py).
v0.3b scope: session injection for received envelopes (`_ask_bus`).
v0.3c scope: outbound `<BUS>` tag parser + envelope publish (fleet_bus.py).
v0.3d scope: baton participation — fields on the frame, `hops` incremented on
             outbound, hop-8 warning to `origin`, hop-16 refusal (fleet_bus.py).
v0.4 scope: audited tool loop plus openat2-confined filesystem tools.

Structural bones adopted from artifice-ia/codex-container/bot.py.
"""

import asyncio
import os
import time
from pathlib import Path

import discord
from discord.ext import commands
import litellm
import openai
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import fleet_bus
import history
import tools
from version import YUGO_VERSION

# --- config ---
# Env parsing goes through `_env_int` so blank values in `.env.example`
# (the documented quick-start's `.env` template AND CI's docker smoke-gate
# env-file per SPEC §11) do NOT hit raw `int()` and crash the container at
# import with a cryptic `invalid literal for int() with base 10: ''`.
# Optional keys silently default; required keys raise with a NAMED message
# so the operator sees WHICH var is bad, not just an anonymous traceback.

def _env_int(
    name: str,
    default: int | None = None,
    *,
    required: bool = False,
    positive: bool = False,
) -> int | None:
    """Parse env var `name` as int; blank/unset → `default` (or raise if
    `required`). Non-int content raises ValueError naming the offender.

    `positive=True` additionally rejects zero and negatives. It lives in the
    helper rather than at each call site so the next bounded knob inherits the
    check instead of re-deriving it — the same class-wide-coverage argument
    that put the parse here in the first place.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        if required:
            raise ValueError(f"{name} is required but is blank or unset")
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as e:
            raise ValueError(f"{name} must be an integer; got {raw!r}") from e
    if positive and value is not None and value <= 0:
        # Fail LOUD at import — a zero/negative bound would otherwise silently
        # blow up mid-message inside record_turn (empty user-visible reply,
        # only a traceback in logs). Cheap to catch here.
        raise ValueError(f"{name} must be a positive integer; got {value}")
    return value


DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID = _env_int("CHANNEL_ID", required=True)
# `.env.example` ships GUILD_ID blank (optional guild-scoping slot); the
# trailing `or None` also collapses an explicit "0" to None, matching the
# pre-v0.2a semantic where a falsy int meant "no guild filter".
GUILD_ID = _env_int("GUILD_ID") or None
MODEL = os.environ["MODEL"]  # LiteLLM model string, e.g. xai/grok-code-fast-1
RESPONSE_TIMEOUT = _env_int("RESPONSE_TIMEOUT", 120)
HISTORY_MAX_TURNS = _env_int("HISTORY_MAX_TURNS", 10, positive=True)
# Bus depth is its OWN knob (v0.3b), not a reuse of the Discord one. The two
# lanes are tuned against different things: Discord depth trades context
# against token cost for one human conversation, bus depth decides how long a
# peer bot's untrusted content keeps shaping this bot's replies, across as
# many stores as there are bots in the manifest. Same default, independently
# movable — a deployment that wants bus turns near-stateless must not have to
# amputate the Discord lane to get there.
BUS_HISTORY_MAX_TURNS = _env_int("BUS_HISTORY_MAX_TURNS", 10, positive=True)
# How many tool ROUNDS one turn may take before `ask_llm` gives up (v0.4a).
# A round is "the model asked for tools, we ran them, we asked again", so the
# ceiling on completions is this + 1. It exists because a model that keeps
# re-requesting the same tool would otherwise spend provider budget until
# RESPONSE_TIMEOUT expires, and — on the bus lane — hold a NATS subscription
# callback for the whole of it.
TOOL_MAX_ROUNDS = _env_int("TOOL_MAX_ROUNDS", 5, positive=True)
# --- sandbox mode (v0.4e) ---
# The flag is a STUB in the sense that no full-mode guard is implemented; it is
# NOT a stub in the sense of being inert. Its whole job is to keep the toggle
# honest, and there is only one way to do that.
#
# An operator sets `full` at exactly the moment the threat model changed —
# external users, unvetted channels, a security review. Accepting the value and
# continuing in reduced mode would answer that moment with a lie, and it is the
# quiet kind: nothing in the logs contradicts the operator's belief that the
# expensive guards are on. A warning is not enough either, because the warning
# scrolls and the belief persists.
#
# So `full` is a startup abort until §9's full-mode guards exist — same fatal
# class as a missing persona. Unknown values abort too: `SANDBOX_MODE=strict`
# is an operator who means `full`, and silently treating it as `reduced` is the
# same lie by a different route.
_SANDBOX_MODES = ("reduced", "full")


def _env_sandbox_mode() -> str:
    """Resolve SANDBOX_MODE. Blank/unset → `reduced` (SPEC §9)."""
    raw = os.environ.get("SANDBOX_MODE", "").strip().lower()
    if not raw:
        return "reduced"
    if raw == "reduced":
        return "reduced"
    if raw == "full":
        raise ValueError(
            "SANDBOX_MODE=full is not implemented (SPEC §9 full mode: cgroup "
            "pids/memory limits, wall-clock timeout, shell broker process, "
            "output-size cap). Refusing to start in reduced mode under a full "
            "label — unset SANDBOX_MODE to run reduced deliberately."
        )
    raise ValueError(
        f"SANDBOX_MODE must be one of {_SANDBOX_MODES}; got {raw!r}"
    )


SANDBOX_MODE = _env_sandbox_mode()

TIMEZONE = os.environ.get("TIMEZONE", "UTC")
PERSONA_FILE = os.environ.get("PERSONA_FILE", "/root/persona.md")

# --- fleet bus (v0.3a) ---
# Config is resolved AT IMPORT so a config fault (unreadable
# FLEET_BUS_TOKEN_FILE, missing/empty fleet manifest, BOT_NAME/FLEET_BUS_USER
# mismatch) is fatal at startup — the same treatment SPEC §10 gives a missing
# persona. A NATS server that is merely unreachable is NOT a config fault and
# never gets here: that degrades to Discord-only and retries forever.
#
# `FLEET_BUS_ENABLED=0` (the `.env.example` default, and therefore CI's
# smoke-gate env) leaves BUS_CONFIG None and no NATS code runs at all —
# fleet_bus imports nats-py lazily, so the library is never even loaded.
BUS_CONFIG = (
    fleet_bus.load_config_from_env(YUGO_VERSION) if fleet_bus.bus_enabled() else None
)


class EmptyResponseError(Exception):
    """Raised by `ask_llm` when the provider returns empty content.

    Handled as its own error path so we can surface "[no response]" to the
    user WITHOUT recording the empty turn (which would poison subsequent
    completions with an assistant message that contains nothing to react to).
    """


# --- persona ---
def load_persona() -> str:
    """Load the persona from PERSONA_FILE, fall back to bundled IDENTITY.md.

    Raises RuntimeError if BOTH are missing or unreadable — SPEC §4.5 ("Persona-
    missing at startup = fatal (bot refuses to run)") and §10 ("Persona file
    missing — fatal at startup") both mandate this. Silent degradation to a
    generic "You are a helpful assistant." string would ship a persona-less
    bot to production, exactly the failure the SPEC forbids.

    Catches `PermissionError` alongside `FileNotFoundError` — a root-owned
    `/root/persona.md` on a bot running as a non-root user must NOT skip the
    IDENTITY.md fallback silently at import.
    """
    errors: list[str] = []
    for candidate in (PERSONA_FILE, str(Path(__file__).parent / "IDENTITY.md")):
        try:
            return Path(candidate).read_text()
        except (FileNotFoundError, PermissionError) as e:
            errors.append(f"{candidate} ({e.__class__.__name__})")
    raise RuntimeError(
        "Persona load failed — neither PERSONA_FILE nor bundled IDENTITY.md "
        f"is readable ({'; '.join(errors)}). SPEC §4.5/§10 require a persona "
        "at startup; refusing to run without one."
    )

PERSONA = load_persona()

# --- tools (v0.4b+) ---
# Resolved once at import so a malformed or unknown capability grant prevents
# the gateway from opening. Persona prose has no authority over this set.
TOOLS = tools.resolve_declared()
TOOL_SCHEMAS = tools.schemas(TOOLS)
TOOL_AUDIT = tools.ToolAuditLog(tools.audit_path_from_env())


class ToolLoopLimitError(Exception):
    """Raised when a turn asks for tool rounds past `TOOL_MAX_ROUNDS`.

    Surfaces through `on_message`'s catch-all as `[error: ...]` — it is a
    harness bound, not one of SPEC §10's provider conditions, and the message
    it carries names the bound. Like every other failure path it leaves
    `llm_succeeded` False, so the dead-ended turn never enters history.
    """


# --- fleet-bus lifecycle ---
# Module-scope so `start_bus` is idempotent across a gateway resume, and so
# `stop_bus` can find the task from `close()` without threading state through
# the Discord client.
_bus: fleet_bus.FleetBus | None = None
_bus_task: asyncio.Task | None = None


def start_bus() -> asyncio.Task | None:
    """Start the fleet-bus supervisor. Idempotent; needs a running loop.

    Called from `setup_hook`, NEVER from `on_ready`. `on_ready` re-fires on
    every gateway resume (that is what put the `_lock` at module scope below);
    starting NATS there would give a second client, duplicate heartbeats and a
    doubled audit trail on the first resume. Module scope is not an option
    either — `create_task` at import has no running loop.

    Returns the supervisor task, or None when the bus is disabled.
    """
    global _bus, _bus_task
    if BUS_CONFIG is None:
        return None
    if _bus_task is not None and not _bus_task.done():
        return _bus_task
    _bus = fleet_bus.FleetBus(
        BUS_CONFIG,
        fleet_bus.AuditLog(BUS_CONFIG.audit_log_path),
        # v0.3b. Without this the adapter is back to 3a's audit-and-drop:
        # connected, heartbeating, and ignoring every envelope addressed to it.
        on_envelope=_ask_bus,
    )
    _bus_task = asyncio.create_task(_bus.run(), name="fleet-bus-supervisor")
    return _bus_task


async def stop_bus() -> None:
    """Cancel the supervisor and let its `finally` drain the connection.

    Safe from any state — connected, mid-reconnect, never started. Cancellation
    is swallowed here (`return_exceptions=True`) because a cancelled supervisor
    is the SUCCESS case for shutdown, not an error to propagate into
    `Client.close()`.
    """
    global _bus, _bus_task
    task, _bus_task = _bus_task, None
    _bus = None
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# --- discord + scheduler ---
intents = discord.Intents.default()
intents.message_content = True


class YugoBot(commands.Bot):
    """Discord client with the fleet-bus supervisor bolted to its lifecycle."""

    async def setup_hook(self) -> None:
        start_bus()

    async def close(self) -> None:
        await stop_bus()
        await super().close()


bot = YugoBot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# Serialize turns so one Discord message doesn't step on another.
# Created at MODULE SCOPE so a discord.py gateway resume (which re-fires
# `on_ready`) cannot swap this out mid-turn. Once v0.2b splices a summary
# at the head under this lock, a mid-flight swap would be silent corruption,
# not just interleaving. Python 3.10+ `asyncio.Lock()` binds lazily on first
# `await`, so this is loop-safe.
_lock = asyncio.Lock()


# --- LLM call ---
async def ask_llm(messages: list[dict], thread_id: history.ThreadKey) -> str:
    """One whole turn: complete, run any tool calls, complete again, repeat.

    Returns the model's final reply text. Raises on timeout, API error, empty
    content or a runaway tool loop — the caller (`on_message`) formats the
    error string for the user, so history is only recorded on a successful,
    non-empty completion.

    **Loop shape (SPEC §15 v0.4a).** With no granted tools this is exactly
    v0.3's single completion: `tools=` is not sent at all, because an empty
    `tools=[]` is a parameter some providers reject outright. With tools
    declared, each round appends the `assistant` message carrying `tool_calls`
    and then one `role: tool` message per call, and re-enters the model with
    both in the payload.

    **The accumulation is per-turn, not persisted.** `history.build_messages`
    hands back a fresh list, this function copies it again, and the tool
    rounds live only in that copy — `on_message` / `_ask_bus` still record the
    user text and the FINAL reply, nothing between. That is not squeamishness
    about context: `history.record_turn` evicts entries from index 0 in pairs,
    so a persisted tool round would eventually have its `assistant`+tool_calls
    message evicted while the matching `role: tool` message survived, and an
    orphan tool result is a provider 400 on the next turn. Giving the store
    group-aware eviction is a real design change and it belongs to whichever
    slice needs cross-turn tool memory, not to the scaffold.

    **`RESPONSE_TIMEOUT` bounds the whole turn, not each completion.** N
    rounds under a per-call timeout would be an N×RESPONSE_TIMEOUT turn, and
    on the bus lane that is a subscription callback held for minutes — the
    slow-consumer road `_ask_bus` already refuses to walk for `_lock`. One
    deadline, computed at entry, spent down by completions AND by tool
    execution — the remaining budget is handed to `run_tool_call` as a
    required argument, so a hanging handler cannot hold the turn open the way
    an unbounded `await` on it did in the first cut of this slice.

    `thread_id` is not used to read or write history here — it is the
    provenance stamped on every tool-call audit line, so an auditor can tell a
    tool call driven by a human's Discord turn from one driven by another
    bot's unauthenticated bus payload (SPEC §8).
    """
    deadline = time.monotonic() + RESPONSE_TIMEOUT
    # Copy defensively even though `build_messages` already returns a fresh
    # list: this function appends, and a future caller that hands it a list it
    # kept a reference to must not have it grow tool rounds behind its back.
    working = [dict(m) for m in messages]
    rounds_used = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Reachable when tool execution, not the provider, ate the budget.
            raise asyncio.TimeoutError(
                f"turn exceeded RESPONSE_TIMEOUT after {rounds_used} tool round(s)"
            )
        response = await asyncio.wait_for(
            litellm.acompletion(
                model=MODEL,
                # A per-round copy, not `working` itself. LiteLLM's provider
                # transforms mutate the list they are handed — the Anthropic
                # and Gemini ones POP the system entry, which is exactly what
                # `test_litellm_transforms` pins them doing. One call never
                # noticed; a loop that handed the same list over twice would
                # lose the persona somewhere after round 1.
                messages=[dict(m) for m in working],
                **({"tools": TOOL_SCHEMAS} if TOOL_SCHEMAS else {}),
            ),
            timeout=remaining,
        )
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []
        if not tool_calls:
            content = (message.content or "").strip()
            if not content:
                raise EmptyResponseError("provider returned empty content")
            return content
        if rounds_used >= TOOL_MAX_ROUNDS:
            reason = f"tool loop ceiling of {TOOL_MAX_ROUNDS} round(s)"
            # Audit the refused batch BEFORE raising. These calls never reach
            # `run_tool_call`, so without this they are the only ones in the
            # turn with no audit line — and they are the batch a runaway model
            # emits last, which from v0.4b/4c means paths and URLs. Every
            # other pre-handler refusal already records; this closed the one
            # lane that did not.
            for call in tool_calls:
                tools.record_refused_call(
                    call,
                    audit=TOOL_AUDIT,
                    thread_id=thread_id,
                    round_index=rounds_used,
                    reason=reason,
                )
            raise ToolLoopLimitError(
                f"tool loop hit its ceiling of {TOOL_MAX_ROUNDS} round(s) "
                "without a final answer"
            )
        working.append(tools.assistant_tool_call_message(message))
        for call in tool_calls:
            working.append(
                await tools.run_tool_call(
                    call,
                    selected=TOOLS,
                    audit=TOOL_AUDIT,
                    thread_id=thread_id,
                    round_index=rounds_used,
                    # Recomputed per CALL, not per round: several tool calls
                    # can arrive in one assistant message and each spends from
                    # the same turn budget, so the second one must see what
                    # the first left. A handler that outruns it is audited and
                    # answered as a tool error, and the deadline check at the
                    # top of the loop then ends the turn.
                    timeout=deadline - time.monotonic(),
                )
            )
        rounds_used += 1


async def _ask_bus(envelope: dict, req_id: str) -> str:
    """One fleet-bus-triggered turn. Returns the reply text. v0.3b.

    `req_id` is the adapter's consumer-local nonce for this injection. It is
    passed through to the frame rather than used here, and it must never be
    confused with the sender-chosen envelope id.

    What happens to the returned text is `fleet_bus`'s business, not this
    function's (v0.3c): the adapter parses `<BUS>` tags out of it, publishes
    each to the peer it names, and sends what is left back to the sender. The
    wire stays on one side of this seam and the LLM on the other. Baton fields
    are on the same side of it (v0.3d) — the whole envelope arrives here, so
    `format_envelope_for_session` shows the model which baton it is holding,
    and the adapter alone decides what the outbound `hops` is.

    BUS-ONLY, per SPEC §8: there is no Discord channel in this function
    and there must never be one. A bus turn that also spoke into `CHANNEL_ID`
    would put another bot's untrusted payload — and this bot's answer to it —
    into a human channel that never asked for either, on a transport whose
    whole point (§8, "transport is the seam") is that bots talk to bots on the
    bus and to humans on Discord.

    Errors PROPAGATE rather than becoming a user-visible `[error: ...]`
    string. The Discord path formats those because a human is waiting and can
    read them; here the caller is `fleet_bus.FleetBus._on_request`, which turns
    the exception into the `injection_failed` audit line its TypeScript peer
    already writes. Same consequence either way: `record_turn` is not reached,
    so a failed turn never enters history.

    Deliberately does NOT take `_lock`. That lock serializes Discord turns
    against each other; the two lanes share no state here (their history
    namespaces cannot collide — see `history.bus_thread_key`), and sharing it
    would mean a stuck 120s Discord completion stalls the `.request`
    subscription callback, which is the road to a NATS slow-consumer. Bus
    turns are already serialized against each other by nats-py, which awaits
    one subscription callback before pulling the next message off that
    subscription's queue.
    """
    thread_id = history.bus_thread_key(envelope["from"])
    prompt = fleet_bus.format_envelope_for_session(envelope, req_id)
    messages = history.build_messages(PERSONA, thread_id, prompt)
    reply = await ask_llm(messages, thread_id)
    history.record_turn(thread_id, BUS_HISTORY_MAX_TURNS, prompt, reply)
    return reply


# --- discord bridge ---
@bot.event
async def on_ready():
    scheduler.start()
    print(f"online as {bot.user} · model={MODEL}")


@bot.event
async def on_message(message: discord.Message):
    # Ignore self and other bots.
    if message.author.bot:
        return
    if message.channel.id != CHANNEL_ID:
        return

    # Thread key: `message.channel.id` is the Thread id when the message is
    # inside a thread (discord.py exposes Thread as a channel subclass), and
    # the parent channel id otherwise. NOTE: the `!= CHANNEL_ID` filter above
    # currently drops all thread messages, so in practice every recorded turn
    # today keys on CHANNEL_ID; per-thread keying activates once thread
    # filtering lands (SPEC §5 `IGNORE_THREADS`) — the filter is what will
    # need to change, not this line.
    thread_id = message.channel.id
    user_text = message.content

    # Typing indicator MUST wrap the `_lock` acquire (SPEC §4.2, §4.5 — SEV-5):
    # queued users see activity while waiting their turn, not silence until the
    # lock frees. The lock still guards message-assembly + send so a concurrent
    # turn on the same thread cannot slip in with a stale view.
    async with message.channel.typing():
        async with _lock:
            messages = history.build_messages(PERSONA, thread_id, user_text)
            llm_succeeded = False
            try:
                reply = await ask_llm(messages, thread_id)
            except asyncio.TimeoutError:
                reply = f"[timeout — no response in {RESPONSE_TIMEOUT}s]"
            except EmptyResponseError:
                # Provider returned empty content — surface it but don't
                # record the turn (history.record_turn contract).
                reply = "[no response]"
            except openai.APIError as e:
                # LiteLLM re-exports openai; every operational LiteLLM
                # exception (RateLimitError, AuthenticationError, Timeout,
                # BadRequestError, ContextWindowExceededError, ...) inherits
                # from openai.APIError but NOT from litellm.exceptions.APIError,
                # so catching the openai base is what actually matches real
                # provider errors.
                reply = f"[api error: {e}]"
            except Exception as e:  # noqa: BLE001 — surface all model errors
                reply = f"[error: {e}]"
            else:
                llm_succeeded = True

            # Send BEFORE recording — if send raises (network hiccup, permission
            # denied, rate limit, disconnect mid-turn, partial multi-chunk
            # failure) the model must NOT see an assistant reply the user never
            # received. Partial delivery counts as "not received": recording the
            # full text here would poison the next turn's context with content
            # the user only saw a fragment of. Kept inside `_lock` so a
            # concurrent turn on the same thread cannot slip in with a stale
            # (record-less) view.
            try:
                for chunk in (reply[i:i + 1990] for i in range(0, len(reply), 1990)):
                    await message.channel.send(chunk)
            except discord.DiscordException as e:
                print(
                    f"[send] failed after LLM completion: {e!r} — history not updated"
                )
                return

            # Send fully succeeded. Record only if the LLM itself succeeded —
            # error strings ([timeout ...] / [api error ...] / [error ...] /
            # [no response]) are still surfaced to the user above but never
            # enter history (v0.2a contract on history.record_turn).
            if llm_succeeded:
                history.record_turn(thread_id, HISTORY_MAX_TURNS, user_text, reply)


# --- TODO markers for future phases ---
# v0.2b: opt-in LLM-summary compaction.
#   When a thread's store hits its bound, call litellm.acompletion with a
#   summarization prompt over the oldest N turns; replace those entries with
#   one `role: user` summary message (NOT `role: system` — provider transforms
#   hoist any system message to the top-level system param regardless of
#   position, which would collapse persona + summary). Splice at head under
#   the same `_lock` `on_message` already holds. Opt-in via config.
#
# v0.4b/v0.4c: the actual tools — `read_file` + `write_file` chroot-scoped to
#   the bot workspace, and `http_get` with SPEC §9's default-deny blocklist,
#   post-resolution IP check and redirect cap. They register in tools.REGISTRY
#   and need no change to the loop above. Note what they must NOT do: the
#   `$YUGO_TOOL_AUDIT_PATH` file has to stay outside `write_file`'s scope
#   (SPEC §9) or the agent can edit its own audit trail.
#
# v0.4e shipped the SANDBOX_MODE flag: reduced is the only implemented mode,
#   and `full` is a startup abort rather than a silent downgrade. Implementing
#   full mode means §9's deferred guards — cgroup pids/memory limits, a
#   wall-clock timeout, a broker process for shell, an output-size cap — after
#   which _env_sandbox_mode stops raising and starts selecting.


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
