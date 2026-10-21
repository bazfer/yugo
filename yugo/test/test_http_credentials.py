import ast
import os
import pathlib
import pytest
import yaml
import http_tools as h

GOOD='''version: 1\ncredentials:\n  - id: github-review\n    host: api.github.com\n    header: Authorization\n    value: Bearer_secret\n'''

def load(tmp_path,text=GOOD,mode=0o600):
 p=tmp_path/'creds.yaml'; p.write_text(text); p.chmod(mode); return h.load_credentials(p)

def test_positive_authority_grammar(tmp_path):
 # The label-anchoring controls: SPEC 9 blocks whole trailing labels, so a host
 # merely CONTAINING a blocked name parses. `printer.notlocal`, `www.contest`
 # and `subhome.arpa` are the discriminating ones — their final label ENDS with
 # a blocked name, which a dotless `endswith` cannot tell from being it.
 for host in ['api.github.com','a.example.com','my-api.example.com','api.github.com:443','mylocal.example.com','test.example.com','onion.example.com','printer.notlocal','www.contest','subhome.arpa']:
  assert h.parse_authority(host).host
 assert len(load(tmp_path))==1

@pytest.mark.parametrize(("host","named"),[("api.github.com:","port"),("api.github.com:-1","port"),("api.github.com:0","1..65535"),("api.github.com:99999","1..65535"),("api.github.com:0443","leading-zero"),("10.0.0.1","IP literal"),("[::1]","IP literal"),("printer.local","blocked name"),("box.internal","blocked name"),("box.localhost","blocked name"),("nas.home","blocked name"),("printer.lan","blocked name"),("router.home.arpa","blocked name"),("y.test","blocked name"),("nope.invalid","blocked name"),("x.onion","blocked name"),("X.ONION","blocked name")])
def test_authority_rejections(host,named):
 """SPEC 9's blocked set is exhaustive for hostnames and the startup path must
 carry all of it: `home.arpa`, `test`, `invalid` and `onion` were named by §9
 and absent from the code, so a credential could be bound to `x.onion`."""
 with pytest.raises(h.HttpStartupError,match=named): h.parse_authority(host)

@pytest.mark.parametrize(("text","named"),[
 ('version: true\ncredentials: []\n','version'),
 (GOOD.replace('header: Authorization','header: X-Bad Name'),'header'),
 (GOOD.replace('value: Bearer_secret','value: true'),'value'),
 (GOOD.replace('value: Bearer_secret','value: "bad\\r\\nvalue"'),'value'),
 (GOOD.replace('    value: Bearer_secret','    value: one\n    value: two'),'duplicate'),
 (GOOD.replace('credentials:','extra: 1\ncredentials:'),'extra'),
 (GOOD.replace('    value: Bearer_secret','    value: Bearer_secret\n    fifth: nope'),'exactly'),
 ('''version: 1\ncredentials:\n - &x {id: a, host: a.example.com, header: X, value: v}\n - *x\n''','alias'),
])
def test_grammar_failures_name_key(tmp_path,text,named):
 with pytest.raises(h.HttpStartupError,match=named): load(tmp_path,text)

def test_duplicate_normalized_origin(tmp_path):
 text=GOOD+'''  - id: second\n    host: api.github.com:443\n    header: X-Token\n    value: second_secret\n'''
 with pytest.raises(h.HttpStartupError,match='duplicate normalized origin'): load(tmp_path,text)

def test_permissions_and_absent_control(tmp_path):
 assert h.load_credentials(tmp_path/'absent')=={}
 with pytest.raises(h.HttpStartupError,match='permission'): load(tmp_path,mode=0o640)


def test_credential_value_wire_repertoire_has_reject_and_boundary_controls(tmp_path):
    assert load(tmp_path, GOOD.replace('Bearer_secret', 'Bearer !~'))
    with pytest.raises(h.HttpStartupError, match='ASCII HTTP field-value repertoire'):
        load(tmp_path, GOOD.replace('Bearer_secret', 'Bearer café'))


def test_credential_id_wire_repertoire_has_reject_and_boundary_controls(tmp_path):
    boundary=load(tmp_path, GOOD.replace('id: github-review','id: "!~ \\tboundary"'))
    assert [c.id for c in boundary.values()]==['!~ \tboundary']
    for bad in ['id: café','id: "bad\\uD800id"','id: " github-review"','id: "github-review\\t"']:
        with pytest.raises(h.HttpStartupError,match='credential id key'):
            load(tmp_path, GOOD.replace('id: github-review',bad))


def test_credential_value_edge_whitespace_shares_the_id_predicate(tmp_path):
    """One predicate for both fields; this pins the `value` half of it."""
    for bad in ['value: " Bearer_secret"','value: "Bearer_secret "']:
        with pytest.raises(h.HttpStartupError,match='credential value key'):
            load(tmp_path, GOOD.replace('value: Bearer_secret',bad))


