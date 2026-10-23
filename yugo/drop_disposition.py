"""Exhaustive diagnostic classification for core NATS drops (not acknowledgements)."""
from enum import Enum

class Disposition(str, Enum):
    TERMINAL = "terminal"
    TRANSIENT = "transient"
    THROTTLED = "throttled"
    DUPLICATE = "duplicate"


class DropReason(str, Enum):
    MALFORMED_JSON = "malformed_json"
    CLAUDE_DISCORD_ADAPTER_MALFORMED_JSON = "claude_discord_adapter_malformed_json"
    RECIPIENT_MISMATCH = "recipient_mismatch"
    INVALID_TO = "invalid_to"
    CLAUDE_DISCORD_ADAPTER_MISROUTED_REPLY = "claude_discord_adapter_misrouted_reply"
    CLAUDE_DISCORD_ADAPTER_REPLY_FROM_MISMATCH = "claude_discord_adapter_reply_from_mismatch"
    CLAUDE_DISCORD_ADAPTER_REPLY_TOKEN_MISMATCH = "claude_discord_adapter_reply_token_mismatch"
    ENVELOPE_NOT_OBJECT = "envelope_not_object"
    ENVELOPE_TOO_LARGE = "envelope_too_large"
    INVALID_ID = "invalid_id"
    INVALID_IN_REPLY_TO = "invalid_in_reply_to"
    INVALID_KIND = "invalid_kind"
    INVALID_TS = "invalid_ts"
    MISSING_PAYLOAD = "missing_payload"
    PAYLOAD_NOT_SERIALIZABLE = "payload_not_serializable"
    CLAUDE_DISCORD_ADAPTER_PAYLOAD_NOT_JSON_SERIALIZABLE = "claude_discord_adapter_payload_not_json_serializable"
    UNSUPPORTED_ENVELOPE_VERSION = "unsupported_envelope_version"
    CLAUDE_DISCORD_ADAPTER_INVALID_HOPS = "claude_discord_adapter_invalid_hops"
    CLAUDE_DISCORD_ADAPTER_INVALID_HOPS_CEILING = "claude_discord_adapter_invalid_hops_ceiling"
    CLAUDE_DISCORD_ADAPTER_INVALID_RECIPIENT = "claude_discord_adapter_invalid_recipient"
    CLAUDE_DISCORD_ADAPTER_INVALID_ROOT_ID = "claude_discord_adapter_invalid_root_id"
    FROM_CLAIM_REJECTED = "from_claim_rejected"
    CLAUDE_DISCORD_ADAPTER_PAYLOAD_BODY_TRUNCATED = "claude_discord_adapter_payload_body_truncated"
    CLAUDE_DISCORD_ADAPTER_MULTI_INSTANCE_PUBLISH_ONLY = "claude_discord_adapter_multi_instance_publish_only"
    CLAUDE_DISCORD_ADAPTER_REQ_ID_UNKNOWN = "claude_discord_adapter_req_id_unknown"
    CLAUDE_DISCORD_ADAPTER_INVALID_ORIGIN = "claude_discord_adapter_invalid_origin"
    CLAUDE_DISCORD_ADAPTER_INVALID_OWNER = "claude_discord_adapter_invalid_owner"
    CLAUDE_DISCORD_ADAPTER_BATON_BOT_NAME_INVALID = "claude_discord_adapter_baton_bot_name_invalid"
    CLAUDE_DISCORD_ADAPTER_BATON_HANDOFF_RECIPIENT_INVALID = "claude_discord_adapter_baton_handoff_recipient_invalid"
    CLAUDE_DISCORD_ADAPTER_BATON_HOPS_EXHAUSTED = "claude_discord_adapter_baton_hops_exhausted"
    YUGO_INVALID_ROOT_ID = "yugo_invalid_root_id"
    YUGO_INVALID_ORIGIN = "yugo_invalid_origin"
    YUGO_INVALID_OWNER = "yugo_invalid_owner"
    YUGO_INVALID_HOPS = "yugo_invalid_hops"
    YUGO_BUS_TAG_REJECTED = "yugo_bus_tag_rejected"
    CLAUDE_DISCORD_ADAPTER_INJECTION_FAILED = "claude_discord_adapter_injection_failed"
    INJECTION_FAILED = "injection_failed"
    CLAUDE_DISCORD_ADAPTER_PUBLISH_FAILED = "claude_discord_adapter_publish_failed"
    CLAUDE_DISCORD_ADAPTER_REQUEST_TIMEOUT = "claude_discord_adapter_request_timeout"
    CLAUDE_DISCORD_ADAPTER_FLEET_BUS_NOT_CONNECTED = "claude_discord_adapter_fleet_bus_not_connected"
    CLAUDE_DISCORD_ADAPTER_LEDGER_OVERFLOW = "claude_discord_adapter_ledger_overflow"
    CLAUDE_DISCORD_ADAPTER_REPLY_UNDELIVERED = "claude_discord_adapter_reply_undelivered"
    CLAUDE_DISCORD_ADAPTER_BATON_DERIVATION_FAILED = "claude_discord_adapter_baton_derivation_failed"
    CLAUDE_DISCORD_ADAPTER_CLAIM_DEADLINE_RELEASED = "claude_discord_adapter_claim_deadline_released"
    YUGO_PUBLISH_FAILED = "yugo_publish_failed"
    CLAUDE_DISCORD_ADAPTER_DEDUP_STORE_FAILED = "claude_discord_adapter_dedup_store_failed"
    CLAUDE_DISCORD_ADAPTER_DEDUP_COMPLETE_FAILED = "claude_discord_adapter_dedup_complete_failed"
    CLAUDE_DISCORD_ADAPTER_DEDUP_RELEASE_FAILED = "claude_discord_adapter_dedup_release_failed"
    CLAUDE_DISCORD_ADAPTER_DEDUP_RENEW_FAILED = "claude_discord_adapter_dedup_renew_failed"
    CLAUDE_DISCORD_ADAPTER_DEDUP_OWNER_LOST = "claude_discord_adapter_dedup_owner_lost"
    CLAUDE_DISCORD_ADAPTER_DEDUP_LEASE_LOST = "claude_discord_adapter_dedup_lease_lost"
    YUGO_DEDUP_STORE_FAILED = "yugo_dedup_store_failed"
    YUGO_DEDUP_COMPLETE_FAILED = "yugo_dedup_complete_failed"
    YUGO_DEDUP_RELEASE_FAILED = "yugo_dedup_release_failed"
    YUGO_DEDUP_RENEW_FAILED = "yugo_dedup_renew_failed"
    YUGO_DEDUP_OWNER_LOST = "yugo_dedup_owner_lost"
    YUGO_DEDUP_LEASE_LOST = "yugo_dedup_lease_lost"
    CLAUDE_DISCORD_ADAPTER_RATE_LIMITED_PER_FROM = "claude_discord_adapter_rate_limited_per_from"
    CLAUDE_DISCORD_ADAPTER_RATE_LIMITED_PER_SUBJECT = "claude_discord_adapter_rate_limited_per_subject"
    CLAUDE_DISCORD_ADAPTER_RATE_LIMITED_PER_SESSION_INJECT = "claude_discord_adapter_rate_limited_per_session_inject"
    CLAUDE_DISCORD_ADAPTER_DUPLICATE_ENVELOPE = "claude_discord_adapter_duplicate_envelope"
    CLAUDE_DISCORD_ADAPTER_DUPLICATE_REQUEST_ENVELOPE = "claude_discord_adapter_duplicate_request_envelope"
    CLAUDE_DISCORD_ADAPTER_DUPLICATE_RESULT_ENVELOPE = "claude_discord_adapter_duplicate_result_envelope"
    YUGO_DUPLICATE_ENVELOPE = "yugo_duplicate_envelope"

