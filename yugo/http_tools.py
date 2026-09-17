"""Pinned HTTP GET transport; mistakes here turn a text tool into network reach."""
from __future__ import annotations

import concurrent.futures
import gzip
import ipaddress
import json
import os
import re
import socket
import ssl
import time
import urllib.parse
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

DEFAULT_CREDENTIALS_FILE = "/etc/yugo/http-credentials.yaml"
MAX_URL_BYTES = 8192
MAX_HEADER_BYTES = 64 * 1024
READ_CHUNK = 16 * 1024
MAX_CHUNK_LINE_BYTES = 8192
REDIRECTS = {301, 302, 303, 307, 308}
_HEADER_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
# SPEC 9: this set is exhaustive for hostnames, not a floor. `home` and `lan`
# have no reservation and are here because the code already refused them — a
# security list is widened to match the stricter artifact, never narrowed.
_BLOCKED_NAMES = ("internal", "local", "localhost", "home.arpa", "home", "lan", "test", "invalid", "onion")
# SPEC 9.3.3: a NAT64 gateway translates these to the IPv4 address they embed,
# so the embedded address is the one that must face the v4 rules.
_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL_USE = ipaddress.IPv6Network("64:ff9b:1::/48")
# SPEC 9.3.3's repeated-field row: each of these decides something an attacker
# would like to split two ways, so a second line is refused rather than joined.
_AT_MOST_ONCE = (
    ("content-type", "http_get_unsupported_content"),
    ("content-encoding", "http_get_unsupported_encoding"),
    ("location", "http_get_bad_redirect"),
    ("content-length", "http_get_unsupported_content"),
)

class HttpStartupError(Exception):
    """A credential file ambiguity could silently spend the wrong secret."""

class HttpGetError(Exception):
    """A named refusal that must remain distinguishable to the model."""
    def __init__(self, code: str, detail: str, *, audit_fields: Mapping[str, Any] | None = None):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.audit_fields = dict(audit_fields or {})

class HttpResult(str):
    """Tool text carrying non-secret fields needed by the audit record."""
    audit_fields: dict[str, Any]
    def __new__(cls, value: str, audit_fields: Mapping[str, Any]):
        obj = str.__new__(cls, value); obj.audit_fields = dict(audit_fields); return obj

def _authority(scheme: str, host: str, port: int) -> str:
    """`host` or `host:port` for a URL netloc or a Host header, bracketing an
    IPv6 literal.

    ONE function because the two call sites disagreed and that was the bug: URL
    normalisation bracketed, the Host header did not, so a public IPv6 literal
    on a non-default port sent `Host: 2606:4700:4700::1111:8080` — where the
    address ends and the port begins is genuinely ambiguous, and it disagrees
    with the URL reported to the model. Keep the bracket decision in one place
    so the two cannot drift apart again.
    """
    bare = f"[{host}]" if ":" in host else host
    return bare if port == (443 if scheme == "https" else 80) else f"{bare}:{port}"

@dataclass(frozen=True)
class Origin:
    scheme: str
    host: str
    port: int
    def rendered(self) -> str:
        # ALWAYS explicit about the port — this is an audit value, and
        # "which origin did the credential go to" is exactly the question a
        # dropped default port makes ambiguous. Brackets an IPv6 literal for
        # the same reason _authority does, but does not share its
        # default-port elision.
        bare = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{bare}:{self.port}"

@dataclass(frozen=True)
class Credential:
    id: str
    origin: Origin
    header: str
    value: str

class _SafeUniqueLoader(yaml.SafeLoader):
    """Secret-bearing YAML cannot survive last-key-wins or graph aliases."""

def _mapping(loader, node, deep=False):
    out = {}
    for kn, vn in node.value:
        if kn.tag == "tag:yaml.org,2002:merge": raise HttpStartupError("merge key '<<' is forbidden")
        key = loader.construct_object(kn, deep=True)
        try: dup = key in out
        except TypeError as e: raise HttpStartupError(f"unhashable mapping key {key!r}") from e
        if dup: raise HttpStartupError(f"duplicate mapping key {key!r}")
        out[key] = loader.construct_object(vn, deep=deep)
    return out
_SafeUniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)

