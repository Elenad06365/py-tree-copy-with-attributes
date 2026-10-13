# py-tree-copy-with-attributes

Recursively copies a directory tree, preserving file permissions, access and modification timestamps, and symlink structure (symlinks are copied as symlinks rather than being followed).

```python
from py_tree_copy_with_attributes import copy_tree, CopyResult

result: CopyResult = copy_tree("src", "dst")
print(result.files, result.directories, result.symlinks, result.total_bytes)
```

## Why

`shutil.copytree` follows symlinks by default and does not give you a usable result object. `cp -a` is a shell tool, not a Python API. This library gives you a single function that copies a tree, preserves POSIX file metadata (mode, atime/mtime, symlink targets), and returns exact counts you can assert on.

The trade-off: we copy **byte-for-byte**. No hardlinks, no reflinks, no attempt to preserve ownership (UID/GID) or extended attributes (xattr). The word "attributes" here means POSIX file metadata — mode, timestamps, and symlink structure — not xattr.

## Edge you will hit

If the destination directory already exists and is **not empty**, `copy_tree` raises `OSError(ENOTEMPTY)` rather than silently merging trees. Merge semantics are ambiguous; a loud failure is easier to reason about than a partial overwrite. If you need to overlay one tree onto another, delete or empty the destination first.

Dangling symlinks are copied verbatim — only the link target string is read, never the target itself, so a link whose target does not exist is handled identically to one whose target does.

FIFOs, sockets, and device files are out of scope and will raise `OSError(ENOTSUP)` if encountered, rather than being silently dropped.