DROP_DISPOSITION: dict[DropReason, Disposition] = {
    DropReason.MALFORMED_JSON: Disposition.TERMINAL,
    DropReason.CLAUDE_DISCORD_ADAPTER_MALFORMED_JSON: Disposition.TERMINAL,
    DropReason.RECIPIENT_MISMATCH: Disposition.TERMINAL,
    DropReason.INVALID_TO: Disposition.TERMINAL,
    DropReason.CLAUDE_DISCORD_ADAPTER_MISROUTED_REPLY: Disposition.TERMINAL,
    DropReason.CLAUDE_DISCORD_ADAPTER_REPLY_FROM_MISMATCH: Disposition.TERMINAL,
    DropReason.CLAUDE_DISCORD_ADAPTER_REPLY_TOKEN_MISMATCH: Disposition.TERMINAL,
    DropReason.ENVELOPE_NOT_OBJECT: Disposition.TERMINAL,
    DropReason.ENVELOPE_TOO_LARGE: Disposition.TERMINAL,
    DropReason.INVALID_ID: Disposition.TERMINAL,
    DropReason.INVALID_IN_REPLY_TO: Disposition.TERMINAL,
    DropReason.INVALID_KIND: Disposition.TERMINAL,
    DropReason.INVALID_TS: Disposition.TERMINAL,
    DropReason.MISSING_PAYLOAD: Disposition.TERMINAL,
    DropReason.PAYLOAD_NOT_SERIALIZABLE: Disposition.TERMINAL,
    DropReason.CLAUDE_DISCORD_ADAPTER_PAYLOAD_NOT_JSON_SERIALIZABLE: Disposition.TERMINAL,
    DropReason.UNSUPPORTED_ENVELOPE_VERSION: Disposition.TERMINAL,
    DropReason.CLAUDE_DISCORD_ADAPTER_INVALID_HOPS: Disposition.TERMINAL,
    DropReason.CLAUDE_DISCORD_ADAPTER_INVALID_HOPS_CEILING: Disposition.TERMINAL,
    DropReason.CLAUDE_DISCORD_ADAPTER_INVALID_RECIPIENT: Disposition.TERMINAL,
    DropReason.CLAUDE_DISCORD_ADAPTER_INVALID_ROOT_ID: Disposition.TERMINAL,
    DropReason.FROM_CLAIM_REJECTED: Disposition.TERMINAL,
    DropReason.CLAUDE_DISCORD_ADAPTER_PAYLOAD_BODY_TRUNCATED: Disposition.TERMINAL,
    DropReason.CLAUDE_DISCORD_ADAPTER_MULTI_INSTANCE_PUBLISH_ONLY: Disposition.TERMINAL,
    DropReason.CLAUDE_DISCORD_ADAPTER_REQ_ID_UNKNOWN: Disposition.TERMINAL,
    DropReason.CLAUDE_DISCORD_ADAPTER_INVALID_ORIGIN: Disposition.TERMINAL,
    DropReason.CLAUDE_DISCORD_ADAPTER_INVALID_OWNER: Disposition.TERMINAL,
    DropReason.CLAUDE_DISCORD_ADAPTER_BATON_BOT_NAME_INVALID: Disposition.TERMINAL,
    DropReason.CLAUDE_DISCORD_ADAPTER_BATON_HANDOFF_RECIPIENT_INVALID: Disposition.TERMINAL,
    DropReason.CLAUDE_DISCORD_ADAPTER_BATON_HOPS_EXHAUSTED: Disposition.TERMINAL,
    DropReason.YUGO_INVALID_ROOT_ID: Disposition.TERMINAL,
    DropReason.YUGO_INVALID_ORIGIN: Disposition.TERMINAL,
    DropReason.YUGO_INVALID_OWNER: Disposition.TERMINAL,
    DropReason.YUGO_INVALID_HOPS: Disposition.TERMINAL,
    DropReason.YUGO_BUS_TAG_REJECTED: Disposition.TERMINAL,
    DropReason.CLAUDE_DISCORD_ADAPTER_INJECTION_FAILED: Disposition.TRANSIENT,
    DropReason.INJECTION_FAILED: Disposition.TRANSIENT,
    DropReason.CLAUDE_DISCORD_ADAPTER_PUBLISH_FAILED: Disposition.TRANSIENT,
    DropReason.CLAUDE_DISCORD_ADAPTER_REQUEST_TIMEOUT: Disposition.TRANSIENT,
    DropReason.CLAUDE_DISCORD_ADAPTER_FLEET_BUS_NOT_CONNECTED: Disposition.TRANSIENT,
    DropReason.CLAUDE_DISCORD_ADAPTER_LEDGER_OVERFLOW: Disposition.TRANSIENT,
    DropReason.CLAUDE_DISCORD_ADAPTER_REPLY_UNDELIVERED: Disposition.TRANSIENT,
    DropReason.CLAUDE_DISCORD_ADAPTER_BATON_DERIVATION_FAILED: Disposition.TRANSIENT,
    DropReason.CLAUDE_DISCORD_ADAPTER_CLAIM_DEADLINE_RELEASED: Disposition.TRANSIENT,
    DropReason.YUGO_PUBLISH_FAILED: Disposition.TRANSIENT,
    DropReason.CLAUDE_DISCORD_ADAPTER_DEDUP_STORE_FAILED: Disposition.TRANSIENT,
    DropReason.CLAUDE_DISCORD_ADAPTER_DEDUP_COMPLETE_FAILED: Disposition.TRANSIENT,
    DropReason.CLAUDE_DISCORD_ADAPTER_DEDUP_RELEASE_FAILED: Disposition.TRANSIENT,
    DropReason.CLAUDE_DISCORD_ADAPTER_DEDUP_RENEW_FAILED: Disposition.TRANSIENT,
    DropReason.CLAUDE_DISCORD_ADAPTER_DEDUP_OWNER_LOST: Disposition.TRANSIENT,
    DropReason.CLAUDE_DISCORD_ADAPTER_DEDUP_LEASE_LOST: Disposition.TRANSIENT,
    DropReason.YUGO_DEDUP_STORE_FAILED: Disposition.TRANSIENT,
    DropReason.YUGO_DEDUP_COMPLETE_FAILED: Disposition.TRANSIENT,
    DropReason.YUGO_DEDUP_RELEASE_FAILED: Disposition.TRANSIENT,
    DropReason.YUGO_DEDUP_RENEW_FAILED: Disposition.TRANSIENT,
    DropReason.YUGO_DEDUP_OWNER_LOST: Disposition.TRANSIENT,
    DropReason.YUGO_DEDUP_LEASE_LOST: Disposition.TRANSIENT,
    DropReason.CLAUDE_DISCORD_ADAPTER_RATE_LIMITED_PER_FROM: Disposition.THROTTLED,
    DropReason.CLAUDE_DISCORD_ADAPTER_RATE_LIMITED_PER_SUBJECT: Disposition.THROTTLED,
    DropReason.CLAUDE_DISCORD_ADAPTER_RATE_LIMITED_PER_SESSION_INJECT: Disposition.THROTTLED,
    DropReason.CLAUDE_DISCORD_ADAPTER_DUPLICATE_ENVELOPE: Disposition.DUPLICATE,
    DropReason.CLAUDE_DISCORD_ADAPTER_DUPLICATE_REQUEST_ENVELOPE: Disposition.DUPLICATE,
    DropReason.CLAUDE_DISCORD_ADAPTER_DUPLICATE_RESULT_ENVELOPE: Disposition.DUPLICATE,
    DropReason.YUGO_DUPLICATE_ENVELOPE: Disposition.DUPLICATE,
}
assert set(DROP_DISPOSITION) == set(DropReason), "unclassified drop reason"