def test_yaml_merge_key_is_refused_by_its_own_name(tmp_path):
    """§11 names this control, and it is the only item on that list with no test.

    Assert the NAMED error rather than merely that startup failed: the guard is
    defence-in-depth, and an anchored merge trips the alias check anyway, so a
    test that only asserts a refusal passes with the merge guard deleted.
    """
    anchored=('version: 1\ncredentials:\n'
              '  - &base {id: a, host: a.example.com, header: X, value: v}\n'
              '  - <<: *base\n    id: b\n')
    with pytest.raises(h.HttpStartupError,match="merge key"): load(tmp_path,anchored)
    # The loader half of the guard, reached directly because composition refuses
    # the document before construction ever runs.
    with pytest.raises(h.HttpStartupError,match="merge key"):
        yaml.load(anchored,Loader=h._SafeUniqueLoader)


def test_duplicate_credential_id_is_refused_across_distinct_origins(tmp_path):
    """Two entries, two origins, one id. Nothing else rejects this: the origin
    map is keyed by origin, so both entries load and `credential_id` stops
    naming one credential."""
    text=GOOD+'  - id: github-review\n    host: other.example.com\n    header: X-Token\n    value: other_secret\n'
    with pytest.raises(h.HttpStartupError,match='duplicate credential id'): load(tmp_path,text)
    ok=GOOD+'  - id: second\n    host: other.example.com\n    header: X-Token\n    value: other_secret\n'
    assert len(load(tmp_path,ok))==2


def test_credential_raise_sites_use_only_safe_diagnostics():
    """All raise sites in credential-loading functions must use CredentialConfigError
    with a string literal as the first argument — no f-strings, no !r, no parsed values.

    This is the class-level structural guard. The canary tests below prove specific
    known paths. This test makes it impossible to add a new leak silently:
    any raise that does NOT use CredentialConfigError('literal_code', ...) fails here
    before CI even runs the canaries.

    Acceptance criterion (Myc's mutation): add `entries!r` to any raise inside a
    credential-loading function and this test goes red.
    """
    src = pathlib.Path(__file__).parent.parent / "http_tools.py"
    tree = ast.parse(src.read_text())

    CRED_FUNCS = {"load_credentials", "_mapping", "_reject_graph", "_parse_yaml"}
    violations: list[str] = []

    for node in ast.walk(tree):
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in CRED_FUNCS):
            continue
        for child in ast.walk(node):
            if not (isinstance(child, ast.Raise) and child.exc is not None):
                continue
            exc = child.exc
            # Only CredentialConfigError(...) is permitted.
            if not (
                isinstance(exc, ast.Call)
                and isinstance(exc.func, ast.Name)
                and exc.func.id == "CredentialConfigError"
            ):
                violations.append(
                    f"  line {child.lineno}: raise does not use CredentialConfigError — {ast.dump(exc)[:80]}"
                )
                continue
            # First argument must be a string literal (no f-string, no variable).
            if not exc.args:
                violations.append(f"  line {child.lineno}: CredentialConfigError called with no code argument")
            elif not (isinstance(exc.args[0], ast.Constant) and isinstance(exc.args[0].value, str)):
                violations.append(
                    f"  line {child.lineno}: CredentialConfigError code is not a string literal — {ast.dump(exc.args[0])[:80]}"
                )
            # Keyword arguments must be named line/col/index only.
            for kw in exc.keywords:
                if kw.arg not in ("line", "col", "index"):
                    violations.append(
                        f"  line {child.lineno}: unexpected keyword {kw.arg!r} in CredentialConfigError"
                    )

    assert not violations, (
        "Credential-loading raise sites must use CredentialConfigError('literal_code', ...)\n"
        "with no f-strings, !r, or parsed values. Violations:\n" + "\n".join(violations)
    )


def test_malformed_yaml_alias_does_not_leak_secret_value(tmp_path):
    """PyYAML embeds the alias name verbatim in e.problem: 'found undefined alias <NAME>'.

    An alias that uses the secret as its name (e.g. *CANARY in a value field)
    leaks through e.problem even when str(e) / source lines are avoided.
    The fix must not render e.problem at all.
    """
    canary = "CANARY_SECRET_DO_NOT_LEAK"
    malformed = (
        f'version: 1\ncredentials:\n'
        f'  - id: github-review\n    host: api.github.com\n'
        f'    header: Authorization\n    value: *{canary}\n'  # undefined alias
    )
    with pytest.raises(h.HttpStartupError) as exc_info:
        load(tmp_path, malformed)
    assert canary not in str(exc_info.value), (
        f"Alias name leaked into error message: {exc_info.value}"
    )


def test_malformed_yaml_does_not_leak_secret_in_traceback(tmp_path):
    """raise ... from e retains the raw parser exception in __cause__.

    traceback.format_exception() renders the full chain, including the PyYAML
    error with its source-quoting str(). The fix uses `from None` to suppress
    the chain and prevent secrets surfacing in formatted tracebacks.
    """
    import traceback
    canary = "CANARY_SECRET_DO_NOT_LEAK"
    malformed = (
        f'version: 1\ncredentials:\n'
        f'  - id: github-review\n    host: api.github.com\n'
        f'    header: Authorization\n    value: "Bearer {canary}\n'
    )
    with pytest.raises(h.HttpStartupError) as exc_info:
        load(tmp_path, malformed)
    tb_text = "".join(traceback.format_exception(exc_info.value))
    assert canary not in tb_text, (
        f"Secret leaked into formatted traceback"
    )
    # `from None` sets __suppress_context__ = True. Python still mechanically sets
    # __context__ inside the except block, but the formatter won't render the chain.
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


