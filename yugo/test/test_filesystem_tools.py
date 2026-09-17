"""SPEC §9.2/§9.3: confinement must survive hostile path components."""

import asyncio
import errno
import os
import stat
import time

import pytest

import filesystem_tools as fs
import tools
from litellm.types.utils import ChatCompletionMessageToolCall, Function


def _workspace(tmp_path):
    return fs.Workspace(str(tmp_path / "workspace"))


def test_legitimate_nested_read_write_and_modes(tmp_path):
    workspace = _workspace(tmp_path)
    assert workspace.write_file("notes/todo.md", "hello", 5) == (
        "wrote 5 bytes to notes/todo.md"
    )
    assert workspace.read_file("notes/todo.md", 5) == "hello"
    root = tmp_path / "workspace"
    assert stat.S_IMODE((root / "notes").stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "notes/todo.md").stat().st_mode) == 0o600


@pytest.mark.parametrize("path", ["/etc/passwd", "../escape", "safe/../escape"])
def test_absolute_and_parent_paths_are_named_errors(tmp_path, path):
    workspace = _workspace(tmp_path)
    with pytest.raises(fs.ToolPathError, match="absolute|forbidden.*component"):
        workspace.read_file(path, 100)


def test_symlinked_final_component_is_refused(tmp_path):
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("secret")
    (tmp_path / "workspace/link").symlink_to(outside)
    with pytest.raises(fs.ToolPathError, match="refused"):
        workspace.read_file("link", 100)


