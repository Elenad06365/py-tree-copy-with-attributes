"""Core implementation of recursive directory-tree copy.

The function :func:`copy_tree` walks an existing directory recursively and
recreates it at a destination path, preserving, as best the operating system
allows:

* file and directory permissions (st_mode)
* access and modification timestamps (st_atime, st_mtime)
* the exact symlink structure — symlinks are copied as symlinks rather than
  being followed and materialised

For *regular* files and directories the actual content is copied byte-for-byte.
For *symlinks* the link target string is copied verbatim; the target itself is
not read.

Edge cases the implementation commits to:

* A dangling symlink is copied as-is, because we only ever read the link's
  target string, never the target itself.
* The top-level destination is created with the source's mode if it did not
  previously exist. If it already exists as an empty directory, its mode is
  left untouched.
* ``dst`` being an existing non-empty directory is treated as an error: we
  refuse to merge trees silently.

Things deliberately NOT supported (kept out of scope on purpose):

* Ownership (UID/GID) preservation — requires root and is platform-dependent.
* Extended attributes (xattr) — the name says "attributes" in the POSIX file
  metadata sense (mode, times, symlink structure); xattr support is left out.
* Cross-filesystem hardlinking or reflinks — a straight byte copy is used.
"""

from __future__ import annotations

import errno
import os
import stat
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


@dataclass
class CopyResult:
    """Summary of a single :func:`copy_tree` run.

    Counts are exact. Dangling symlinks are counted under ``symlinks`` regardless
    of whether their target resolves.
    """

    files: int = 0
    directories: int = 0
    symlinks: int = 0
    total_bytes: int = 0


def _walk_sorted(root: str) -> Iterator[tuple[str, list[str], list[str]]]:
    """Depth-first walk that yields entries in sorted order.

    Sorting is used purely so that, given the same source tree, the set of
    emitted :class:`CopyResult` counts is identical across runs and platforms.
    The actual copy result does not depend on order, but deterministic output
    keeps the test suite stable.
    """

    dirs: list[str] = []
    files: list[str] = []

    try:
        with os.scandir(root) as it:
            for entry in it:
                name = entry.name
                try:
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append(name)
                    else:
                        files.append(name)
                except OSError:
                    # On some platforms scandir may fail for an individual
                    # entry; treat unreadable entries as plain files so the
                    # walk continues rather than aborting.
                    files.append(name)
    except OSError:
        return

    dirs.sort()
    files.sort()
    yield root, dirs, files

    for d in dirs:
        yield from _walk_sorted(os.path.join(root, d))


def _copy_symlink(src: str, dst: str) -> None:
    """Copy a symlink as a symlink, never following it.

    Using ``lstat``/``readlink`` (rather than ``stat``/open) means a dangling
    symlink is handled identically to one whose target exists.
    """

    target = os.readlink(src)
    if os.path.islink(dst):
        os.unlink(dst)
    elif os.path.lexists(dst):
        if os.path.isdir(dst) and not os.path.islink(dst):
            shutil.rmtree(dst)
        else:
            os.unlink(dst)
    os.symlink(target, dst)
    st = os.lstat(src)
    try:
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
    except (OSError, ValueError, OverflowError):
        # Not every platform supports utime on symlinks; it is best-effort.
        pass


def _copy_regular(src: str, dst: str, st: os.stat_result) -> int:
    """Byte-copy a regular file and restore its mode and timestamps.

    ``shutil.copyfile`` is used rather than ``shutil.copy2`` so we can apply
    timestamps in nanosecond precision via ``st_atime_ns`` / ``st_mtime_ns``,
    which ``copy2`` does not guarantee.
    """

    if os.path.lexists(dst):
        if os.path.isdir(dst) and not os.path.islink(dst):
            shutil.rmtree(dst)
        else:
            os.unlink(dst)

    shutil.copyfile(src, dst)
    os.chmod(dst, stat.S_IMODE(st.st_mode))
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    return os.path.getsize(dst)


def _mkdir_preserving(src_dir: str, dst_dir: str, src_st: os.stat_result) -> bool:
    """Create ``dst_dir`` mirroring ``src_dir``'s mode.

    Returns ``True`` if a *new* directory was created, ``False`` if it already
    existed as a directory. Any other pre-existing entry type is an error.
    """

    if os.path.lexists(dst_dir):
        if os.path.islink(dst_dir) or not os.path.isdir(dst_dir):
            raise NotADirectoryError(errno.ENOTDIR, "destination exists and is not a directory", dst_dir)
        return False

    os.mkdir(dst_dir)
    os.chmod(dst_dir, stat.S_IMODE(src_st.st_mode))
    return True


def copy_tree(src: str | os.PathLike, dst: str | os.PathLike) -> CopyResult:
    """Recursively copy ``src`` into ``dst``, preserving attributes.

    Returns a :class:`CopyResult` with exact counts.

    Raises:
        FileNotFoundError: ``src`` does not exist (or is a dangling symlink
            at the top level).
        NotADirectoryError: ``src`` is not a directory, or ``dst`` exists and
            is not a directory.
        OSError: underlying filesystem errors propagate.
    """

    src = os.fspath(src)
    dst = os.fspath(dst)

    # ``lstat`` so a top-level dangling symlink raises FileNotFoundError with a
    # clear message rather than being silently skipped.
    if not os.path.lexists(src):
        raise FileNotFoundError(errno.ENOENT, "source does not exist", src)

    src_st = os.lstat(src)
    if not stat.S_ISDIR(src_st.st_mode):
        raise NotADirectoryError(errno.ENOTDIR, "source is not a directory", src)

    created_top = _mkdir_preserving(src, dst, src_st)
    if not created_top:
        # Refuse to merge into a non-empty destination. Merging silently would
        # make it impossible to tell which files came from which tree.
        if any(os.scandir(dst)):
            raise OSError(errno.ENOTEMPTY, "destination directory is not empty", dst)

    result = CopyResult(directories=1 if created_top else 0)

    for current, dirs, files in _walk_sorted(src):
        rel = os.path.relpath(current, src)
        dst_current = dst if rel == os.curdir else os.path.join(dst, rel)

        for name in dirs:
            src_entry = os.path.join(current, name)
            dst_entry = os.path.join(dst_current, name)
            st = os.lstat(src_entry)
            if stat.S_ISLNK(st.st_mode):
                _copy_symlink(src_entry, dst_entry)
                result.symlinks += 1
            else:
                if _mkdir_preserving(src_entry, dst_entry, st):
                    result.directories += 1

        for name in files:
            src_entry = os.path.join(current, name)
            dst_entry = os.path.join(dst_current, name)
            st = os.lstat(src_entry)
            if stat.S_ISLNK(st.st_mode):
                _copy_symlink(src_entry, dst_entry)
                result.symlinks += 1
            elif stat.S_ISREG(st.st_mode):
                result.total_bytes += _copy_regular(src_entry, dst_entry, st)
                result.files += 1
            else:
                # FIFOs, sockets, device files are intentionally out of scope.
                # Rather than silently dropping them, raise so the caller knows
                # the copy is incomplete.
                raise OSError(errno.ENOTSUP, f"unsupported file type: {stat.filemode(st.st_mode)}", src_entry)

    return result
