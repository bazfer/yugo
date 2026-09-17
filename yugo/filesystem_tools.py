"""Kernel-enforced workspace confinement for the native filesystem tools."""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import stat
from pathlib import PurePosixPath
from typing import Any

RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08
RESOLVE_FLAGS = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS

# These architectures currently share a number by Linux ABI definition. They
# remain explicit: an unknown architecture must not inherit a guessed syscall.
_OPENAT2_NR = {
    "x86_64": 437,
    "amd64": 437,
    "aarch64": 437,
    "arm64": 437,
}


class FilesystemStartupError(RuntimeError):
    """The workspace cannot provide the confinement promised to operators."""


class ToolPathError(ValueError):
    """A model-authored path cannot safely or truthfully be serviced."""


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.syscall.restype = ctypes.c_long


def _syscall_number(machine: str | None = None) -> int:
    name = (machine or platform.machine()).casefold()
    try:
        return _OPENAT2_NR[name]
    except KeyError as error:
        raise FilesystemStartupError(
            f"openat2 unavailable: unsupported architecture {name!r}"
        ) from error


def _openat2(dirfd: int, path: str, flags: int, mode: int = 0) -> int:
    number = _syscall_number()
    how = _OpenHow(flags=flags, mode=mode, resolve=RESOLVE_FLAGS)
    raw = os.fsencode(path)
    result = _LIBC.syscall(
        number,
        dirfd,
        ctypes.c_char_p(raw),
        ctypes.byref(how),
        ctypes.sizeof(how),
    )
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), path)
    return int(result)


def _validated_parts(path: Any, *, allow_root: bool = False) -> tuple[str, ...]:
    if not isinstance(path, str):
        raise ToolPathError(f"path must be a string; got {type(path).__name__}")
    if not path or "\x00" in path:
        raise ToolPathError("path must be a non-empty workspace-relative path")
    parsed = PurePosixPath(path)
    if parsed.is_absolute():
        raise ToolPathError(f"absolute path {path!r} is forbidden")
    parts = tuple(part for part in parsed.parts if part != ".")
    if ".." in parts:
        raise ToolPathError(f"path {path!r} contains forbidden '..' component")
    for part in parts:
        try:
            part.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ToolPathError(f"path component {part!r} is not valid UTF-8") from error
        control = next((char for char in part if ord(char) < 0x20 or ord(char) == 0x7F), None)
        if control is not None:
            raise ToolPathError(
                f"path component {part!r} contains forbidden control character "
                f"U+{ord(control):04X}"
            )
    if not parts and not allow_root:
        raise ToolPathError(f"path {path!r} must name a file")
    return parts