def _reject_graph(node, seen=None, path="root"):
    if node is None: return
    seen = set() if seen is None else seen
    if id(node) in seen: raise HttpStartupError(f"alias at {path!r} is forbidden")
    seen.add(id(node))
    if isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            if k.tag == "tag:yaml.org,2002:merge": raise HttpStartupError("merge key '<<' is forbidden")
            _reject_graph(k, seen, path); _reject_graph(v, seen, f"{path}.{getattr(k, 'value', '?')}")
    elif isinstance(node, yaml.SequenceNode):
        for i, v in enumerate(node.value): _reject_graph(v, seen, f"{path}[{i}]")

def _parse_yaml(text: str, path: Path):
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader); _reject_graph(node)
        return yaml.load(text, Loader=_SafeUniqueLoader)  # custom SafeLoader, never default
    except HttpStartupError: raise
    except yaml.YAMLError as e: raise HttpStartupError(f"credentials file {path} malformed YAML: {e}") from e

def _blocked_name(host: str) -> str | None:
    """Name the blocked entry `host` sits under, matching WHOLE trailing labels.

    SPEC 9: `printer.local` is blocked and `mylocal.example.com` is not, so the
    comparison is label-anchored rather than a raw suffix — and a bare `local`
    is the whole host, which no suffix comparison reaches.

    The empty root label is dropped before anchoring, because `svc.internal.`
    names the same destination as `svc.internal` and DNS answers both. SPEC
    9.3.3's post-IDNA rule is what makes this load-bearing: `svc.internal。`
    becomes `svc.internal.`, which a label anchor on the dotted form misses.
    """
    lower = host.lower().removesuffix(".")
    for name in _BLOCKED_NAMES:
        if lower == name or lower.endswith("." + name): return name
    return None

def parse_authority(value: str) -> Origin:
    if not isinstance(value, str) or not value: raise HttpStartupError(f"host key {value!r} must be a non-empty string")
    try: value.encode("ascii")
    except UnicodeEncodeError as e: raise HttpStartupError(f"host key {value!r} must be ASCII A-label form") from e
    if value.endswith("."): raise HttpStartupError(f"host key {value!r} has forbidden trailing dot")
    if value.startswith("[") or value.count(":") > 1: raise HttpStartupError(f"host key {value!r} cannot be an IP literal or contain multiple colons")
    host, sep, port_text = value.partition(":")
    if sep:
        if not port_text or not port_text.isascii() or not port_text.isdecimal(): raise HttpStartupError(f"host key {value!r} has invalid port")
        if len(port_text) > 1 and port_text.startswith("0"): raise HttpStartupError(f"host key {value!r} has leading-zero port")
        port = int(port_text)
        if not 1 <= port <= 65535: raise HttpStartupError(f"host key {value!r} port is outside 1..65535")
    else: port = 443
    if len(host.encode()) > 253 or not host: raise HttpStartupError(f"host key {value!r} has invalid hostname length")
    labels = host.split(".")
    if len(labels) < 2 or any(not _HOST_LABEL.fullmatch(x) for x in labels): raise HttpStartupError(f"host key {value!r} violates A-label grammar")
    try: ipaddress.ip_address(host)
    except ValueError: pass
    else: raise HttpStartupError(f"host key {value!r} cannot be an IP literal")
    lower = host.lower()
    if _blocked_name(lower): raise HttpStartupError(f"host key {value!r} has blocked name")
    return Origin("https", lower, port)

def _field_value_fault(text: str) -> str | None:
    """`id` and `value` share one predicate so the two cannot drift apart.

    The repertoire is visible ASCII 0x21-0x7E plus SP and HTAB, which excludes
    CR, LF, NUL and every lone surrogate by construction; edge whitespace is
    rejected on top of it (SPEC 9.4.1).
    """
    if any(c != "\t" and not 0x20 <= ord(c) <= 0x7E for c in text):
        return "a byte outside the ASCII HTTP field-value repertoire"
    if text != text.strip(): return "leading or trailing whitespace"
    return None

