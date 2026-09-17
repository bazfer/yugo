"""HTTP capability controls: each rejection has an adjacent successful shape."""
import base64
import gzip
import ipaddress
import json
import socket
import ssl
import time
import tracemalloc

import pytest

import http_tools as h


class FakeSocket:
    def __init__(self, response): self.response=bytearray(response); self.sent=b""; self.closed=False
    def settimeout(self, value): self.timeout=value
    def sendall(self, value): self.sent += value
    def recv(self, n):
        value=bytes(self.response[:n]); del self.response[:n]; return value
    def close(self): self.closed=True


class GradualSocket(FakeSocket):
    def __init__(self, response, step=7): super().__init__(response); self.step=step
    def recv(self,n): return super().recv(min(n,self.step))


class PostHeaderReadCountingSocket(FakeSocket):
    """Counts reads issued once the header terminator has already been handed over.

    §9.3.3's no-drain rule is asserted in reads, not bytes: whatever overshoots
    the header block in the same recv is allowed, a further recv is not.
    """
    def __init__(self, response):
        super().__init__(response)
        self.headers_end=bytes(response).index(b"\r\n\r\n")+4
        self.consumed=0
        self.reads_after_headers=0
    def recv(self,n):
        if self.consumed>=self.headers_end: self.reads_after_headers+=1
        data=super().recv(n); self.consumed+=len(data); return data


def response(body=b"ok", status=200, headers=()):
    names={k.lower() for k,_ in headers}
    base=[b"HTTP/1.1 "+str(status).encode()+b" OK"]
    if "content-type" not in names: base.append(b"Content-Type: text/plain")
    if "content-length" not in names: base.append(b"Content-Length: "+str(len(body)).encode())
    base.extend(k.encode()+b": "+v.encode() for k,v in headers)
    return b"\r\n".join(base)+b"\r\n\r\n"+body


def run(monkeypatch, responses, url="http://public.example/x", **kw):
    sockets=[FakeSocket(x) for x in responses]
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",80))])
    monkeypatch.setattr(h,"_connect",lambda *a,**k:sockets.pop(0))
    defaults=dict(max_bytes=1000,timeout_ms=30000,max_redirects=5,remaining_budget=30,credentials={})
    defaults.update(kw)
    return h.http_get(url,**defaults)


def _connections_recording(monkeypatch, responses):
    """Wire up `responses` and return the list of hosts `_connect` reaches."""
    sockets=[FakeSocket(x) for x in responses]
    hosts=[]
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",80))])
    def connect(origin,*a,**k): hosts.append(origin.host); return sockets.pop(0)
    monkeypatch.setattr(h,"_connect",connect)
    return hosts


def test_non_2xx_and_spoof_body_are_structurally_framed(monkeypatch):
    out=run(monkeypatch,[response(b'status: 200\n"credential_id":"x"',404)])
    parsed=json.loads(out)
    assert parsed["status"]==404 and parsed["credential_id"] is None
    assert "status: 200" in parsed["body"] and set(parsed)=={"url","status","credential_attached","credential_id","body"}

@pytest.mark.parametrize("ctype",["application/json","application/problem+json","text/plain; charset=UTF-8","text/plain; charset=utf8","text/plain; charset=UtF-8"])
def test_allowed_media_and_charset_controls(monkeypatch,ctype):
    out=run(monkeypatch,[response(headers=[("Content-Type",ctype)])])
    assert json.loads(out)["body"]=="ok"

@pytest.mark.parametrize(("headers","code"),[
    ([("Content-Type","text/plain"),("Content-Type","image/png")],"http_get_unsupported_content"),
    ([("Content-Type","image/png"),("Content-Type","text/plain")],"http_get_unsupported_content"),
    ([("Content-Encoding","gzip"),("Content-Encoding","gzip")],"http_get_unsupported_encoding"),
    ([("Content-Length","2"),("Content-Length","2")],"http_get_unsupported_content"),
],ids=["type-allowed-first","type-allowed-last","encoding","length"])
def test_repeated_header_fields_are_refused_by_name(monkeypatch,headers,code):
    """§9.3.3: these four fields must each appear at most once.

    Both `Content-Type` orders are asserted because the defect they exist for is
    order-dependent: an implementation that comma-joins per RFC 9110 and then
    prefix-matches accepts `text/plain,image/png` and refuses the reverse, so a
    single-order control passes it. The message is asserted too — for the other
    three, joining produces a value that is refused by accident (`gzip,gzip`
    reads as a stacked encoding, `2,2` as an invalid length) with the same code
    the explicit check raises, so the code alone cannot tell them apart.
    """
    with pytest.raises(h.HttpGetError) as caught: run(monkeypatch,[response(b"ok",headers=headers)])
    assert caught.value.code==code and "repeated" in str(caught.value)

def test_repeated_location_is_a_bad_redirect(monkeypatch):
    """The one that decides a destination: first-wins goes to `/safe`, last-wins
    to the attacker, and both are conforming readings of the same response."""
    with pytest.raises(h.HttpGetError) as caught:
        run(monkeypatch,[response(b"",302,[("Location","/safe"),("Location","https://evil.example/x")]),response()])
    assert caught.value.code=="http_get_bad_redirect" and "repeated" in str(caught.value)

@pytest.mark.parametrize(("headers","code"),[
    ([('Content-Type','application/octet-stream')],"http_get_unsupported_content"),
    ([('Content-Type','text/plain; charset=ISO-8859-1')],"http_get_invalid_encoding"),
    ([('Content-Type','text/plain; charset=utf8; charset=utf-8')],"http_get_invalid_encoding"),
    ([('Content-Encoding','br')],"http_get_unsupported_encoding"),
    ([('Content-Encoding','gzip, gzip')],"http_get_unsupported_encoding"),
])
def test_content_rejections_are_named(monkeypatch,headers,code):
    with pytest.raises(h.HttpGetError,match=code): run(monkeypatch,[response(headers=headers)])


def test_gzip_accept_control_and_expansion_bound(monkeypatch):
    packed=gzip.compress(b"hello")
    assert json.loads(run(monkeypatch,[response(packed,headers=[("Content-Encoding","gzip")])]))["body"]=="hello"
    bomb=gzip.compress(b"x"*101)
    with pytest.raises(h.HttpGetError,match="http_get_size_cap"):
        run(monkeypatch,[response(bomb,headers=[("Content-Encoding","gzip")])],max_bytes=100)

@pytest.mark.parametrize("payload",[b"not gzip",gzip.compress(b"ok")[:-2]])
def test_bad_gzip_is_named(monkeypatch,payload):
    with pytest.raises(h.HttpGetError,match="http_get_invalid_encoding"):
        run(monkeypatch,[response(payload,headers=[("Content-Encoding","gzip")])])


@pytest.mark.parametrize(
    "payload",
    [gzip.compress(b"first") + gzip.compress(b"second"), gzip.compress(b"first") + b"garbage"],
    ids=["second-member", "trailing-garbage"],
)
def test_gzip_is_exactly_one_member_with_no_trailing_bytes(monkeypatch, payload):
    with pytest.raises(h.HttpGetError, match="http_get_invalid_encoding"):
        run(monkeypatch,[response(payload,headers=[("Content-Encoding","gzip")])])


def test_wire_bound_charges_compressed_bytes_the_decoded_bound_cannot_see(monkeypatch):
    """§9.3.3: both bounds are required; neither substitutes for the other.

    A level-0 gzip stream is valid and LARGER on the wire than inflated, so the
    decoded bound never fires and only the wire bound can refuse it. Every other
    wire-bound case here uses an identity body, where the decoded bound fires too
    and the two are told apart only by the message text. This one fails open
    without the wire check. The accepted half is the paired control: a rejections-
    only suite passes a transport that refuses every compressed response.
    """
    body=b"a"*200
    packed=gzip.compress(body,0)
    over=len(body)+10
    assert len(body) <= over < len(packed), "premise: wire exceeds the cap while decoded does not"
    with pytest.raises(h.HttpGetError,match="wire bytes"):
        run(monkeypatch,[response(packed,headers=[("Content-Encoding","gzip")])],max_bytes=over)
    out=run(monkeypatch,[response(packed,headers=[("Content-Encoding","gzip")])],max_bytes=len(packed))
    assert json.loads(out)["body"]==body.decode()


def test_wire_bound_is_independent(monkeypatch):
    with pytest.raises(h.HttpGetError,match="wire bytes"):
        run(monkeypatch,[response(b"a"*101)],max_bytes=100)
    assert json.loads(run(monkeypatch,[response(b"a"*100)],max_bytes=100))["body"]=="a"*100


