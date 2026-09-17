"""
Packaging + documented-deploy-path guards.

Not behavior tests — these watch the seam where the repo's shipped files and
the README's quick-start can drift apart silently. Every failure here is one an
operator hits on a clean clone (or a second bot on the same host), and none of
them are visible to the runtime suite.
"""

from pathlib import Path

import yaml

import bot

_ROOT = Path(__file__).resolve().parent.parent
_README = (_ROOT / "README.md").read_text()
_GITIGNORE = (_ROOT / ".gitignore").read_text().split()
_COMPOSE_TEXT = (_ROOT / "compose.example.yml").read_text()
_COMPOSE = yaml.safe_load(_COMPOSE_TEXT)
_DOCKERFILE = (_ROOT / "Dockerfile").read_text()
_ENTRYPOINT = (_ROOT / "entrypoint.sh").read_text()


# ---------- version ----------

def test_readme_status_names_the_version_bot_py_reports():
    """`YUGO_VERSION` is what the `status_heartbeat` payload carries, so it is
    the version an operator sees on the wire. The README stated v0.1 while
    bot.py reported 0.3a — bind the two so the next bump cannot repeat it.
    """
    assert f"**Current: v{bot.YUGO_VERSION}.**" in _README, (
        f"README's Status block must lead with `**Current: v{bot.YUGO_VERSION}.**` "
        "— bot.YUGO_VERSION is the source of truth for the running version"
    )


def test_readme_changelog_has_an_entry_for_the_running_version():
    """The existing test above binds README's Status line to YUGO_VERSION, and
    the two stayed in lockstep — both stale — for two whole slices: 4d shipped
    and 4e shipped while the wire kept announcing 0.4c, so an operator could
    not tell a deployment carrying the audit log from one without it.

    Agreement between two fields is not the same as either being right. This
    binds the version to the CHANGELOG, which is written when a slice actually
    ships, so a bump with no shipped slice and a shipped slice with no bump are
    both caught.
    """
    entry = f"**v{bot.YUGO_VERSION} (shipped)"
    assert entry in _README, (
        f"README has no changelog entry starting `{entry}` — either the slice "
        f"shipped without one, or YUGO_VERSION was bumped past what shipped"
    )


def test_readme_changelog_has_no_gaps_in_the_current_minor():
    """v0.4b and v0.4d shipped with no changelog entry at all; the list jumped
    0.4a -> 0.4c -> (nothing). A missing entry reads as a slice that never
    happened, which is how 4d's audit log became invisible in the release
    history it belongs to.
    """
    import re
    shipped = set(re.findall(r"^\*\*v(\d+\.\d+[a-z]?) \(shipped\)", _README, re.MULTILINE))
    current = bot.YUGO_VERSION
    minor, suffix = current[:-1], current[-1]
    if suffix.isalpha():
        expected = {f"{minor}{chr(c)}" for c in range(ord("a"), ord(suffix) + 1)}
        missing = sorted(expected - shipped)
        assert not missing, f"README changelog is missing shipped slices: {missing}"


# ---------- compose ----------


def test_dockerfile_packages_coordinator_entrypoint_and_dependencies():
    for path in ("coordinator.py", "coordinator_main.py", "coordinator_state.py", "version.py", "entrypoint.sh"):
        assert f"COPY yugo/{path} ." in _DOCKERFILE
    assert "COPY schema/envelope.v1.schema.json ./schema/envelope.v1.schema.json" in _DOCKERFILE
    assert "COPY conformance/envelope.v1.vectors.json ./conformance/envelope.v1.vectors.json" in _DOCKERFILE
    assert 'ENTRYPOINT ["/app/entrypoint.sh"]' in _DOCKERFILE


def test_shell_dispatcher_uses_canonical_mode_resolver():
    assert "from coordinator import resolve_mode" in _ENTRYPOINT
    assert 'case "$YUGO_MODE"' not in _ENTRYPOINT
    assert 'coordinator_main.py "$@"' in _ENTRYPOINT


