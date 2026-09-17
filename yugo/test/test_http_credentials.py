import os
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


@pytest.mark.parametrize("cid",['id: Bearer_secret','id: gh-Bearer_secret'],ids=["equal","contains"])
def test_credential_id_may_not_carry_its_own_value(tmp_path,cid):
    """`id` is returned to the model as `credential_id` and is written to the
    audit; an id built out of the secret leaks it through both, and redaction
    rewrites the value TO the id, which would leave the secret in place."""
    with pytest.raises(h.HttpStartupError,match='contains its value'):
        load(tmp_path,GOOD.replace('id: github-review',cid))