def test_intermediate_symlink_swap_is_refused_and_rejected_design_leaks(tmp_path):
    workspace = _workspace(tmp_path)
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (root / "safe").mkdir()
    (root / "safe").rmdir()
    (root / "safe").symlink_to(outside, target_is_directory=True)

    # CONTROL: final-component O_NOFOLLOW follows the swapped parent and leaks.
    fd = os.open(root / "safe/secret.txt", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        assert os.read(fd, 100) == b"secret"
    finally:
        os.close(fd)

    with pytest.raises(fs.ToolPathError, match="refused"):
        workspace.read_file("safe/secret.txt", 100)


def test_parent_creation_never_calls_plain_makedirs_after_startup(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)

    def rejected(*args, **kwargs):
        raise AssertionError("plain os.makedirs reintroduced the parent race")

    monkeypatch.setattr(fs.os, "makedirs", rejected)
    workspace.write_file("one/two/file.txt", "ok", 2)
    assert workspace.read_file("one/two/file.txt", 2) == "ok"


def test_parent_creation_refuses_a_symlink_escape(tmp_path):
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "workspace/link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(fs.ToolPathError, match="parent path"):
        workspace.write_file("link/child/file.txt", "no", 2)
    assert not (outside / "child").exists()


def test_fifo_reads_and_writes_refuse_without_blocking(tmp_path):
    workspace = _workspace(tmp_path)
    fifo = tmp_path / "workspace/pipe"
    os.mkfifo(fifo)
    started = time.monotonic()
    with pytest.raises(fs.ToolPathError, match="not a regular file"):
        workspace.read_file("pipe", 10)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(fs.ToolPathError, match="not a regular file"):
            workspace.write_file("pipe", "x", 10)
    finally:
        os.close(reader)
    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize("operation", ["read", "write"])
def test_directory_targets_are_refused_as_non_regular(tmp_path, operation):
    workspace = _workspace(tmp_path)
    (tmp_path / "workspace/directory").mkdir()
    with pytest.raises(fs.ToolPathError, match="regular file|refused"):
        if operation == "read":
            workspace.read_file("directory", 10)
        else:
            workspace.write_file("directory", "x", 10)


def test_read_bound_accepts_exact_and_rejects_one_over(tmp_path):
    workspace = _workspace(tmp_path)
    path = tmp_path / "workspace/file"
    path.write_bytes(b"abcd")
    assert workspace.read_file("file", 4) == "abcd"
    path.write_bytes(b"abcde")
    with pytest.raises(fs.ToolPathError, match="max_bytes 4.*size 5"):
        workspace.read_file("file", 4)


def test_write_bound_accepts_exact_utf8_bytes_and_rejects_one_over(tmp_path):
    workspace = _workspace(tmp_path)
    workspace.write_file("file", "é", 2)
    with pytest.raises(fs.ToolPathError, match="max_bytes 1.*size 2"):
        workspace.write_file("file", "é", 1)


def test_non_utf8_file_is_named_error(tmp_path):
    workspace = _workspace(tmp_path)
    (tmp_path / "workspace/file").write_bytes(b"\xff")
    with pytest.raises(fs.ToolPathError, match="not valid UTF-8"):
        workspace.read_file("file", 10)


def test_list_dir_is_byte_sorted_and_marks_entry_types(tmp_path):
    workspace = _workspace(tmp_path)
    root = tmp_path / "workspace"
    (root / "z-file").write_text("")
    (root / "a-dir").mkdir()
    (root / "m-link").symlink_to(root / "z-file")
    assert workspace.list_dir(".", 3) == (
        "# 3 listed, 0 omitted, 3 total\na-dir/\nm-link@\nz-file"
    )


def test_list_dir_bound_accepts_exact_and_reports_truncation(tmp_path):
    workspace = _workspace(tmp_path)
    root = tmp_path / "workspace"
    (root / "a").touch()
    (root / "b").touch()
    assert workspace.list_dir(".", 2) == "# 2 listed, 0 omitted, 2 total\na\nb"
    clipped = workspace.list_dir(".", 1)
    assert clipped == (
        "# 1 listed, 0 omitted, 2 total (truncated at max_entries=1)\na"
    )


@pytest.mark.parametrize("path", ["missing", "file"])
def test_list_dir_missing_or_file_is_named_error(tmp_path, path):
    workspace = _workspace(tmp_path)
    if path == "file":
        (tmp_path / "workspace/file").touch()
    with pytest.raises(fs.ToolPathError, match="refused"):
        workspace.list_dir(path, 10)


def test_list_dir_omits_unrepresentable_names_but_lists_good_entries(tmp_path):
    workspace = _workspace(tmp_path)
    raw_root = os.fsencode(tmp_path / "workspace")
    fd = os.open(raw_root + b"/bad-\xff", os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    (tmp_path / "workspace/good").touch()
    (tmp_path / "workspace/bad\nname").touch()
    result = workspace.list_dir(".", 10)
    assert result == "# 1 listed, 2 omitted, 3 total\ngood"
    lines = result.splitlines()
    listed = int(lines[0].split()[1])
    assert listed == len(lines) - 1


def test_list_dir_empty_directory_still_has_header(tmp_path):
    workspace = _workspace(tmp_path)
    assert workspace.list_dir(".", 10) == "# 0 listed, 0 omitted, 0 total"


@pytest.mark.parametrize("bad", ["line\nbreak", "carriage\rreturn", "tab\tname", "del\x7fname"])
@pytest.mark.parametrize("operation", ["read", "write", "list"])
def test_every_filesystem_tool_refuses_control_characters_by_name(
    tmp_path, bad, operation
):
    workspace = _workspace(tmp_path)
    with pytest.raises(fs.ToolPathError, match="control character U\\+"):
        if operation == "read":
            workspace.read_file(bad, 10)
        elif operation == "write":
            workspace.write_file(bad, "x", 10)
        else:
            workspace.list_dir(bad, 10)


def test_list_dir_scan_ceiling_is_named(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)

    class Entry:
        name = "ordinary"

        def is_dir(self, *, follow_symlinks):
            return False

        def is_file(self, *, follow_symlinks):
            return True

    class Scan:
        def __enter__(self):
            return iter(Entry() for _ in range(100001))

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(fs.os, "scandir", lambda fd: Scan())
    with pytest.raises(fs.ToolPathError, match="scan ceiling 100000"):
        workspace.list_dir(".", 10)


def test_unknown_architecture_is_a_named_startup_failure():
    with pytest.raises(fs.FilesystemStartupError, match="unsupported architecture.*mystery"):
        fs._syscall_number("mystery")


def test_enosys_probe_is_a_named_startup_failure(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError(errno.ENOSYS, "Function not implemented")

    monkeypatch.setattr(fs, "_openat2", unavailable)
    with pytest.raises(fs.FilesystemStartupError, match="openat2 unavailable"):
        fs.Workspace(str(tmp_path / "workspace"))


def test_workspace_is_created_0700_and_non_directory_is_fatal(tmp_path):
    path = tmp_path / "new/workspace"
    workspace = fs.Workspace(str(path))
    assert path.is_dir()
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    workspace.close()
    bad = tmp_path / "file"
    bad.touch()
    with pytest.raises(fs.FilesystemStartupError, match="workspace path"):
        fs.Workspace(str(bad))


@pytest.mark.parametrize(
    ("tool_name", "key", "value", "named"),
    [
        ("read_file", "max_bytes", "true", "max_bytes"),
        ("read_file", "max_bytes", "1.5", "max_bytes"),
        ("read_file", "max_bytes", "0", "max_bytes"),
        ("read_file", "max_bytes", str(16 * 1024 * 1024 + 1), "max_bytes"),
        ("write_file", "max_bytes", str(4 * 1024 * 1024 + 1), "max_bytes"),
        ("list_dir", "max_entries", "10001", "max_entries"),
    ],
)
def test_invalid_tool_bounds_abort_startup_naming_key(
    tmp_path, monkeypatch, tool_name, key, value, named
):
    grant = tmp_path / "tools.yaml"
    grant.write_text(f"version: 1\ntools:\n  {tool_name}:\n    {key}: {value}\n")
    grant.chmod(0o644)
    monkeypatch.setenv("YUGO_WORKSPACE_PATH", str(tmp_path / "workspace"))
    with pytest.raises(tools.ToolDeclarationError, match=named):
        tools.resolve_declared(grant)


def test_config_boundaries_and_handlers_are_bound(tmp_path, monkeypatch):
    grant = tmp_path / "tools.yaml"
    grant.write_text(
        "version: 1\ntools:\n"
        "  read_file: {max_bytes: 16777216}\n"
        "  write_file: {max_bytes: 4194304}\n"
        "  list_dir: {max_entries: 10000}\n"
    )
    grant.chmod(0o644)
    monkeypatch.setenv("YUGO_WORKSPACE_PATH", str(tmp_path / "workspace"))
    selected = tools.resolve_declared(grant)
    assert set(selected) == {"read_file", "write_file", "list_dir"}
    assert all(tool.handler is not tools._filesystem_unbound for tool in selected.values())


@pytest.mark.asyncio
async def test_granted_handlers_share_the_confined_workspace(tmp_path, monkeypatch):
    grant = tmp_path / "tools.yaml"
    grant.write_text(
        "version: 1\ntools:\n"
        "  read_file: {max_bytes: 2}\n"
        "  write_file: {max_bytes: 2}\n"
        "  list_dir: {max_entries: 1}\n"
    )
    grant.chmod(0o644)
    monkeypatch.setenv("YUGO_WORKSPACE_PATH", str(tmp_path / "workspace"))
    selected = tools.resolve_declared(grant)
    assert await selected["write_file"].handler({"path": "file", "content": "ok"}) == (
        "wrote 2 bytes to file"
    )
    assert await selected["read_file"].handler({"path": "file"}) == "ok"
    assert await selected["list_dir"].handler({"path": "."}) == (
        "# 1 listed, 0 omitted, 1 total\nfile"
    )


def test_chat_only_grant_does_not_initialize_workspace(tmp_path, monkeypatch):
    grant = tmp_path / "tools.yaml"
    grant.write_text("version: 1\ntools:\n  loop_probe: null\n")
    grant.chmod(0o644)

    def unavailable(path):
        raise AssertionError("chat-only startup probed openat2")

    monkeypatch.setattr(tools.filesystem_tools, "Workspace", unavailable)
    assert list(tools.resolve_declared(grant)) == ["loop_probe"]


def test_filesystem_grant_fails_closed_when_openat2_is_unavailable(tmp_path, monkeypatch):
    grant = tmp_path / "tools.yaml"
    grant.write_text("version: 1\ntools:\n  read_file: null\n")
    grant.chmod(0o644)

    def unavailable(path):
        raise fs.FilesystemStartupError("openat2 unavailable: ENOSYS")

    monkeypatch.setattr(tools.filesystem_tools, "Workspace", unavailable)
    with pytest.raises(tools.ToolDeclarationError, match="openat2 unavailable"):
        tools.resolve_declared(grant)


def test_default_protected_paths_are_outside_default_workspace():
    tools._validate_protected_paths(
        "/var/lib/yugo/workspace/yugo",
        {
            "YUGO_TOOL_AUDIT_PATH": "/var/lib/yugo/tool-audit.jsonl",
            "YUGO_TOOLS_FILE": "/etc/yugo/tools.yaml",
        },
    )


@pytest.mark.parametrize(
    ("workspace", "label", "protected"),
    [
        ("/srv/work", "YUGO_TOOL_AUDIT_PATH", "/srv/work"),
        ("/srv/work", "YUGO_TOOL_AUDIT_PATH", "/srv/work/log/audit.jsonl"),
        ("/srv/work", "YUGO_TOOLS_FILE", "/srv/work/config/tools.yaml"),
    ],
)
def test_protected_path_equal_or_below_workspace_aborts_naming_path(
    workspace, label, protected
):
    with pytest.raises(tools.ToolDeclarationError, match=label):
        tools._validate_protected_paths(workspace, {label: protected})


def test_protected_sibling_path_is_the_rejection_control():
    tools._validate_protected_paths(
        "/srv/workspace", {"YUGO_TOOL_AUDIT_PATH": "/srv/audit/tool.jsonl"}
    )


def test_explicit_audit_env_below_workspace_aborts_filesystem_startup(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    grant = tmp_path / "tools.yaml"
    grant.write_text("version: 1\ntools:\n  read_file: null\n")
    grant.chmod(0o644)
    monkeypatch.setenv("YUGO_WORKSPACE_PATH", str(workspace))
    monkeypatch.setenv("YUGO_TOOL_AUDIT_PATH", str(workspace / "audit.jsonl"))
    with pytest.raises(tools.ToolDeclarationError, match="YUGO_TOOL_AUDIT_PATH"):
        tools.resolve_declared(grant)


def test_grant_file_below_workspace_aborts_filesystem_startup(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    grant = workspace / "tools.yaml"
    grant.write_text("version: 1\ntools:\n  read_file: null\n")
    grant.chmod(0o644)
    monkeypatch.setenv("YUGO_WORKSPACE_PATH", str(workspace))
    monkeypatch.setenv("YUGO_TOOL_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    with pytest.raises(tools.ToolDeclarationError, match="YUGO_TOOLS_FILE"):
        tools.resolve_declared(grant)


@pytest.mark.asyncio
async def test_blocking_filesystem_call_yields_and_is_timed_out_and_audited(tmp_path):
    class BlockingWorkspace:
        def read_file(self, path, max_bytes):
            time.sleep(0.2)
            return "too late"

    handler = tools.partial(
        tools._read_file, workspace=BlockingWorkspace(), max_bytes=10
    )
    selected = {
        "read_file": tools.replace(tools.REGISTRY["read_file"], handler=handler)
    }
    call = ChatCompletionMessageToolCall(
        id="call_blocking",
        type="function",
        function=Function(name="read_file", arguments='{"path":"file"}'),
    )
    audit_path = tmp_path / "audit.jsonl"
    audit = tools.ToolAuditLog(str(audit_path))
    loop_ticked = asyncio.Event()

    async def ticker():
        await asyncio.sleep(0.01)
        loop_ticked.set()

    tick_task = asyncio.create_task(ticker())
    started = time.monotonic()
    result = await tools.run_tool_call(
        call,
        selected=selected,
        audit=audit,
        thread_id="test",
        round_index=0,
        timeout=0.03,
    )
    await tick_task
    assert loop_ticked.is_set()
    assert time.monotonic() - started < 0.15
    assert "timed out" in result["content"]
    line = audit_path.read_text()
    assert '"ok":false' in line
    assert '"read_file"' in line