def test_missing_length_chunked_body_still_hits_wire_bound(monkeypatch):
    raw=(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nTransfer-Encoding: chunked\r\n\r\n"
         b"65\r\n"+b"a"*101+b"\r\n0\r\n\r\n")
    with pytest.raises(h.HttpGetError,match="wire bytes"):
        run(monkeypatch,[raw],max_bytes=100)


def test_lying_content_length_cannot_replace_streaming_bound(monkeypatch):
    """§9.3.3: `Content-Length` is a claim by the host that controls the body.

    The refusal half is the case §11 names: 101 bytes arrive under a declared
    length of 1, and the streaming bound refuses them.

    The other two halves are the framing control: the declared length is under
    `max_bytes`, so no bound can fire and only the framing decides the answer.
    Trusting it hands the model a body truncated at the declared length that it
    believes is complete — the §9.3 lie, not a named error. §11 requires both
    delivery shapes, the whole body in one read and the same body dribbled.
    """
    raw=(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 1\r\n\r\n"+b"a"*101)
    with pytest.raises(h.HttpGetError,match="wire bytes"):
        run(monkeypatch,[raw],max_bytes=100)
    body=bytes(range(97,97+26))*4
    understated=(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 10\r\n\r\n"+body)
    assert json.loads(run(monkeypatch,[understated]))["body"]==body.decode()
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",80))])
    monkeypatch.setattr(h,"_connect",lambda *a,**k:GradualSocket(understated))
    out=h.http_get("http://public.example/x",max_bytes=1000,timeout_ms=1000,max_redirects=0,
                   remaining_budget=1,credentials={})
    assert json.loads(out)["body"]==body.decode()


def test_missing_content_type_and_header_bomb_are_named(monkeypatch):
    # A body to interpret and no media type to interpret it with. The zero-length
    # version of this response is NOT an error — see the empty-body controls below.
    missing=b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\npayload"
    with pytest.raises(h.HttpGetError,match="http_get_unsupported_content"): run(monkeypatch,[missing])
    huge=b"HTTP/1.1 200 OK\r\nX: "+b"a"*h.MAX_HEADER_BYTES
    with pytest.raises(h.HttpGetError,match="http_get_size_cap"): run(monkeypatch,[huge])


def test_a_204_is_an_answer_not_an_unsupported_content_error(monkeypatch):
    """§9.3.3 empty-body row: a 204 is an answer, and a real one carries no
    `Content-Type`. Checking the media type before reading the body refused every
    204 — the two rows contradicted each other and the stricter one won.
    """
    out=run(monkeypatch,[b"HTTP/1.1 204 No Content\r\n\r\n"])
    parsed=json.loads(out)
    assert parsed["status"]==204 and parsed["body"]==""


def test_empty_body_is_valid_regardless_of_content_type(monkeypatch):
    """"Regardless" is the spec's word: the allowlist applies only where there is
    a body to interpret, so a disallowed media type on an empty body is moot."""
    raw=b"HTTP/1.1 204 No Content\r\nContent-Type: image/png\r\n\r\n"
    assert json.loads(run(monkeypatch,[raw]))["body"]==""


def test_non_empty_body_still_needs_a_media_type(monkeypatch):
    """Paired control. Without it, deleting the media-type check entirely passes
    the two empty-body cases above."""
    with pytest.raises(h.HttpGetError,match="http_get_unsupported_content"):
        run(monkeypatch,[b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi"])
    with pytest.raises(h.HttpGetError,match="http_get_unsupported_content"):
        run(monkeypatch,[response(b"hi",headers=[("Content-Type","image/png")])])


def test_header_cap_is_cumulative_across_hops(monkeypatch):
    """§9.3.3: headers are bounded at 64 KiB TOTAL across all hops.

    Each hop below is inside the cap on its own, so a per-response counter serves
    the whole chain without complaint; only the cumulative one refuses.
    """
    pad=b"a"*(h.MAX_HEADER_BYTES//2)
    redirect=b"HTTP/1.1 302 Found\r\nLocation: /next\r\nX: "+pad+b"\r\n\r\n"
    final=b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nX: "+pad+b"\r\n\r\nok"
    assert max(len(redirect),len(final)) < h.MAX_HEADER_BYTES < len(redirect)+len(final)
    with pytest.raises(h.HttpGetError,match="http_get_size_cap"):
        run(monkeypatch,[redirect,final],max_redirects=1)
    lean=b"HTTP/1.1 302 Found\r\nLocation: /next\r\n\r\n"
    assert json.loads(run(monkeypatch,[lean,final],max_redirects=1))["body"]=="ok"


def test_the_header_cap_is_enforced_on_a_block_no_read_landed_past(monkeypatch):
    """§11: exercised with the read boundary SHORT of the cap, not on it.

    The in-loop check fires only when a read has already landed at or past
    65536, so it enforces nothing for a header block whose every read boundary
    sits below the cap — the post-loop re-check against the block itself is
    then the only enforcement, and deleting it leaves the whole suite green.
    65537 is the largest block this loop can produce (the recv limit is
    `MAX_HEADER_BYTES + 1 - len(data)`), so it is the one block that overshoots
    the cap while the last loop-top check saw 65000: `FakeSocket` always
    returns the full requested length and lands on 4096-byte boundaries, which
    cannot produce that shape, so this reads in short 1000-byte steps and the
    final short read of 537 carries the terminator across.
    """
    prefix=b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nX: "
    head=prefix+b"a"*(h.MAX_HEADER_BYTES-3-len(prefix))
    raw=head+b"\r\n\r\n"+b"ok"
    assert len(head)+4==h.MAX_HEADER_BYTES+1, "premise: the block is one byte over the cap"
    sock=GradualSocket(raw,step=1000)
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",80))])
    monkeypatch.setattr(h,"_connect",lambda *a,**k:sock)
    with pytest.raises(h.HttpGetError) as caught:
        h.http_get("http://public.example/x",max_bytes=1000,timeout_ms=1000,max_redirects=0,
                   remaining_budget=1,credentials={})
    assert caught.value.code=="http_get_size_cap"


def test_the_header_cap_charges_header_bytes_and_not_redirect_overshoot(monkeypatch):
    """§11: a control that fails an implementation billing overshoot to the cap.

    The recv that finishes a header block lifts whatever body bytes shared the
    segment with it. Those bytes are discarded uncharged (§9.3.3), so the hop
    below charges 63500 of the 64 KiB. Returning `cumulative + len(data)`
    instead charges the 2036 bytes of 302 body that arrived with the headers,
    which puts the running total at exactly 65536 and refuses the next hop —
    a chain whose header bytes are 8 KiB inside the cap. That overshoot is
    charged against no bound at all, by design.
    """
    prefix=b"HTTP/1.1 302 Found\r\nLocation: /next\r\nX: "
    head=prefix+b"a"*(63500-4-len(prefix))
    redirect=head+b"\r\n\r\n"+b"z"*8192
    assert len(head)+4==63500 < h.MAX_HEADER_BYTES
    sockets=[FakeSocket(redirect),FakeSocket(response())]
    first=sockets[0]
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",80))])
    monkeypatch.setattr(h,"_connect",lambda *a,**k:sockets.pop(0))
    out=h.http_get("http://public.example/x",max_bytes=1000,timeout_ms=1000,max_redirects=1,
                   remaining_budget=1,credentials={})
    assert json.loads(out)["body"]=="ok"
    # Premise: body bytes really did arrive with the header block, so there was
    # an overshoot to mis-charge. Without this the test could pass vacuously.
    consumed=len(redirect)-len(first.response)
    assert consumed-(len(head)+4)==2036


def test_redirect_boundary_zero_and_relative_resolution(monkeypatch):
    redir=response(b"ignored",302,[("Location","/next")])
    with pytest.raises(h.HttpGetError,match="http_get_redirect_cap"):
        run(monkeypatch,[redir],max_redirects=0)
    out=run(monkeypatch,[redir,response()],max_redirects=1)
    assert json.loads(out)["url"]=="http://public.example/next"


def test_the_redirect_cap_also_refuses_above_zero(monkeypatch):
    """§11: the `N >= 1` refusal is required, not just the `N = 0` one.

    A suite covering only zero passes an implementation whose cap fires
    exclusively there and follows redirects forever at the shipped default of
    5 — the configuration every bot runs. The third response exists so that
    such an implementation returns a 200 here instead of running out of
    sockets: the refusal is what is asserted, and the hop count is asserted
    with it so a cap that fires one hop early cannot pass either.
    """
    redir=response(b"ignored",302,[("Location","/next")])
    hosts=_connections_recording(monkeypatch,[redir,redir,response()])
    with pytest.raises(h.HttpGetError) as caught:
        h.http_get("http://public.example/x",max_bytes=1000,timeout_ms=1000,max_redirects=1,
                   remaining_budget=1,credentials={})
    assert caught.value.code=="http_get_redirect_cap"
    assert len(hosts)==2


def test_a_redirect_body_costs_nothing_and_is_never_drained(monkeypatch):
    """§9.3.3: a 3xx body is never charged, and no read follows the terminator.

    The operative property is not how many body bytes arrive — it is that they
    reach no one and cost nothing. `max_bytes` here leaves 100 bytes of slack
    over the final body, so a 1 MiB 302 body billed against it refuses the whole
    chain; the fetch SUCCEEDING is the charging control. The second assertion is
    the draining control, counted in reads rather than bytes: a byte count of
    zero can only be met by a one-byte-at-a-time header read, which buys nothing
    but syscalls, while a read issued after the header terminator is exactly the
    drain the rule forbids.
    """
    body=b"z"*(1024*1024); final=b"a"*900
    sockets=[PostHeaderReadCountingSocket(response(body,302,[("Location","/next")])),FakeSocket(response(final))]; used=[]
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",80))])
    def connect(*a,**k): used.append(sockets.pop(0)); return used[-1]
    monkeypatch.setattr(h,"_connect",connect)
    out=h.http_get("http://public.example",max_bytes=1000,timeout_ms=1000,max_redirects=1,remaining_budget=1,credentials={})
    assert json.loads(out)["body"]==final.decode()
    assert used[0].reads_after_headers==0 and used[0].closed


@pytest.mark.parametrize(("url","code"),[
    ("file:///etc/passwd","http_get_invalid_url"),
    ("ftp://public.example/x","http_get_invalid_url"),
    ("http://user@public.example/","http_get_invalid_url"),
    ("http:///nohost","http_get_invalid_url"),
    ("http://public.example./x","http_get_invalid_url"),
    ("http://svc.internal./x","http_get_blocked_address"),
    ("http://public.example/"+"x"*8192,"http_get_url_too_long"),
    ("http://public.example/x#"+"y"*9000,"http_get_url_too_long"),
],ids=["file-scheme","ftp-scheme","userinfo","no-host","trailing-dot","blocked-trailing-dot","too-long","too-long-fragment"])
def test_hop_zero_url_rejections_are_invalid_url_not_bad_redirect(url,code):
    """Hop zero: the URL came from the model, so there is no `Location` to blame.

    Paired with `test_redirect_hop_url_rejections_keep_the_redirect_codes` below
    — every fault here carries a different code on a redirect hop, so neither
    test is right unless it names the hop it exercises.

    SPEC §10 names `invalid_url` for a model-supplied scheme other than
    `http`/`https` and `bad_redirect_scheme` only for the redirect hop; the
    scheme case previously reported a *redirect* error where no `Location`
    existed. `ftp://public.example/x` is the discriminating one — it carries a
    hostname, so deleting the scheme check entirely leaves it ACCEPTED, while
    `file:///etc/passwd` has no host and would be refused by the hostname check
    anyway. A trailing dot is `invalid_url` here UNLESS the host is also under
    the blocked-name set: §10 orders the two predicates name-first, so
    `public.example.` is the grammar fault and `svc.internal.` is the forbidden
    destination. §11 names that ASCII hop-zero row explicitly, and every other
    trailing-dot case in this file uses a host nobody blocked, so an
    implementation refusing the trailing dot on the PRE-IDNA hostname for hop
    zero alone passes all of them.

    The fragment row is the entry bound doing work the post-normalisation check
    cannot: `urlunsplit` drops the fragment, so a 9 KiB one is under the ceiling
    by the time the second check reads it.
    """
    with pytest.raises(h.HttpGetError) as caught: h._normalize_url(url)
    assert caught.value.code==code

@pytest.mark.parametrize("raw",[
    "http://public.example/a b","http://public.example/a\x7fb","http://public.example/a\x00b",
    "http://public.example/a\tb","http://public.example/a\rb","http://public.example/a\nb",
    "http://pub lic.example/a"," http://public.example/a",
])
def test_out_of_repertoire_bytes_in_a_url_are_refused_not_encoded(raw):
    """§9.3.3: a byte `<= 0x20` or `== 0x7F` is REFUSED, never percent-encoded.

    `urlsplit` strips `\t\r\n` and silently keeps a raw space, so `/a b`
    otherwise reaches the wire as `GET /a b HTTP/1.1` — a request line a lenient
    origin may re-parse as a different target than the URL reported back to the
    model. Encoding it instead would invent a destination the server never
    named, which is the same silent reinterpretation the no-sniffing rule
    forbids. The stripped-by-urlsplit trio is here because their being stripped
    is not the same as their being refused.
    """
    with pytest.raises(h.HttpGetError) as caught: h._normalize_url(raw)
    assert caught.value.code=="http_get_invalid_url"


def test_an_already_encoded_url_still_fetches():
    """Paired positive: a check that refused every URL would pass the rejections
    above. `%20` is the encoding the client must NOT perform for itself, and it
    is legal arriving from the model."""
    url,_,_=h._normalize_url("http://public.example/a%20b?q=x%7F")
    assert url=="http://public.example/a%20b?q=x%7F"


@pytest.mark.parametrize("location",[
    "//evil.example\n/x","//evil.example\r/x","//evil.example\t/x","//evil.example/a b",
],ids=["lf","cr","tab","space"])
def test_a_location_out_of_the_repertoire_is_refused_before_resolution(monkeypatch,location):
    """§9.3.3: the repertoire check binds the RAW `Location` value BEFORE any
    RFC 3986 resolution, as well as the resolved URL.

    This is the site that matters: a `Location` is attacker-supplied where hop
    zero's URL is the model's. Checking only after resolution cannot refuse the
    first three at all — `urljoin`/`urlsplit` strip `\\t`, `\\r` and `\\n`
    before the check ever sees them, so `//evil.example\\n/x` resolves to a
    clean `http://evil.example/x` and is FOLLOWED to a host the origin never
    named as a URL. Hop zero refuses the same bytes because it checks the
    string it was handed; being stripped is not the same as being refused. The
    space is the one byte `urlsplit` keeps, so it is the only case a
    resolved-URL-only check already refused — it is here as the shape the
    other three were mistaken for. The reached-hosts assertion is the teeth:
    an implementation that refuses only after connecting to `evil.example` has
    already made the request the rule exists to prevent.
    """
    hosts=_connections_recording(monkeypatch,[response(b"",302,[("Location",location)]),response()])
    with pytest.raises(h.HttpGetError) as caught:
        h.http_get("http://public.example/x",max_bytes=1000,timeout_ms=1000,max_redirects=5,
                   remaining_budget=1,credentials={})
    assert caught.value.code=="http_get_bad_redirect"
    assert hosts==["public.example"]


@pytest.mark.parametrize(("location","code"),[
    ("ftp://evil.example/x","http_get_bad_redirect_scheme"),
    ("file:///etc/passwd","http_get_bad_redirect_scheme"),
    ("http://public.example./x","http_get_bad_redirect"),
],ids=["ftp","file","trailing-dot"])
def test_redirect_hop_url_rejections_keep_the_redirect_codes(monkeypatch,location,code):
    """The redirect half of the hop-zero pair above (SPEC §10).

    A forbidden scheme is `bad_redirect_scheme` HERE and `invalid_url` on hop
    zero; a trailing dot on a host nobody blocked — which `public.example.` is —
    is `bad_redirect` here and `invalid_url` there. A trailing dot on a BLOCKED
    name is `blocked_address` on both hops, since §10 judges the name first;
    that pair is covered on hop zero by the `svc.internal./x` row above and on
    the wire by
    `test_a_redirect_to_a_blocked_name_with_a_trailing_dot_is_blocked_not_grammar`.
    Both halves are asserted because fixing one of them alone is the miss SPEC
    §10 records having made before.
    """
    hosts=_connections_recording(monkeypatch,[response(b"",302,[("Location",location)]),response()])
    with pytest.raises(h.HttpGetError) as caught:
        h.http_get("http://public.example/x",max_bytes=1000,timeout_ms=1000,max_redirects=5,
                   remaining_budget=1,credentials={})
    assert caught.value.code==code
    assert hosts==["public.example"]


def test_a_redirect_to_an_ordinary_location_still_follows(monkeypatch):
    """Paired positive for the hop-level check, for the same reason as above."""
    out=run(monkeypatch,[response(b"",302,[("Location","/a%20b")]),response()])
    assert json.loads(out)["url"]=="http://public.example/a%20b"


@pytest.mark.parametrize("address",["127.0.0.1","::ffff:127.0.0.1","::ffff:100.64.0.1","::127.0.0.1","fe80::1","0.0.0.0","2001:db8::1"])
def test_all_non_global_addresses_refused(address):
    """§9.3.3: IPv4-mapped forms normalise to the v4 address BEFORE comparing.

    `::ffff:127.0.0.1` documents the intent §11 names, but it cannot discriminate:
    CPython already reports it non-global, so deleting the normalisation leaves it
    refused anyway. `::ffff:100.64.0.1` is the family that trips — the mapped form
    is `is_global` True and only the v4 CGNAT rule refuses it.
    """
    family=socket.AF_INET6 if ":" in address else socket.AF_INET
    resolver=lambda *a:[(family,socket.SOCK_STREAM,0,"",(address,443))]
    with pytest.raises(h.HttpGetError,match="http_get_blocked_address"):
        h._approved_addresses("x",443,time.monotonic()+1,resolver)


def _resolving_to(address):
    family=socket.AF_INET6 if ":" in address else socket.AF_INET
    return lambda *a:[(family,socket.SOCK_STREAM,0,"",(address,443))]


@pytest.mark.parametrize("address",["64:ff9b::a00:1","64:ff9b::7f00:1","239.255.255.250","224.0.0.1","ff02::1"])
def test_addresses_is_global_approves_are_still_refused(address):
    """§9.3.3: `is_global` alone is NOT the contract and must not be the whole
    predicate. Every address here is `is_global` True on CPython, so each one
    fails an implementation whose entire post-resolution check is
    `if not ip.is_global: refuse` — the two NAT64 forms with teeth, since
    `64:ff9b::a00:1` IS `10.0.0.1` and `64:ff9b::7f00:1` IS `127.0.0.1` to the
    gateway that translates them. The premise is asserted rather than trusted:
    if CPython ever refuses these itself, this stops discriminating and should
    say so out loud instead of passing quietly.
    """
    assert ipaddress.ip_address(address).is_global, "premise: the obvious predicate approves this"
    with pytest.raises(h.HttpGetError,match="http_get_blocked_address"):
        h._approved_addresses("x",443,time.monotonic()+1,_resolving_to(address))


def test_nat64_embedding_of_a_public_address_is_approved():
    """Paired positive. Refusing `64:ff9b::/96` wholesale passes every rejection
    above; only this fails it. The embedded v4 is `93.184.216.34`, and it is the
    v4 rules — not the prefix — that decide."""
    assert h._approved_addresses("x",443,time.monotonic()+1,_resolving_to("64:ff9b::5db8:d822"))


def test_local_use_nat64_prefix_is_refused():
    """RFC 8215's `64:ff9b:1::/48`, which SPEC §9.3.3 names alongside the
    well-known prefix. Documented, not discriminating: CPython already reports
    this prefix non-global, so it stays refused with the NAT64 handling deleted.
    It is refused whole rather than unpacked — the RFC 6052 embedding length
    inside it is chosen per deployment, so no extraction is trustworthy.
    """
    with pytest.raises(h.HttpGetError,match="http_get_blocked_address"):
        h._approved_addresses("x",443,time.monotonic()+1,_resolving_to("64:ff9b:1::a00:1"))


@pytest.mark.parametrize("url",[
    "http://foo.internal/","http://printer.local/","http://box.localhost/",
    "http://router.home.arpa/","http://nas.home/","http://printer.lan/",
    "http://y.test/","http://nope.invalid/","http://x.onion/",
    "http://X.ONION/","http://local/",
])
def test_host_shapes_refused_before_any_resolution(url):
    """§9's blocked-name set is refused at the URL rather than left to DNS — a
    check no resolver answer can reach, so nothing else in the suite covers it.
    The trailing-dot host moved to the hop-zero and redirect-hop code tests:
    SPEC §10 says it is never `blocked_address`. Every name in the set is
    listed because four of them — `home.arpa`,
    `test`, `invalid` and `onion` — were absent from the code while §9 named
    them, so `http://x.onion/` reached resolution. `X.ONION` is the
    case-insensitivity case and bare `local` is the whole-host case, which a
    suffix comparison against `".local"` never reaches.
    """
    with pytest.raises(h.HttpGetError) as caught: h._normalize_url(url)
    assert caught.value.code=="http_get_blocked_address"


@pytest.mark.parametrize("host",[
    "mylocal.example.com","localhost.example.com","test.example.com","onion.example.com","home.arpa.example.com",
    "printer.notlocal","www.contest","gear.mylan","subhome.arpa",
])
def test_a_blocked_name_matches_whole_trailing_labels_only(host):
    """Paired positive. §9 blocks `printer.local` and NOT `mylocal.example.com`,
    so the comparison is label-anchored rather than a raw suffix or substring.

    The `.example.com` half only trips a substring test — every one of those
    hosts ends in `com`, so a dotless `endswith` survives them all. The four
    below are the ones that discriminate: their FINAL label ends with a blocked
    name without being it, which is precisely what `endswith("local")` cannot
    tell apart. `subhome.arpa` is the same trap one label further in.
    """
    url,origin,_=h._normalize_url(f"http://{host}/x")
    assert origin.host==host


# SPEC §9.3.3's post-IDNA row and §11's alternate-separator control. Python's
# IDNA codec splits labels on U+3002, U+FF0E and U+FF61 as well as `.`, and
# folds a trailing one into an ASCII trailing dot. Every case here is refused
# with the SAME code whether the separator is ASCII or one of the three, which
# is the property "every host predicate reads one form of the host" produces.
_ALTERNATE_SEPARATOR_HOSTS=[
    ("svc.internal。","blocked"),
    ("x.onion．","blocked"),
    ("printer.local｡","blocked"),
    ("a。internal","blocked"),
    ("box.home.arpa。","blocked"),
    ("example.com。","grammar"),
    ("public.example｡","grammar"),
]
_SEPARATOR_IDS=["ideographic-internal","fullwidth-onion","halfwidth-local","interior-separator",
                "ideographic-home-arpa","ideographic-bare-name","halfwidth-bare-name"]


@pytest.mark.parametrize(("host","kind"),_ALTERNATE_SEPARATOR_HOSTS,ids=_SEPARATOR_IDS)
def test_alternate_idna_separators_are_judged_on_the_post_idna_host_at_hop_zero(host,kind):
    """§11: an ASCII-only control cannot see this defect, which is why it lived.

    `test_a_blocked_name_matches_whole_trailing_labels_only` and the
    `http://public.example./x` case both pass whether or not the host predicates
    agree about which form of the host they read. One character split them: with
    the trailing-dot check on the PRE-IDNA `parts.hostname` and the name check
    on the POST-IDNA `host`, `http://svc.internal。/x` passed both — the
    pre-IDNA string does not end in `.`, and `svc.internal.` does not end in
    `.internal` — and `svc.internal.` is a valid FQDN a real resolver answers,
    so the name reached DNS and the `Host` header with only the post-resolution
    address check left standing.

    Each row fails a different wrong implementation. The blocked rows fail one
    that runs the name check pre-IDNA; `interior-separator` fails it hardest,
    since post-IDNA it is plainly `a.internal` with no trailing dot to catch it
    on the way past. The grammar rows fail one that runs the trailing-dot check
    pre-IDNA — the shipped defect. And ordering matters in both directions: the
    blocked rows also fail an implementation that judges the trailing dot before
    the name set, which reports `svc.internal。` as a grammar fault rather
    than the forbidden destination §11 names it as.
    """
    with pytest.raises(h.HttpGetError) as caught: h._normalize_url(f"http://{host}/x")
    assert caught.value.code==("http_get_blocked_address" if kind=="blocked" else "http_get_invalid_url")


@pytest.mark.parametrize(("host","kind"),_ALTERNATE_SEPARATOR_HOSTS,ids=_SEPARATOR_IDS)
def test_alternate_idna_separators_are_judged_on_the_post_idna_host_on_a_redirect(host,kind):
    """The redirect half of the pair above, and it is asserted DELIBERATELY.

    These hosts are refused end-to-end today only by accident: `_read_headers`
    decodes header values as latin-1, so the UTF-8 bytes of U+3002 arrive as
    mojibake and IDNA raises on the C1 characters. That is a property of the
    decode, not a control — change the decode and the accident evaporates. So
    the redirect hop is exercised where the hop actually judges the host, with
    the separator intact, and the codes it must produce are §10's redirect-hop
    codes: `http_get_blocked_address` for a forbidden destination (the same code
    on either hop, because it is the §9 check refusing) and
    `http_get_bad_redirect` for the grammar fault, never `invalid_url` here.
    The wire-level companion is
    `test_a_redirect_to_a_blocked_name_with_a_trailing_dot_is_blocked_not_grammar`.
    """
    with pytest.raises(h.HttpGetError) as caught: h._normalize_url(f"http://{host}/x",redirect=True)
    assert caught.value.code==("http_get_blocked_address" if kind=="blocked" else "http_get_bad_redirect")


def test_a_redirect_to_a_blocked_name_with_a_trailing_dot_is_blocked_not_grammar(monkeypatch):
    """The reachable-over-a-real-wire half: `svc.internal.` needs no separator.

    A trailing dot is representable in a header value, so this is the shape a
    hostile origin can actually send, and it exercises the same ordering the
    alternate-separator rows pin: the blocked name is judged before the trailing
    dot, on the post-IDNA host. An implementation checking the trailing dot
    first reports `http_get_bad_redirect` here and fails, and one whose
    `_blocked_name` does not drop the root label fails the same way.
    """
    hosts=_connections_recording(monkeypatch,[response(b"",302,[("Location","http://svc.internal./x")]),response()])
    with pytest.raises(h.HttpGetError) as caught:
        h.http_get("http://public.example/x",max_bytes=1000,timeout_ms=1000,max_redirects=5,
                   remaining_budget=1,credentials={})
    assert caught.value.code=="http_get_blocked_address"
    assert hosts==["public.example"], "the blocked name is refused before any connection to it"


def test_normalisation_growth_is_re_checked_against_the_url_bound():
    """§9.3.3 bounds the URL at 8192 bytes; normalisation can cross it AFTER the
    entry check passes. This URL is exactly at the bound and has an empty path,
    which normalises to `/` — one byte over. Without the second check the
    request line and the audit value are both unbounded by exactly the amount
    normalisation adds.
    """
    raw="http://public.example?"+"x"*(h.MAX_URL_BYTES-22)
    assert len(raw.encode())==h.MAX_URL_BYTES
    with pytest.raises(h.HttpGetError) as caught: h._normalize_url(raw)
    assert caught.value.code=="http_get_url_too_long"
    # Paired positive: one byte shorter parses, so the check is the bound and
    # not a refusal of every normalised URL.
    assert h._normalize_url(raw[:-1])[0]


def test_gzip_is_fed_incrementally_so_the_bomb_stops_at_the_bound(monkeypatch):
    """§9.3.3: the decoder MUST be fed incrementally so it can be stopped AT the
    bound rather than after it — decompressing to completion and then measuring
    is the bomb working as designed.

    Both implementations end at the same named error, so the error cannot
    discriminate: what differs is how much of the 64 MiB expansion is
    materialised first. `decompress(chunk)` without a `max_length` inflates each
    16 KiB wire chunk to ~16 MiB before anything is measured; the bounded call
    returns `max_bytes + 1` bytes and stops. Peak traced allocation is the only
    observable that separates them, so it is what is asserted.
    """
    packed=gzip.compress(b"\0"*(64*1024*1024))
    raw=response(packed,headers=[("Content-Encoding","gzip")])
    assert len(packed) < 200_000, "premise: the wire bound must not fire first"
    tracemalloc.start()
    try:
        with pytest.raises(h.HttpGetError,match="http_get_size_cap"):
            run(monkeypatch,[raw],max_bytes=200_000)
        peak=tracemalloc.get_traced_memory()[1]
    finally: tracemalloc.stop()
    assert peak < 4*1024*1024, f"decoder materialised {peak} bytes before the bound"


def rebinding_resolver(answers):
    """A resolver that changes its answer between calls, and counts them."""
    calls=[]
    def resolve(host,port,*a,**k):
        calls.append(host); address=answers[min(len(calls),len(answers))-1]
        return [(socket.AF_INET,socket.SOCK_STREAM,6,"",(address,port))]
    resolve.calls=calls
    return resolve


def test_connect_is_pinned_to_the_validated_address_not_the_hostname(monkeypatch):
    """SPEC §9.3.3: handing the HOSTNAME to the client is check-then-use.

    Nothing here fakes `_connect`, so the join between the approved addresses and
    the socket is under test. The fake models an OS truthfully: an address that is
    not an IP literal is resolved again at connect, and the rebound private answer
    is reachable — that is why the attack works. The assertion is therefore on the
    address actually reached, not on a connect failure.
    """
    resolve=rebinding_resolver(["93.184.216.34","169.254.169.254"])
    reached=[]
    class RebindingSocket(FakeSocket):
        def __init__(self): super().__init__(response(b"ok"))
        def connect(self,sockaddr):
            host=sockaddr[0]
            try: ipaddress.ip_address(host)
            except ValueError: host=resolve(host,sockaddr[1])[0][4][0]
            reached.append(host)
    monkeypatch.setattr(socket,"socket",lambda *a,**k:RebindingSocket())
    out=h.http_get("http://public.example/x",max_bytes=1000,timeout_ms=1000,max_redirects=0,
                   remaining_budget=1,credentials={},resolver=resolve)
    assert json.loads(out)["body"]=="ok"
    assert reached==["93.184.216.34"] and resolve.calls==["public.example"]


def test_dns_rebind_between_hops_is_refused_with_a_stable_resolver_control(monkeypatch):
    """A resolver answering public then private must fail the fetch (§9.3.3).

    Redirects repeat resolve/validate/pin, so hop two sees the rebound answer.
    The paired control answers public twice: without it an implementation that
    refuses every fetch passes the refusal half.
    """
    replies=[response(b"",302,[("Location","/next")]),response(b"ok")]
    def connect_from(replies):
        remaining=list(replies)
        return lambda *a,**k: FakeSocket(remaining.pop(0))
    monkeypatch.setattr(h,"_connect",connect_from(replies))
    rebind=rebinding_resolver(["93.184.216.34","169.254.169.254"])
    with pytest.raises(h.HttpGetError,match="http_get_blocked_address"):
        h.http_get("http://public.example/x",max_bytes=1000,timeout_ms=1000,max_redirects=1,
                   remaining_budget=1,credentials={},resolver=rebind)
    assert len(rebind.calls)==2
    monkeypatch.setattr(h,"_connect",connect_from(replies))
    stable=rebinding_resolver(["93.184.216.34"])
    out=h.http_get("http://public.example/x",max_bytes=1000,timeout_ms=1000,max_redirects=1,
                   remaining_budget=1,credentials={},resolver=stable)
    assert json.loads(out)["body"]=="ok" and len(stable.calls)==2


def test_credentials_same_origin_forward_then_permanently_drop(monkeypatch):
    cred=h.Credential("token-id",h.Origin("https","a.example",443),"Authorization","SECRET")
    replies=[response(b"",302,[("Location","https://a.example:443/2")]),response(b"",302,[("Location","https://b.example/3")]),response(b"",302,[("Location","https://a.example/4")]),response(b"SECRET")]
    sockets=[]
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",443))])
    def connect(*a,**k): s=FakeSocket(replies[len(sockets)]); sockets.append(s); return s
    monkeypatch.setattr(h,"_connect",connect)
    out=h.http_get("https://a.example/",max_bytes=1000,timeout_ms=1000,max_redirects=3,remaining_budget=1,credentials={cred.origin:cred})
    assert b"Authorization: SECRET" in sockets[0].sent and b"Authorization: SECRET" in sockets[1].sent
    assert b"Authorization: SECRET" not in sockets[2].sent+sockets[3].sent
    # "token-id" in out was vacuous: redaction rewrites the reflected SECRET to
    # the id, so the assertion held whatever credential_id reported. Assert the
    # redaction on the body and the attachment on the field that carries it.
    parsed=json.loads(out)
    assert "SECRET" not in out and parsed["body"]=="token-id"
    assert parsed["credential_attached"] is False and parsed["credential_id"] is None
    assert out.audit_fields["credential_drop"]=={"event":"credential_dropped","hop":"https://b.example:443"}


def test_credential_attached_describes_the_final_hop_not_the_chain(monkeypatch):
    """A credential dropped cross-origin means the final request was unauthenticated.

    SPEC §9.3.3 result shape: `credential_attached`/`credential_id` describe the
    request that produced THIS response, never the chain. §9.4.1 is why — GitHub
    answers 404 rather than 403 for an unauthorised private repo, so a chain-wide
    `true` on an unauthenticated 404 tells the model the repo does not exist.
    """
    cred=h.Credential("gh-token",h.Origin("https","api.github.com",443),"Authorization","Bearer ghp_x")
    replies=[response(b"",302,[("Location","https://evil.example/x")]),response(b"nope",404)]
    sockets=[]
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",443))])
    def connect(*a,**k): s=FakeSocket(replies[len(sockets)]); sockets.append(s); return s
    monkeypatch.setattr(h,"_connect",connect)
    out=h.http_get("https://api.github.com/repos/x",max_bytes=1000,timeout_ms=1000,max_redirects=1,remaining_budget=1,credentials={cred.origin:cred})
    assert b"Authorization" in sockets[0].sent and b"Authorization" not in sockets[1].sent
    parsed=json.loads(out)
    assert parsed["status"]==404
    assert parsed["credential_attached"] is False and parsed["credential_id"] is None
    # The audit keeps the chain-wide view: different question, different field.
    assert out.audit_fields["credential_origins"]==["https://api.github.com:443"]
    assert out.audit_fields["credential_drop"]=={"event":"credential_dropped","hop":"https://evil.example:443"}


def test_credential_attached_is_true_when_the_final_hop_carried_it(monkeypatch):
    """Paired positive: without it, a result hardcoding False passes the drop test."""
    cred=h.Credential("gh-token",h.Origin("https","api.github.com",443),"Authorization","Bearer ghp_x")
    replies=[response(b"",302,[("Location","https://api.github.com:443/repos/y")]),response(b"body",404)]
    sockets=[]
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",443))])
    def connect(*a,**k): s=FakeSocket(replies[len(sockets)]); sockets.append(s); return s
    monkeypatch.setattr(h,"_connect",connect)
    out=h.http_get("https://api.github.com/repos/x",max_bytes=1000,timeout_ms=1000,max_redirects=1,remaining_budget=1,credentials={cred.origin:cred})
    assert b"Authorization: Bearer ghp_x" in sockets[1].sent
    parsed=json.loads(out)
    assert parsed["status"]==404
    assert parsed["credential_attached"] is True and parsed["credential_id"]=="gh-token"


@pytest.mark.parametrize(("location","dropped_at"),[
    ("https://api.github.com:8443/x","https://api.github.com:8443"),
    ("http://api.github.com/x","http://api.github.com:80"),
],ids=["port","scheme-downgrade"])
def test_the_forwarding_comparison_is_the_whole_origin_not_the_host_alone(monkeypatch,location,dropped_at):
    """§11's FORWARDING half: a port change and a scheme downgrade both drop it.

    Every other drop control redirects to a different HOST, so weakening the
    comparison to `next_origin.host != selected.origin.host` passes all of them
    — and sends the bearer token to `:8443`, and on the second row over
    CLEARTEXT `http`. The selection half of the same comparison lives in
    `test_credentials_are_never_selected_mid_chain_or_for_other_port`, whose
    `:8443` case is hop ZERO, where the credential-map lookup misses before
    forwarding is ever consulted; nothing there reaches hop two.

    Asserted on the bytes sent on hop two, since that is where the leak would
    be — the result fields alone would pass an implementation that attaches the
    header and then reports `credential_attached: False`.
    """
    cred=h.Credential("gh-token",h.Origin("https","api.github.com",443),"Authorization","Bearer ghp_x")
    replies=[response(b"",302,[("Location",location)]),response(b"body")]
    sockets=[]
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",443))])
    def connect(*a,**k): s=FakeSocket(replies[len(sockets)]); sockets.append(s); return s
    monkeypatch.setattr(h,"_connect",connect)
    out=h.http_get("https://api.github.com/x",max_bytes=1000,timeout_ms=1000,max_redirects=1,remaining_budget=1,credentials={cred.origin:cred})
    # Premise: the credential WAS selected and forwarded on hop one, so hop
    # two's absence is the drop and not a selection that never happened.
    assert b"Authorization: Bearer ghp_x" in sockets[0].sent
    assert b"Authorization" not in sockets[1].sent and b"ghp_x" not in sockets[1].sent
    parsed=json.loads(out)
    assert parsed["credential_attached"] is False and parsed["credential_id"] is None
    assert out.audit_fields["credential_drop"]=={"event":"credential_dropped","hop":dropped_at}


def test_one_deadline_is_spent_across_redirect_hops(monkeypatch):
    replies=[response(b"",302,[("Location","/2")]),response()]
    sockets=[]
    def resolve(*a,**k):
        time.sleep(.035)
        return [(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",80))]
    monkeypatch.setattr(h,"_approved_addresses",resolve)
    monkeypatch.setattr(h,"_connect",lambda *a,**k: FakeSocket(replies.pop(0)))
    with pytest.raises(h.HttpGetError,match="http_get_timeout"):
        h.http_get("http://public.example",max_bytes=1000,timeout_ms=50,max_redirects=1,remaining_budget=1,credentials={})


class StallingSocket(FakeSocket):
    """Never completes a header block, so only the deadline can end the read.

    `limit` is the control's teeth: an implementation that ignores
    `remaining_budget` runs to `timeout_ms` instead, and rather than making the
    suite wait 30 seconds for that, the fake gives up first and says so.
    """
    def __init__(self, step=0.005, limit=200):
        super().__init__(b""); self.step=step; self.limit=limit; self.reads=0
    def recv(self,n):
        self.reads+=1
        if self.reads>self.limit:
            raise AssertionError(f"the deadline never bit after {self.reads} reads")
        time.sleep(self.step); return b"x"


def test_the_deadline_takes_the_budget_when_it_is_smaller_than_timeout_ms(monkeypatch):
    """§9.3.3: `deadline = now + min(timeout_ms, remaining turn budget)`.

    `test_one_deadline_is_spent_across_redirect_hops` passes `timeout_ms=50`
    against a budget of 1s, so its `min()` resolves to `timeout_ms` and the
    budget term is never taken — an implementation treating `timeout_ms` as a
    second independent deadline passes it. This is the reverse input, the only
    shape that exercises the other term: a 30s configured timeout against 0.05s
    of turn budget must expire at the budget.
    """
    sock=StallingSocket()
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",80))])
    monkeypatch.setattr(h,"_connect",lambda *a,**k:sock)
    started=time.monotonic()
    with pytest.raises(h.HttpGetError) as caught:
        h.http_get("http://public.example/x",max_bytes=1000,timeout_ms=30000,max_redirects=0,
                   remaining_budget=0.05,credentials={})
    assert caught.value.code=="http_get_timeout"
    assert time.monotonic()-started < 1, "the deadline came from timeout_ms, not the budget"


class RecordingSocket(GradualSocket):
    """Keeps the settimeout/recv interleaving; every other fake discards it."""
    def __init__(self, response, step=7): super().__init__(response,step); self.events=[]
    def settimeout(self,value): self.events.append(("settimeout",value)); super().settimeout(value)
    def recv(self,n): self.events.append(("recv",n)); return super().recv(n)


def test_body_reads_push_the_deadline_down_as_a_socket_timeout(monkeypatch):
    """§9.3.3: the deadline MUST also be pushed down as socket-level timeouts.

    `http_get` runs under `asyncio.to_thread`, and §9.3.2's caveat says an outer
    cancel cannot be assumed to interrupt a recv already in progress — a stalled
    body read with no socket timeout is the unkillable thread this clause exists
    to prevent. Every other fake here ignores `settimeout`, so dropping it from
    the body loop leaves them all green; this one records the interleaving.
    """
    body=bytes(range(97,97+26))*4
    raw=(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
         +str(len(body)).encode()+b"\r\n\r\n"+body)
    sock=RecordingSocket(raw)
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",80))])
    monkeypatch.setattr(h,"_connect",lambda *a,**k:sock)
    out=h.http_get("http://public.example/x",max_bytes=1000,timeout_ms=1000,max_redirects=0,
                   remaining_budget=1,credentials={})
    # Premise: the body could not fit in the header read, so it was streamed.
    assert json.loads(out)["body"]==body.decode()
    events=sock.events
    assert all(i and events[i-1][0]=="settimeout" and events[i-1][1] > 0
               for i,(kind,_) in enumerate(events) if kind=="recv"), events


def test_tls_pin_keeps_hostname_for_sni_and_certificate_identity(monkeypatch):
    calls={}
    class Raw:
        def settimeout(self,v): calls["timeout"]=v
        def connect(self,addr): calls["address"]=addr
        def close(self): pass
    class Context:
        def wrap_socket(self,raw,server_hostname): calls["sni"]=server_hostname; return raw
    monkeypatch.setattr(socket,"socket",lambda *a:Raw())
    out=h._connect(h.Origin("https","api.example.com",443),time.monotonic()+1,[(socket.AF_INET,socket.SOCK_STREAM,6,("93.184.216.34",443))],Context())
    assert out and calls["address"][0]=="93.184.216.34" and calls["sni"]=="api.example.com"

def test_default_tls_context_verifies_certificate_identity(monkeypatch):
    """§11: a pinning test that passes by disabling certificate identity has
    tested the wrong thing and must itself fail.

    Every other control here supplies its own context or never reaches TLS, so
    `ssl._create_unverified_context()` in the transport leaves them green. Assert
    the context http_get actually builds.
    """
    captured={}
    def connect(origin,deadline,addresses,ssl_context):
        captured["context"]=ssl_context; return FakeSocket(response())
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",443))])
    monkeypatch.setattr(h,"_connect",connect)
    h.http_get("https://public.example/",max_bytes=1000,timeout_ms=1000,max_redirects=0,remaining_budget=1,credentials={})
    context=captured["context"]
    assert context.verify_mode is ssl.CERT_REQUIRED and context.check_hostname is True


def test_every_resolved_address_is_validated_with_public_control():
    public=(socket.AF_INET,socket.SOCK_STREAM,0,"",("93.184.216.34",443))
    private=(socket.AF_INET,socket.SOCK_STREAM,0,"",("127.0.0.1",443))
    assert h._approved_addresses("x",443,time.monotonic()+1,lambda *a:[public])
    with pytest.raises(h.HttpGetError,match="http_get_blocked_address"):
        h._approved_addresses("x",443,time.monotonic()+1,lambda *a:[public,private])


def test_credentials_are_never_selected_mid_chain_or_for_other_port(monkeypatch):
    b=h.Credential("b-token",h.Origin("https","b.example",443),"Authorization","BSECRET")
    replies=[response(b"",302,[("Location","https://b.example/end")]),response()]
    sockets=[]
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",443))])
    def connect(*a,**k): s=FakeSocket(replies[len(sockets)]); sockets.append(s); return s
    monkeypatch.setattr(h,"_connect",connect)
    out=h.http_get("https://a.example/",max_bytes=1000,timeout_ms=1000,max_redirects=1,remaining_budget=1,credentials={b.origin:b})
    assert b"Authorization" not in sockets[1].sent and not json.loads(out)["credential_attached"]
    replies[:]=[response()]; sockets.clear()
    out=h.http_get("https://b.example:8443/",max_bytes=1000,timeout_ms=1000,max_redirects=0,remaining_budget=1,credentials={b.origin:b})
    assert b"Authorization" not in sockets[0].sent and not json.loads(out)["credential_attached"]


def test_redirect_hop_userinfo_is_a_bad_redirect_and_target_length_still_bounds(monkeypatch):
    """Redirect hop: the offending URL IS a `Location`, so it keeps that code."""
    for location,code in [("https://user@evil.example/","http_get_bad_redirect"),("/"+"x"*9000,"http_get_url_too_long")]:
        with pytest.raises(h.HttpGetError) as caught:
            run(monkeypatch,[response(b"",302,[("Location",location)])])
        assert caught.value.code==code

@pytest.mark.parametrize("secret", ['a"b', r'a\\b'], ids=["quote", "backslash"])
def test_reflected_credential_is_redacted_before_json_escaping(monkeypatch, secret):
    cred=h.Credential("credential-id",h.Origin("https","a.example",443),"Authorization",secret)
    replies=[response(secret.encode("utf-8"))]
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",443))])
    monkeypatch.setattr(h,"_connect",lambda *a,**k:FakeSocket(replies.pop(0)))
    out=h.http_get("https://a.example/",max_bytes=1000,timeout_ms=1000,max_redirects=0,remaining_budget=1,credentials={cred.origin:cred})
    assert json.loads(out)["body"] == "credential-id"
    assert secret not in out


class ReflectingSocket(FakeSocket):
    """A debug endpoint that echoes the request headers back as the body.

    §11 names a reflector for the redaction controls because it is the shape
    that puts the credential in the response without the test writing it there
    — the tool's own `Authorization` line comes back at it.
    """
    def __init__(self, transform=lambda sent: sent):
        super().__init__(b""); self.transform=transform
    def sendall(self, value):
        super().sendall(value)
        self.response=bytearray(response(self.transform(value)))


def _reflector(monkeypatch, sock):
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",443))])
    monkeypatch.setattr(h,"_connect",lambda *a,**k:sock)


def test_a_reflected_credential_is_absent_from_the_result_AND_the_audit(monkeypatch):
    """§11 requires BOTH halves, asserted separately.

    A control reading only the tool result leaves the audit half unpinned: the
    audit fields are built from the same `Credential` object and nothing in the
    result stops them carrying its `value` instead of its `id`. The result
    assertion above passes an implementation whose audit line ships the secret
    to disk, which is the half that is written down forever.
    """
    cred=h.Credential("credential-id",h.Origin("https","a.example",443),"Authorization","Bearer s3cr3t-value")
    sock=ReflectingSocket()
    _reflector(monkeypatch,sock)
    out=h.http_get("https://a.example/",max_bytes=2000,timeout_ms=1000,max_redirects=0,remaining_budget=1,credentials={cred.origin:cred})
    # Premise: the reflector really did see the secret and echo it back.
    assert cred.value.encode() in sock.sent
    assert "credential-id" in json.loads(out)["body"]
    assert cred.value not in str(out)
    assert cred.value not in json.dumps(out.audit_fields)


def test_a_base64_reflector_is_NOT_defeated_by_literal_redaction(monkeypatch):
    """§11's documented negative, so a later reader does not mistake literal
    redaction for secrecy.

    `_redact` is a byte-for-byte replacement of the credential value. An
    endpoint that re-encodes what it echoes — base64 here, but URL-encoding,
    JSON escaping inside the body or a hash would do as well — returns the
    secret in a form the replacement cannot see, and this test asserts that it
    DOES. Redaction is a defence against accidental reflection, not a
    confidentiality guarantee against a host chosen to break it; the guarantee
    is §9.4's forwarding rules, which decide who gets the header at all.
    """
    cred=h.Credential("credential-id",h.Origin("https","a.example",443),"Authorization","Bearer s3cr3t-value")
    def b64_of_the_authorization_value(sent):
        line=[x for x in sent.split(b"\r\n") if x.lower().startswith(b"authorization:")][0]
        return base64.b64encode(line.split(b": ",1)[1])
    sock=ReflectingSocket(b64_of_the_authorization_value)
    _reflector(monkeypatch,sock)
    out=h.http_get("https://a.example/",max_bytes=2000,timeout_ms=1000,max_redirects=0,remaining_budget=1,credentials={cred.origin:cred})
    body=json.loads(out)["body"]
    assert cred.value.encode() in sock.sent, "premise: the endpoint was given the secret"
    assert cred.value not in str(out), "the literal value is still redacted wherever it appears"
    assert base64.b64decode(body)==cred.value.encode(), "re-encoded reflection is NOT claimed to be defeated"


def _error_echoing_the_credential(monkeypatch, secret):
    """A hostile origin echoes `secret` back as a `Content-Encoding`.

    That header is rendered into `http_get_unsupported_encoding`'s message
    verbatim, which is how attacker-chosen bytes reach the model through an
    ERROR rather than through a body — the path `_redact` covers on the result
    and which had no control at all.
    """
    cred=h.Credential("credential-id",h.Origin("https","a.example",443),"Authorization",secret)
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",443))])
    sock=FakeSocket(response(b"ok",200,[("Content-Encoding",secret)]))
    monkeypatch.setattr(h,"_connect",lambda *a,**k:sock)
    with pytest.raises(h.HttpGetError) as caught:
        h.http_get("https://a.example/",max_bytes=1000,timeout_ms=1000,max_redirects=0,
                   remaining_budget=1,credentials={cred.origin:cred})
    assert caught.value.code=="http_get_unsupported_encoding"
    assert secret.encode() in sock.sent, "premise: the credential really was sent"
    return cred, str(caught.value)


def test_an_error_message_echoing_the_credential_is_redacted(monkeypatch):
    """The missing control: deleting `_redact` from the error path left the
    suite green, so nothing said error messages are scrubbed at all.

    The secret here is already lower-case, so `_encoding`'s fold is a no-op and
    the case-insensitivity of the match is not what is under test — this row
    fails exactly one wrong implementation, the one that hands `str(e)` to the
    model unscrubbed.
    """
    cred,message=_error_echoing_the_credential(monkeypatch,"bearer s3cr3t-lowercase")
    assert cred.value not in message
    assert cred.id in message, "the id names which credential leaked, per the result-path convention"


def test_error_redaction_survives_the_case_fold_yugo_itself_applies(monkeypatch):
    """`_encoding` lowercases the header value before rendering it, so an
    exact-byte `str.replace` no longer matched and a mixed-case credential
    reached the model case-folded:

        http_get_unsupported_encoding: Content-Encoding 'bearer supersecret' is unsupported

    The fold is yugo's own transformation, inside the text whose redaction
    `_redact` owns, so the match is case-insensitive. This row fails an
    implementation whose `_redact` is `str.replace`, which the row above does
    not — the two are not interchangeable.
    """
    cred,message=_error_echoing_the_credential(monkeypatch,"Bearer S3cr3t-MixedCase")
    assert cred.value not in message
    assert cred.value.lower() not in message.lower(), "a case-folded echo is still the credential"
    assert cred.id in message


def test_oversized_declared_chunk_is_charged_while_streaming(monkeypatch):
    raw=(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nTransfer-Encoding: chunked\r\n\r\n"
         b"100000000\r\n" + b"a"*200)
    sock=GradualSocket(raw)
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",80))])
    monkeypatch.setattr(h,"_connect",lambda *a,**k:sock)
    with pytest.raises(h.HttpGetError,match="wire bytes"):
        h.http_get("http://public.example",max_bytes=100,timeout_ms=1000,max_redirects=0,remaining_budget=1,credentials={})
    assert len(sock.response) > 50


def test_unterminated_chunk_size_line_has_its_own_bound(monkeypatch):
    raw=(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nTransfer-Encoding: chunked\r\n\r\n"
         + b"1"*(h.MAX_CHUNK_LINE_BYTES+100))
    sock=GradualSocket(raw)
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",80))])
    monkeypatch.setattr(h,"_connect",lambda *a,**k:sock)
    with pytest.raises(h.HttpGetError,match="chunk-size line"):
        h.http_get("http://public.example",max_bytes=20000,timeout_ms=1000,max_redirects=0,remaining_budget=1,credentials={})


class ExplodingSocket(FakeSocket):
    def __init__(self, error): super().__init__(b""); self.error=error
    def sendall(self, value): raise self.error


@pytest.mark.parametrize(("error","code"),[
    (OSError("connection reset by peer"),"http_get_transport_error"),
    (ssl.SSLError("handshake failure"),"http_get_transport_error"),
    (UnicodeEncodeError("utf-8","x",0,1,"surrogates not allowed"),"http_get_transport_error"),
    (TimeoutError("timed out"),"http_get_timeout"),
],ids=["socket","tls","encoding","timeout-control"])
def test_unclassified_failures_never_borrow_bad_redirect(monkeypatch,error,code):
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",80))])
    monkeypatch.setattr(h,"_connect",lambda *a,**k:ExplodingSocket(error))
    with pytest.raises(h.HttpGetError) as caught:
        h.http_get("http://public.example",max_bytes=1000,timeout_ms=1000,max_redirects=0,remaining_budget=1,credentials={})
    assert caught.value.code==code


def test_result_encoder_failure_is_a_transport_error_not_a_bad_redirect(monkeypatch):
    """The §9.4.1 case for constraining `id`, reached by bypassing the loader."""
    cred=h.Credential("bad\ud800id",h.Origin("https","a.example",443),"Authorization","SECRET")
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,("93.184.216.34",443))])
    monkeypatch.setattr(h,"_connect",lambda *a,**k:FakeSocket(response()))
    with pytest.raises(h.HttpGetError) as caught:
        h.http_get("https://a.example/",max_bytes=1000,timeout_ms=1000,max_redirects=0,remaining_budget=1,credentials={cred.origin:cred})
    assert caught.value.code=="http_get_transport_error"


class RefusingSocket:
    def __init__(self, error): self.error=error
    def settimeout(self, value): pass
    def connect(self, address): raise self.error
    def close(self): pass


class FailingContext:
    def __init__(self, error): self.error=error
    def wrap_socket(self, raw, server_hostname): raise self.error


@pytest.mark.parametrize(("error","code"),[
    (ConnectionRefusedError("connection refused"),"http_get_transport_error"),
    (OSError("network is unreachable"),"http_get_transport_error"),
    (TimeoutError("timed out"),"http_get_timeout"),
],ids=["refused","unreachable","timeout-control"])
def test_connect_failure_to_an_approved_address_is_not_an_ssrf_accusation(monkeypatch,error,code):
    """The address already passed the §9 check, so it cannot be `blocked_address`."""
    monkeypatch.setattr(socket,"socket",lambda *a:RefusingSocket(error))
    with pytest.raises(h.HttpGetError) as caught:
        h._connect(h.Origin("https","api.example.com",443),time.monotonic()+1,[(socket.AF_INET,socket.SOCK_STREAM,6,("93.184.216.34",443))],FailingContext(ssl.SSLError("unused")))
    assert caught.value.code==code


def test_tls_handshake_failure_is_a_transport_error(monkeypatch):
    class Raw:
        def settimeout(self,value): pass
        def connect(self,address): pass
        def close(self): pass
    monkeypatch.setattr(socket,"socket",lambda *a:Raw())
    with pytest.raises(h.HttpGetError) as caught:
        h._connect(h.Origin("https","api.example.com",443),time.monotonic()+1,[(socket.AF_INET,socket.SOCK_STREAM,6,("93.184.216.34",443))],FailingContext(ssl.SSLError("handshake failure")))
    assert caught.value.code=="http_get_transport_error"


def _raising_resolver(error):
    def resolve(*args): raise error
    return resolve


def test_all_filtered_answer_is_blocked_but_no_answer_at_all_is_transport():
    """The §9 boundary: the check refused every answer, versus never getting one.

    An empty answer, a `gaierror` and an unparseable answer all sit on the same
    side — in none of them did the check have an address to evaluate. Collapsing
    the two codes fails exactly one of these halves, whichever way it is
    collapsed; the pairing is the whole point of the split.
    """
    private=lambda *a:[(socket.AF_INET,socket.SOCK_STREAM,0,"",("127.0.0.1",443)),(socket.AF_INET,socket.SOCK_STREAM,0,"",("10.0.0.5",443))]
    with pytest.raises(h.HttpGetError) as caught:
        h._approved_addresses("x",443,time.monotonic()+1,private)
    assert caught.value.code=="http_get_blocked_address"
    unparseable=lambda *a:[(socket.AF_INET,socket.SOCK_STREAM,0,"",("not-an-address",443))]
    for resolver in [lambda *a:[], _raising_resolver(socket.gaierror(-2,"Name or service not known")), unparseable]:
        with pytest.raises(h.HttpGetError) as caught:
            h._approved_addresses("x",443,time.monotonic()+1,resolver)
        assert caught.value.code=="http_get_transport_error"


@pytest.mark.parametrize(("url","expected_host"),[
    ("http://[2606:4700:4700::1111]/x",      b"Host: [2606:4700:4700::1111]\r\n"),
    ("http://[2606:4700:4700::1111]:8080/x", b"Host: [2606:4700:4700::1111]:8080\r\n"),
    ("http://public.example/x",              b"Host: public.example\r\n"),
    ("http://public.example:8080/x",         b"Host: public.example:8080\r\n"),
],ids=["v6-default-port","v6-explicit-port","name-default-port","name-explicit-port"])
def test_an_ipv6_literal_reaches_the_wire_bracketed(monkeypatch,url,expected_host):
    """RFC 3986/9110: an IPv6 literal in a Host field must be bracketed.

    The normalised URL bracketed and the Host header did not, because the two
    built the authority separately. Unbracketed, `2606:4700:4700::1111:8080`
    gives no way to tell where the address ends and the port begins, and the
    header disagrees with the URL reported to the model.

    The name rows are the paired positive: bracketing must not leak onto a
    hostname, and the default port must still be elided. An implementation that
    brackets everything, or one that brackets nothing, fails this parametrize.
    """
    sent={}
    def connect(*a,**k):
        s=FakeSocket(response(b"ok"))
        sent["sock"]=s
        return s
    monkeypatch.setattr(h,"_approved_addresses",lambda *a,**k:[(socket.AF_INET6,socket.SOCK_STREAM,0,("2606:4700:4700::1111",80,0,0))])
    monkeypatch.setattr(h,"_connect",connect)
    out=h.http_get(url,max_bytes=1000,timeout_ms=1000,max_redirects=0,remaining_budget=1,credentials={})
    assert expected_host in sent["sock"].sent, sent["sock"].sent.split(b"\r\n")[:3]
    # The Host line and the URL handed back to the model must agree about the
    # authority — that disagreement is what made this a defect rather than a
    # cosmetic issue.
    assert json.loads(out)["url"].startswith(url.rsplit("/x",1)[0])


def test_the_authority_is_built_in_one_place(monkeypatch):
    """Both call sites go through `_authority`, so they cannot drift apart.

    This is the actual regression guard: the bug was not a missing bracket, it
    was two independent implementations of the same decision.
    """
    assert h._authority("http","2606:4700:4700::1111",80)   == "[2606:4700:4700::1111]"
    assert h._authority("http","2606:4700:4700::1111",8080) == "[2606:4700:4700::1111]:8080"
    assert h._authority("https","2606:4700:4700::1111",443) == "[2606:4700:4700::1111]"
    assert h._authority("http","public.example",80)         == "public.example"
    assert h._authority("https","public.example",443)       == "public.example"
    assert h._authority("https","public.example",8443)      == "public.example:8443"
    # rendered() deliberately does NOT elide the port — it is an audit value,
    # and "which origin did the credential reach" is the question a dropped
    # default port makes ambiguous. It brackets for the same reason though.
    assert h.Origin("http","2606:4700:4700::1111",80).rendered() == "http://[2606:4700:4700::1111]:80"
    assert h.Origin("https","api.github.com",443).rendered()     == "https://api.github.com:443"