class Workspace:
    """A process-lifetime workspace dirfd; losing it loses confinement."""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(os.path.expanduser(path))
        try:
            existed = os.path.exists(self.path)
            os.makedirs(self.path, mode=0o700, exist_ok=True)
            if not existed:
                os.chmod(self.path, 0o700)
            if not os.path.isdir(self.path):
                raise NotADirectoryError(self.path)
            self.dirfd = os.open(
                self.path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
        except OSError as error:
            raise FilesystemStartupError(
                f"workspace path {self.path!r} cannot be created/opened: {error}"
            ) from error
        try:
            probe = _openat2(
                self.dirfd, ".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
            )
            os.close(probe)
        except (OSError, FilesystemStartupError) as error:
            os.close(self.dirfd)
            if isinstance(error, OSError) and error.errno in (errno.ENOSYS, errno.EPERM):
                reason = f"{error.strerror} ({error.errno})"
            else:
                reason = str(error)
            raise FilesystemStartupError(f"openat2 unavailable: {reason}") from error

    def close(self) -> None:
        if getattr(self, "dirfd", -1) >= 0:
            os.close(self.dirfd)
            self.dirfd = -1

    def _open(self, parts: tuple[str, ...], flags: int, mode: int = 0) -> int:
        path = "." if not parts else "/".join(parts)
        try:
            return _openat2(self.dirfd, path, flags | os.O_CLOEXEC, mode)
        except OSError as error:
            raise ToolPathError(f"path {path!r} refused: {error.strerror}") from error

    def read_file(self, path: Any, max_bytes: int) -> str:
        parts = _validated_parts(path)
        fd = self._open(parts, os.O_RDONLY | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ToolPathError(f"path {path!r} is not a regular file")
            if info.st_size > max_bytes:
                raise ToolPathError(
                    f"max_bytes {max_bytes} exceeded by file size {info.st_size}"
                )
            data = bytearray()
            while len(data) <= max_bytes:
                chunk = os.read(fd, min(65536, max_bytes + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > max_bytes:
                raise ToolPathError(
                    f"max_bytes {max_bytes} exceeded by file size at least {len(data)}"
                )
            try:
                return bytes(data).decode("utf-8")
            except UnicodeDecodeError as error:
                raise ToolPathError(f"path {path!r} is not valid UTF-8") from error
        finally:
            os.close(fd)

    def _parent_dirfd(self, parts: tuple[str, ...]) -> tuple[int, str]:
        current = os.dup(self.dirfd)
        try:
            for component in parts[:-1]:
                try:
                    child = _openat2(
                        current,
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                    )
                except OSError as error:
                    if error.errno != errno.ENOENT:
                        raise
                    try:
                        os.mkdir(component, 0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                    child = _openat2(
                        current,
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                    )
                os.close(current)
                current = child
            return current, parts[-1]
        except OSError as error:
            os.close(current)
            raise ToolPathError(
                f"parent path for {'/'.join(parts)!r} refused: {error.strerror}"
            ) from error

    def write_file(self, path: Any, content: Any, max_bytes: int) -> str:
        parts = _validated_parts(path)
        if not isinstance(content, str):
            raise ToolPathError(
                f"content must be a string; got {type(content).__name__}"
            )
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ToolPathError("content is not valid UTF-8") from error
        if len(data) > max_bytes:
            raise ToolPathError(
                f"max_bytes {max_bytes} exceeded by content size {len(data)}"
            )
        parentfd, basename = self._parent_dirfd(parts)
        try:
            try:
                fd = _openat2(
                    parentfd,
                    basename,
                    os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC,
                )
            except OSError as error:
                if error.errno != errno.ENOENT:
                    raise ToolPathError(
                        f"path {path!r} refused: {error.strerror}"
                    ) from error
                try:
                    fd = _openat2(
                        parentfd,
                        basename,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                        0o600,
                    )
                except OSError as create_error:
                    raise ToolPathError(
                        f"path {path!r} refused: {create_error.strerror}"
                    ) from create_error
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ToolPathError(f"path {path!r} is not a regular file")
                os.ftruncate(fd, 0)
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
            finally:
                os.close(fd)
        finally:
            os.close(parentfd)
        return f"wrote {len(data)} bytes to {path}"

    def list_dir(self, path: Any, max_entries: int) -> str:
        parts = _validated_parts(path, allow_root=True)
        fd = self._open(parts, os.O_RDONLY | os.O_DIRECTORY)
        try:
            entries: list[tuple[bytes, str]] = []
            omitted = 0
            total = 0
            with os.scandir(fd) as iterator:
                for entry in iterator:
                    total += 1
                    if total > 100000:
                        raise ToolPathError(
                            "directory scan ceiling 100000 exceeded"
                        )
                    name = entry.name
                    try:
                        encoded = name.encode("utf-8")
                    except UnicodeEncodeError:
                        omitted += 1
                        continue
                    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in name):
                        omitted += 1
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            suffix = "/"
                        elif entry.is_file(follow_symlinks=False):
                            suffix = ""
                        else:
                            suffix = "@"
                    except OSError as error:
                        raise ToolPathError(
                            f"directory entry {name!r} could not be classified: {error}"
                        ) from error
                    entries.append((encoded, name + suffix))
            entries.sort(key=lambda item: item[0])
            truncated = len(entries) > max_entries
            entries = entries[:max_entries]
            header = f"# {len(entries)} listed, {omitted} omitted, {total} total"
            if truncated:
                header += f" (truncated at max_entries={max_entries})"
            rendered = [header, *(value for _, value in entries)]
            return "\n".join(rendered)
        finally:
            os.close(fd)