def load_credentials(path_value: str | os.PathLike | None = None) -> dict[Origin, Credential]:
    path = Path(path_value or os.getenv("YUGO_HTTP_CREDENTIALS_FILE", "").strip() or DEFAULT_CREDENTIALS_FILE).expanduser().resolve(strict=False)
    if not path.exists(): return {}
    mode = path.stat().st_mode
    if mode & 0o077: raise HttpStartupError(f"credentials file {path} has group/world permission bits (mode {mode & 0o777:04o})")
    try: text = path.read_text()
    except OSError as e: raise HttpStartupError(f"cannot read credentials file {path}: {e}") from e
    doc = _parse_yaml(text, path)
    if not isinstance(doc, dict): raise HttpStartupError("credentials root key must be a mapping")
    unknown = set(doc) - {"version", "credentials"}
    if unknown: raise HttpStartupError(f"unknown credentials top-level key {sorted(unknown)[0]!r}")
    if type(doc.get("version")) is not int or doc["version"] != 1: raise HttpStartupError(f"credentials version key must be integer 1; got {doc.get('version')!r}")
    entries = doc.get("credentials", [])
    if not isinstance(entries, list): raise HttpStartupError("credentials key must be a list")
    result = {}; ids = set()
    for i, item in enumerate(entries):
        if not isinstance(item, dict): raise HttpStartupError(f"credential entry {i} must be a mapping")
        expected = {"id", "host", "header", "value"}
        if set(item) != expected: raise HttpStartupError(f"credential entry {i} keys must be exactly {sorted(expected)}; got {sorted(item)}")
        for key in expected:
            if type(item[key]) is not str or not item[key]: raise HttpStartupError(f"credential entry {i} key {key!r} must be a non-empty string")
        cid, header, secret = item["id"], item["header"], item["value"]
        # `id` never reaches the wire, but it IS returned as `credential_id`,
        # so an id outside the field-value repertoire trips the result encoder
        # at call time instead of here, where an operator can fix it.
        fault = _field_value_fault(cid)
        if fault: raise HttpStartupError(f"credential id key {cid!r} has {fault}")
        if cid == secret or secret in cid: raise HttpStartupError(f"credential id key {cid!r} contains its value")
        if cid in ids: raise HttpStartupError(f"duplicate credential id key {cid!r}")
        if not _HEADER_TOKEN.fullmatch(header): raise HttpStartupError(f"credential header key {header!r} is not an HTTP token")
        fault = _field_value_fault(secret)
        if fault: raise HttpStartupError(f"credential value key for id {cid!r} has {fault}")
        origin = parse_authority(item["host"])
        if origin in result: raise HttpStartupError(f"duplicate normalized origin key {origin.rendered()!r}")
        ids.add(cid); result[origin] = Credential(cid, origin, header, secret)
    return result

def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0: raise HttpGetError("http_get_timeout", "single request deadline expired")
    return left

def _out_of_repertoire(encoded: bytes) -> bool:
    """SPEC 9.3.3's repertoire predicate: a byte `<= 0x20` or `== 0x7F`.

    Shared so the resolved URL and the RAW `Location` header value are judged
    by one rule. The raw half is load-bearing: `urljoin`/`urlsplit` strip
    `\\t`, `\\r` and `\\n`, so a check applied only after resolution sees a
    clean string and cannot refuse them.
    """
    return any(b <= 0x20 or b == 0x7F for b in encoded)