def test_compose_coordinator_has_restart_network_token_state_and_stop_grace():
    service = _COMPOSE["services"]["coordinator"]
    assert service["profiles"] == ["coordinator"]
    assert service["restart"] == "unless-stopped"
    assert "fleet-bus-net" in service["networks"]
    assert service["stop_grace_period"] == "10s"
    assert service["environment"]["FLEET_BUS_TOKEN_FILE"] == "/run/secrets/coordinator-token"
    mounts = "\n".join(service["volumes"])
    assert "/run/secrets/coordinator-token:ro" in mounts
    assert ":/var/lib/yugo" in mounts


def test_readme_documents_opt_in_coordinator_prerequisites():
    assert "--profile coordinator" in _README
    for prerequisite in ("fleet-bus-net", "coordinator-token", "coordinator-state"):
        assert prerequisite in _README

def test_compose_container_name_is_parameterised_per_bot():
    """A hardcoded `container_name` means the second bot on a host fails to
    start with a name conflict, and the fix is an edit to a tracked file rather
    than that bot's own `.env`.
    """
    container_name = _COMPOSE["services"]["bot"]["container_name"]
    assert "${BOT_NAME" in container_name, (
        f"container_name is {container_name!r}; it must interpolate BOT_NAME so "
        "two bots on one host get distinct container names"
    )


def test_compose_container_name_default_survives_a_blank_bot_name():
    """`.env.example` ships `BOT_NAME=` blank (it doubles as CI's smoke-gate
    env-file). `${BOT_NAME-yugo}` would then interpolate to the empty string and
    Compose would reject the file; only the `:-` form treats blank as unset.
    """
    container_name = _COMPOSE["services"]["bot"]["container_name"]
    assert "${BOT_NAME:-" in container_name, (
        f"container_name is {container_name!r}; use the `:-` default form so a "
        "blank BOT_NAME falls back instead of producing an empty name"
    )


def test_compose_does_not_hand_bot_name_a_default_inside_the_container():
    """The `:-yugo` fallback is for the container NAME only. Repeating BOT_NAME
    under `environment:` would push that default into the container's env, and a
    bus-enabled bot with no BOT_NAME would then adopt the identity `yugo`
    silently instead of aborting at startup with the variable named
    (`fleet_bus.load_config_from_env`).
    """
    environment = _COMPOSE["services"]["bot"].get("environment") or {}
    keys = environment.keys() if isinstance(environment, dict) else [
        entry.split("=", 1)[0] for entry in environment
    ]
    assert "BOT_NAME" not in keys, (
        "BOT_NAME must reach the container through env_file only — an "
        "`environment:` entry would mask the missing-BOT_NAME startup abort"
    )


# ---------- documented quick start ----------

def test_quickstart_copies_every_file_the_clean_clone_lacks():
    """`docker compose up` on a fresh clone fails three ways: Compose does not
    read `*.example.yml`, `.env` does not exist, and the `./persona.md` bind
    mount would be materialised as a DIRECTORY (`IsADirectoryError` is an OSError
    that `bot.load_persona` deliberately does not fall back on). Each hole needs
    its copy step spelled out.
    """
    for step in (
        "cp .env.example .env",
        "cp compose.example.yml compose.yml",
        "cp IDENTITY.md persona.md",
    ):
        assert step in _README, f"quick start is missing `{step}`"


def test_repo_ships_the_example_compose_not_a_live_one():
    """The copy step above is only correct while `compose.yml` stays untracked
    and ignored. If someone commits a live `compose.yml`, `cp` starts clobbering
    a tracked file and the ignore entry hides the diff.
    """
    assert (_ROOT / "compose.example.yml").is_file()
    assert "compose.yml" in _GITIGNORE, (
        ".gitignore must list compose.yml — the quick start creates it as a "
        "per-host file"
    )


def test_readme_clone_url_points_at_this_repo():
    """The repo moved from the artifice-ia org to bazfer (see the CI workflow's
    runner swap). A stale clone URL is the first command in the quick start, so
    it fails before anything else can.
    """
    assert "github.com:bazfer/yugo2.git" in _README
    assert "artifice-ia/yugo" not in _README