def test_malformed_yaml_does_not_leak_secret_value(tmp_path):
    """A YAML parse error must never include the source line in its message.

    PyYAML's MarkedYAMLError quotes the offending source line verbatim. If the
    bad line contains a partially-written secret, that secret reaches whatever
    surfaces the exception — the model's context and from there a provider API.

    Assert the canary does NOT appear in the HttpStartupError message. The
    error may include the file path, line number, and error problem, but not
    the source text.
    """
    canary = "CANARY_SECRET_DO_NOT_LEAK"
    # Missing closing quote causes PyYAML to quote the secret-bearing line in
    # its error message. The error is real: try yaml.safe_load to confirm.
    malformed = (
        f'version: 1\ncredentials:\n'
        f'  - id: github-review\n    host: api.github.com\n'
        f'    header: Authorization\n    value: "Bearer {canary}\n'
    )
    with pytest.raises(h.HttpStartupError) as exc_info:
        load(tmp_path, malformed)
    assert canary not in str(exc_info.value), (
        f"Secret leaked into error message: {exc_info.value}"
    )


def test_duplicate_mapping_key_does_not_leak_secret(tmp_path):
    """_mapping interpolated the duplicate key verbatim: 'duplicate mapping key CANARY'.

    Parsed keys are user-controlled data. The fix uses only numeric node position.
    This canary independently fails when key!r is restored.
    """
    import traceback
    canary = "CANARY_SECRET_DO_NOT_LEAK"
    malformed = (
        f'version: 1\ncredentials:\n'
        f'  - id: github-review\n    host: api.github.com\n'
        f'    header: Authorization\n    value: one\n'
        f'    {canary}: extra\n'
        f'    {canary}: extra2\n'
    )
    with pytest.raises(h.HttpStartupError) as exc_info:
        load(tmp_path, malformed)
    assert canary not in str(exc_info.value)
    tb_text = "".join(traceback.format_exception(exc_info.value))
    assert canary not in tb_text
    # Direct raise inside _mapping (not in an except block) — no chain to suppress.
    assert exc_info.value.__context__ is None


def test_alias_in_value_does_not_leak_key_path(tmp_path):
    """_reject_graph built a path from parsed key values: 'alias at root.value.CANARY'.

    Parsed key names are user-controlled data. The fix uses only numeric node position.
    This canary independently fails when the path-building is restored.
    """
    import traceback
    canary = "CANARY_SECRET_DO_NOT_LEAK"
    # Anchor on a valid key, alias it as the value of a field named after the canary.
    # _reject_graph walks the composed node tree and previously embedded key.value in path.
    malformed = (
        f'version: 1\ncredentials:\n'
        f'  - &anchor id: github-review\n    host: api.github.com\n'
        f'    header: Authorization\n    value: v\n'
        f'    {canary}: *anchor\n'
    )
    with pytest.raises(h.HttpStartupError) as exc_info:
        load(tmp_path, malformed)
    assert canary not in str(exc_info.value)
    tb_text = "".join(traceback.format_exception(exc_info.value))
    assert canary not in tb_text
    # Direct raise inside _reject_graph (not in an except block) — no chain to suppress.
    assert exc_info.value.__context__ is None


def test_unhashable_key_does_not_leak_key_repr(tmp_path):
    """_mapping interpolated the unhashable key repr: 'unhashable mapping key [CANARY]'.

    The TypeError from `key in out` previously propagated the key repr via `from e`.
    The fix uses only numeric node position and suppresses the chain.
    This canary independently fails when key!r and from e are restored.
    """
    import traceback
    canary = "CANARY_SECRET_DO_NOT_LEAK"
    # A list key is unhashable. YAML allows it; Python dicts do not.
    malformed = (
        f'version: 1\ncredentials:\n'
        f'  - id: github-review\n    host: api.github.com\n'
        f'    header: Authorization\n    value: v\n'
        f'    ? [{canary}]\n    : extra\n'
    )
    with pytest.raises(h.HttpStartupError) as exc_info:
        load(tmp_path, malformed)
    assert canary not in str(exc_info.value)
    tb_text = "".join(traceback.format_exception(exc_info.value))
    assert canary not in tb_text
    assert exc_info.value.__suppress_context__ is True


@pytest.mark.parametrize("cid",['id: Bearer_secret','id: gh-Bearer_secret'],ids=["equal","contains"])
def test_credential_id_may_not_carry_its_own_value(tmp_path,cid):
    """`id` is returned to the model as `credential_id` and is written to the
    audit; an id built out of the secret leaks it through both, and redaction
    rewrites the value TO the id, which would leave the secret in place."""
    with pytest.raises(h.HttpStartupError,match='contains its value'):
        load(tmp_path,GOOD.replace('id: github-review',cid))