def _normalize_url(raw: str, *, redirect: bool = False) -> tuple[str, Origin, urllib.parse.SplitResult]:
    """Hop zero parses a model-supplied URL; a later hop parses a `Location`.

    Same grammar, different accusation: there is no `Location` on hop zero, so
    a malformed URL there is `http_get_invalid_url` (SPEC 10).
    """
    bad = "http_get_bad_redirect" if redirect else "http_get_invalid_url"
    if type(raw) is not str: raise HttpGetError(bad, "url must be a string")
    try: encoded = raw.encode("utf-8")
    except UnicodeEncodeError as e: raise HttpGetError(bad, "url contains a lone surrogate") from e
    size = len(encoded)
    if size > MAX_URL_BYTES: raise HttpGetError("http_get_url_too_long", f"URL is {size} bytes; ceiling is {MAX_URL_BYTES}")
    # SPEC 9.3.3's repertoire row: refused, never percent-encoded. `urlsplit`
    # strips \t\r\n but NOT a raw space, so `Location: /a b` otherwise reaches
    # the wire as `GET /a b HTTP/1.1` — a request line a lenient origin may
    # re-parse as a different target than the one reported to the model.
    # Encoding it would invent a destination the server did not name. The check
    # lives here so hop zero and every redirect hop are bound identically.
    if _out_of_repertoire(encoded): raise HttpGetError(bad, "url contains a byte outside the URL repertoire")
    try: parts = urllib.parse.urlsplit(raw)
    except ValueError as e: raise HttpGetError(bad, f"malformed URL: {e}") from e
    scheme = parts.scheme.lower()
    # SPEC 10: a scheme other than http/https is `http_get_invalid_url` on hop
    # zero, where the model supplied the URL and no `Location` exists to blame,
    # and `http_get_bad_redirect_scheme` on a redirect hop. Reporting a
    # *redirect* error for a model-supplied `file:///etc/passwd` was the
    # borrowed-specific-code shape SPEC 10 has corrected three times.
    if scheme not in {"http", "https"}: raise HttpGetError("http_get_bad_redirect_scheme" if redirect else bad, f"scheme {scheme!r} is not http/https")
    if parts.username is not None or parts.password is not None: raise HttpGetError(bad, "userinfo is forbidden")
    if not parts.hostname: raise HttpGetError(bad, "URL has no hostname")
    try: host = parts.hostname.encode("idna").decode("ascii").lower(); port = parts.port or (443 if scheme == "https" else 80)
    except (UnicodeError, ValueError) as e: raise HttpGetError(bad, f"invalid authority: {e}") from e
    # SPEC 9.3.3: EVERY host predicate reads the post-IDNA `host`, never
    # `parts.hostname`. Python's IDNA codec splits labels on U+3002, U+FF0E and
    # U+FF61 as well as `.`, and folds a trailing one into an ASCII trailing
    # dot — so a pre-IDNA trailing-dot check paired with a post-IDNA name check
    # let `http://svc.internal。/x` through both: the pre-IDNA string does
    # not end in `.`, and `svc.internal.` does not end in `.internal`. One
    # character defeated the blocked-name set and the trailing-dot rule
    # together, and the name reaching DNS and the `Host` header was a valid
    # FQDN a real resolver answers.
    blocked = _blocked_name(host)
    if blocked:
        raise HttpGetError("http_get_blocked_address", f"hostname {host!r} is under the blocked name {blocked!r}")
    # SPEC 10: a trailing dot violates the authority grammar rather than naming
    # a forbidden destination, so it takes the hop's own code and is NEVER
    # `blocked_address`. It is judged AFTER the name set because SPEC 11 names
    # `http://svc.internal。/x` as a blocked-name refusal: post-IDNA it IS
    # `svc.internal.`, and the destination it names outranks the grammar fault.
    if host.endswith("."): raise HttpGetError(bad, "trailing-dot hostname is refused")
    origin = Origin(scheme, host, port)
    netloc = _authority(scheme, host, port)
    path = parts.path or "/"
    url = urllib.parse.urlunsplit((scheme, netloc, path, parts.query, ""))
    if len(url.encode()) > MAX_URL_BYTES: raise HttpGetError("http_get_url_too_long", "normalized URL exceeds 8192 bytes")
    return url, origin, urllib.parse.urlsplit(url)

def _not_global_unicast(ip) -> str | None:
    """Name why an address fails SPEC 9.3.3's contract, or `None` if it passes.

    `is_global` alone is NOT the contract and must not be the whole predicate.
    CPython reports `True` for every multicast address and for every NAT64
    address, so `if not ip.is_global` approves `239.255.255.250`, `ff02::1` and
    `64:ff9b::a00:1` — the last of which IS `10.0.0.1` on any host behind a
    NAT64 gateway, which is the private address this section exists to refuse.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        # The RFC 8215 local-use prefix is refused whole rather than unpacked:
        # its RFC 6052 embedding length is chosen per deployment, so no single
        # extraction can be trusted, and a local-use prefix is never globally
        # routable anyway.
        if ip in _NAT64_LOCAL_USE: return "local-use NAT64"
        mapped = ip.ipv4_mapped
        if mapped is not None: ip = mapped
        # The well-known NAT64 prefix embeds the v4 address in its low 32 bits;
        # the deprecated IPv4-compatible form `::w.x.y.z` does the same and is
        # not exposed by `ipv4_mapped`. Both are judged as the v4 they reach.
        elif ip in _NAT64_WELL_KNOWN or (int(ip) > 1 and int(ip) >> 32 == 0):
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.is_multicast: return "multicast"
    if not ip.is_global: return "non-global"
    return None

def _approved_addresses(host: str, port: int, deadline: float, resolver=socket.getaddrinfo):
    """Resolve under the deadline; a wedged libc resolver thread may linger.

    The future is abandoned at the deadline so the turn and event loop remain
    live, but Python cannot cancel a getaddrinfo already executing in libc.
    Repeated permanently wedged resolutions can therefore consume executor
    threads until the process is restarted. This is the same residual as the
    filesystem `to_thread` lane in SPEC §9.3.2, not an extension of the tool's
    visible deadline.
    """
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = pool.submit(resolver, host, port, 0, socket.SOCK_STREAM)
        try: infos = fut.result(timeout=_remaining(deadline))
        except concurrent.futures.TimeoutError as e: raise HttpGetError("http_get_timeout", "DNS resolution exceeded deadline") from e
        # A typo'd hostname is a mistake, not an SSRF attempt: the §9 check
        # never ran, so there is no address to call blocked (SPEC §10).
        except socket.gaierror as e: raise HttpGetError("http_get_transport_error", f"DNS resolution failed for {host}: {e}") from e
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    addresses=[]
    for family, socktype, proto, _, sockaddr in infos:
        try: raw_ip = ipaddress.ip_address(sockaddr[0])
        # Not an address, so the §9 check had nothing to evaluate and never
        # ran: a broken resolver, not a blocked destination (SPEC §10).
        except ValueError as e: raise HttpGetError("http_get_transport_error", f"resolver returned invalid address {sockaddr[0]!r}") from e
        blocked=_not_global_unicast(raw_ip)
        if blocked: raise HttpGetError("http_get_blocked_address", f"{host} resolved to {blocked} address {raw_ip}")
        item=(family,socktype,proto,sockaddr)
        if item not in addresses: addresses.append(item)
    # Reached only when the resolver answered with nothing at all; an answer
    # the §9 check refused raises above, and dedup cannot empty a non-empty
    # list. Never an address to block, so it is a transport failure.
    if not addresses: raise HttpGetError("http_get_transport_error", f"resolver returned no addresses for {host}")
    return addresses

def _connect(origin: Origin, deadline: float, addresses, ssl_context):
    last=None
    for family, socktype, proto, sockaddr in addresses:
        raw=socket.socket(family,socktype,proto)
        try:
            raw.settimeout(_remaining(deadline)); raw.connect(sockaddr)
            if origin.scheme == "https":
                raw.settimeout(_remaining(deadline)); raw=ssl_context.wrap_socket(raw,server_hostname=origin.host)
            raw.settimeout(_remaining(deadline)); return raw
        except (OSError, ssl.SSLError) as e: last=e; raw.close()
    # The address already passed the SSRF check; `blocked_address` here would
    # report a refused connection to a public host as an SSRF refusal.
    raise HttpGetError("http_get_timeout" if isinstance(last, TimeoutError) else "http_get_transport_error", f"connect failed: {last}")

def _read_headers(sock, deadline, cumulative):
    """Read the header block in bulk and hand back whatever overshot it.

    SPEC 9.3.3: the observable property is not how many body bytes arrive, it
    is that they reach no one and cost nothing. The recv that finishes the
    header block lifts whatever body bytes shared the segment with it, and on
    a 3xx those are discarded uncharged with no further read issued. Only
    header bytes are charged against the 64 KiB cap, which is why the cap is
    re-checked against the header block rather than against everything read.
    """
    data=bytearray()
    while b"\r\n\r\n" not in data:
        if cumulative + len(data) >= MAX_HEADER_BYTES:
            raise HttpGetError("http_get_size_cap", f"cumulative response headers exceed {MAX_HEADER_BYTES} bytes")
        sock.settimeout(_remaining(deadline))
        try: chunk=sock.recv(min(4096, MAX_HEADER_BYTES + 1 - len(data)))
        except TimeoutError as e: raise HttpGetError("http_get_timeout", "header read exceeded deadline") from e
        if not chunk: raise HttpGetError("http_get_unsupported_content", "connection closed before headers")
        data += chunk
    head, rest=bytes(data).split(b"\r\n\r\n",1)
    if cumulative + len(head) + 4 > MAX_HEADER_BYTES:
        raise HttpGetError("http_get_size_cap", f"cumulative response headers exceed {MAX_HEADER_BYTES} bytes")
    lines=head.split(b"\r\n")
    try: status=int(lines[0].split(b" ",2)[1])
    except Exception as e: raise HttpGetError("http_get_unsupported_content", "malformed HTTP status line") from e
    headers=[]
    for line in lines[1:]:
        if b":" not in line: raise HttpGetError("http_get_unsupported_content", "malformed response header")
        k,v=line.split(b":",1)
        try: headers.append((k.decode("ascii").lower(),v.strip().decode("latin1")))
        except UnicodeDecodeError as e: raise HttpGetError("http_get_unsupported_content", "non-ASCII header name") from e
    _reject_repeated_fields(headers)
    return status, headers, rest, cumulative+len(head)+4

def _reject_repeated_fields(headers):
    """SPEC 9.3.3: four fields must each appear at most once.

    The check is on the parsed lines, before anything joins or matches them,
    because every downstream reading of a joined value is wrong in its own way:
    `text/plain,image/png` passes a prefix match, `gzip,gzip` is refused only by
    accident of containing a comma, and two `Location`s send a first-wins client
    and a last-wins client to different origins.
    """
    for name, code in _AT_MOST_ONCE:
        if sum(1 for k, _ in headers if k == name) > 1:
            raise HttpGetError(code, f"repeated {name} header field")

def _header(headers, name):
    values=[v for k,v in headers if k==name]
    return ",".join(values) if values else None

def _body_chunks(sock, initial, headers, deadline, wire_state, max_bytes):
    transfer=(_header(headers,"transfer-encoding") or "").lower()
    length=_header(headers,"content-length")
    buf=bytearray()
    def charge(data):
        wire_state[0] += len(data)
        if wire_state[0] > max_bytes:
            raise HttpGetError("http_get_size_cap",f"wire bytes exceed max_bytes={max_bytes}")
        return data
    if initial:
        buf.extend(charge(initial))
    def more(limit=READ_CHUNK):
        sock.settimeout(_remaining(deadline))
        try: return charge(sock.recv(limit))
        except TimeoutError as e: raise HttpGetError("http_get_timeout", "body read exceeded deadline") from e
    if "chunked" in transfer:
        while True:
            while b"\r\n" not in buf:
                if len(buf) >= MAX_CHUNK_LINE_BYTES:
                    raise HttpGetError("http_get_size_cap", f"chunk-size line exceeds {MAX_CHUNK_LINE_BYTES} bytes")
                c=more(min(READ_CHUNK, MAX_CHUNK_LINE_BYTES + 1 - len(buf)))
                if not c: raise HttpGetError("http_get_invalid_encoding", "truncated chunk header")
                buf.extend(c)
            line,_,tail=buf.partition(b"\r\n"); buf=bytearray(tail)
            if len(line) > MAX_CHUNK_LINE_BYTES:
                raise HttpGetError("http_get_size_cap", f"chunk-size line exceeds {MAX_CHUNK_LINE_BYTES} bytes")
            try: n=int(line.split(b";",1)[0],16)
            except ValueError as e: raise HttpGetError("http_get_invalid_encoding", "malformed chunk size") from e
            if n < 0:
                raise HttpGetError("http_get_invalid_encoding", "negative chunk size")
            if n == 0:
                while len(buf) < 2:
                    c=more(2-len(buf))
                    if not c: raise HttpGetError("http_get_invalid_encoding", "truncated final chunk")
                    buf.extend(c)
                if bytes(buf[:2]) != b"\r\n":
                    raise HttpGetError("http_get_invalid_encoding", "invalid final chunk terminator")
                return
            remaining=n
            while remaining:
                if buf:
                    take=min(remaining,len(buf),READ_CHUNK)
                    yield bytes(buf[:take]); del buf[:take]; remaining-=take
                    continue
                c=more(min(READ_CHUNK,remaining))
                if not c: raise HttpGetError("http_get_invalid_encoding", "truncated chunk")
                buf.extend(c)
            while len(buf)<2:
                c=more(2-len(buf))
                if not c: raise HttpGetError("http_get_invalid_encoding", "truncated chunk terminator")
                buf.extend(c)
            if bytes(buf[:2]) != b"\r\n":
                raise HttpGetError("http_get_invalid_encoding", "invalid chunk terminator")
            del buf[:2]
    elif length is not None:
        try: declared=int(length)
        except ValueError as e: raise HttpGetError("http_get_unsupported_content", "invalid Content-Length") from e
        if declared < 0:
            raise HttpGetError("http_get_unsupported_content", "negative Content-Length")
        seen=0
        if buf:
            seen += len(buf); yield bytes(buf)
        # The peer controls Content-Length. Because the request asks for
        # Connection: close, EOF is the independent framing control that
        # exposes a body which lies low about its declared length.
        while True:
            chunk=more()
            if not chunk: break
            seen += len(chunk); yield chunk
        if seen < declared:
            raise HttpGetError("http_get_invalid_encoding", "truncated response body")
    else:
        if buf: yield bytes(buf)
        while True:
            chunk=more()
            if not chunk:return
            yield chunk

def _content_type(headers):
    raw=_header(headers,"content-type")
    if raw is None: raise HttpGetError("http_get_unsupported_content", "missing Content-Type")
    bits=[x.strip() for x in raw.split(";")]; media=bits[0].lower()
    if not (media.startswith("text/") or media=="application/json" or (media.startswith("application/") and media.endswith("+json"))):
        raise HttpGetError("http_get_unsupported_content", f"media type {media!r} is not allowed")
    charsets=[]
    for part in bits[1:]:
        if "=" in part and part.split("=",1)[0].strip().lower()=="charset": charsets.append(part.split("=",1)[1].strip().strip('"').lower())
    if len(charsets)>1 or (charsets and charsets[0] not in {"utf-8","utf8"}): raise HttpGetError("http_get_invalid_encoding", f"unsupported charset parameters {charsets!r}")

def _encoding(headers):
    raw=(_header(headers,"content-encoding") or "identity").strip().lower()
    if "," in raw: raise HttpGetError("http_get_unsupported_encoding", f"stacked Content-Encoding {raw!r} is forbidden")
    if raw not in {"identity","gzip"}: raise HttpGetError("http_get_unsupported_encoding", f"Content-Encoding {raw!r} is unsupported")
    return raw

def _redact(text, credential):
    """Replace the credential value with its id, matching case-INSENSITIVELY.

    An exact-byte `str.replace` was not enough on the error path: `_encoding`
    lowercases the header value before rendering it into
    `http_get_unsupported_encoding`, so a hostile origin echoing the credential
    back as a `Content-Encoding` reached the model case-folded and untouched.
    The fold is a transformation yugo chose, inside the text this function owns,
    so the match is widened rather than that one caller patched — any other
    case-folding rendering is covered by the same change. `re.ASCII` keeps the
    fold ASCII-only, since the credential repertoire is ASCII and Unicode
    case-folding would match characters the value does not contain.

    SPEC 9.4.1 still stands: this is literal redaction, not secrecy. A hostile
    origin that re-encodes the value defeats it.
    """
    if not credential: return text
    return re.sub(re.escape(credential.value), lambda _: credential.id, text, flags=re.IGNORECASE | re.ASCII)

def http_get(url: str, *, max_bytes: int, timeout_ms: int, max_redirects: int, remaining_budget: float, credentials: Mapping[Origin,Credential], resolver=socket.getaddrinfo, ssl_context=None) -> HttpResult:
    deadline=time.monotonic()+min(timeout_ms/1000, remaining_budget)
    ssl_context=ssl_context or ssl.create_default_context()
    current, origin, parts=_normalize_url(url)
    selected=None
    forwarding=False
    first_hop=True
    attached=[]; dropped_at=None; redirects=0; wire=decoded=headers_total=0
    audit=lambda: {
        "credential_id": selected.id if selected else None,
        "credential_origins": attached,
        "credential_drop": (
            {"event": "credential_dropped", "hop": dropped_at}
            if dropped_at is not None else None
        ),
    }
    try:
        while True:
            addresses=_approved_addresses(origin.host,origin.port,deadline,resolver)
            # Credential selection happens once and only after the initial
            # origin's runtime SSRF check. A blocked host must not become a
            # credential-map oracle through audit metadata.
            if first_hop:
                selected=credentials.get(origin) if origin.scheme=="https" else None
                forwarding=selected is not None
                first_hop=False
            sock=_connect(origin,deadline,addresses,ssl_context)
            try:
                target=urllib.parse.urlunsplit(("","",parts.path or "/",parts.query,""))
                host_header=_authority(origin.scheme,origin.host,origin.port)
                req=[f"GET {target} HTTP/1.1",f"Host: {host_header}","Accept: text/*, application/json, application/*+json","Accept-Encoding: gzip","Connection: close"]
                # `attached` is the chain-wide audit view; `hop_attached` is the
                # one this response was produced by. SPEC §9.3.3's result-shape
                # row: they answer different questions and must not share a
                # field, or a credential dropped cross-origin still reports as
                # attached on the final unauthenticated response.
                hop_attached=bool(forwarding and selected)
                if hop_attached:
                    req.append(f"{selected.header}: {selected.value}"); attached.append(origin.rendered())
                sock.sendall(("\r\n".join(req)+"\r\n\r\n").encode("ascii"))
                status, hs, initial, headers_total=_read_headers(sock,deadline,headers_total)
                if status in REDIRECTS:
                    # SPEC 9.3.3: body bytes that shared the header read are
                    # discarded UNCHARGED and no further read is issued. The
                    # count is not the observable property — charging bytes the
                    # harness happened to lift made the model's budget depend on
                    # the read size, and demanding zero of them bought that back
                    # at one syscall per byte.
                    location=_header(hs,"location")
                    if location is None: raise HttpGetError("http_get_bad_redirect","redirect has no Location")
                    # SPEC 9.3.3's repertoire row binds the RAW `Location`
                    # value BEFORE any RFC 3986 resolution. `urljoin` and
                    # `urlsplit` strip \t, \r and \n, so `_normalize_url` on
                    # the resolved target sees a clean string:
                    # `Location: //evil.example\n/x` resolved to
                    # `http://evil.example/x` and was followed to a host the
                    # origin never named as a URL, while hop zero — which
                    # checks the string it was handed — refuses the same bytes.
                    if _out_of_repertoire(location.encode("utf-8")):
                        raise HttpGetError("http_get_bad_redirect","Location contains a byte outside the URL repertoire")
                    if redirects>=max_redirects: raise HttpGetError("http_get_redirect_cap",f"max_redirects={max_redirects} exhausted")
                    nxt=urllib.parse.urljoin(current,location)
                    next_url,next_origin,next_parts=_normalize_url(nxt,redirect=True)
                    if forwarding and selected and next_origin != selected.origin:
                        forwarding=False; dropped_at=next_origin.rendered()
                    # close immediately: never drain redirect body
                    redirects+=1; current,origin,parts=next_url,next_origin,next_parts
                    continue
                encoding=_encoding(hs)
                decoder=zlib.decompressobj(16+zlib.MAX_WBITS) if encoding=="gzip" else None
                output=bytearray()
                wire_state=[wire]
                for chunk in _body_chunks(sock,initial,hs,deadline,wire_state,max_bytes):
                    try: piece=decoder.decompress(chunk,max_bytes-decoded+1) if decoder else chunk
                    except zlib.error as e: raise HttpGetError("http_get_invalid_encoding",f"malformed gzip: {e}") from e
                    decoded+=len(piece)
                    if decoded>max_bytes: raise HttpGetError("http_get_size_cap",f"decoded bytes exceed max_bytes={max_bytes}")
                    output.extend(piece)
                wire=wire_state[0]
                if decoder:
                    try: tail=decoder.flush(max_bytes-decoded+1)
                    except zlib.error as e: raise HttpGetError("http_get_invalid_encoding",f"malformed gzip: {e}") from e
                    decoded+=len(tail); output.extend(tail)
                    if decoded>max_bytes: raise HttpGetError("http_get_size_cap",f"decoded bytes exceed max_bytes={max_bytes}")
                    if not decoder.eof: raise HttpGetError("http_get_invalid_encoding","truncated gzip stream")
                    if decoder.unused_data:
                        raise HttpGetError(
                            "http_get_invalid_encoding",
                            "gzip response contains a second member or trailing bytes",
                        )
                # SPEC 9.3.3's empty-body row orders these two rules: an empty
                # body is a valid empty result regardless of `Content-Type`, and
                # the media-type allowlist applies only where there is a body to
                # interpret. A real 204 carries no media type, so checking it
                # before reading the body refused every 204.
                if output: _content_type(hs)
                try: body=output.decode("utf-8")
                except UnicodeDecodeError as e: raise HttpGetError("http_get_invalid_encoding",f"body is not valid UTF-8: {e}") from e
                result={"url":_redact(current,selected),"status":status,"credential_attached":hop_attached,"credential_id":selected.id if hop_attached and selected else None,"body":_redact(body,selected)}
                text=json.dumps(result,ensure_ascii=False,separators=(",",":"),allow_nan=False)
                text.encode("utf-8")
                return HttpResult(text,audit())
            finally: sock.close()
    except HttpGetError as e:
        e.args=(_redact(str(e),selected),); e.audit_fields=audit(); raise
    except (OSError,ssl.SSLError,UnicodeError,ValueError) as e:
        # A catch-all knows only that it did not know. Borrowing a specific
        # code here reported socket, TLS and result-encoding failures as
        # "bad redirect" — a precise claim about none of them (SPEC §10).
        raise HttpGetError("http_get_timeout" if isinstance(e,TimeoutError) else "http_get_transport_error",_redact(str(e),selected),audit_fields=audit()) from e
