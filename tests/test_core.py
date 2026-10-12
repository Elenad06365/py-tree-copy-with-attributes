import errno
import os
import stat
import tempfile
import unittest

from py_tree_copy_with_attributes import copy_tree, CopyResult


class _TempTree:
    """Small helper around tempfile.mkdtemp for readable setup."""

    def __init__(self):
        self.path = tempfile.mkdtemp(prefix="ptcwat_src_")

    def join(self, *parts):
        return os.path.join(self.path, *parts)

    def write(self, relpath, content=b""):
        full = self.join(relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(content)
        return full

    def symlink(self, relpath, target):
        full = self.join(relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        os.symlink(target, full)
        return full

    def mkdir(self, relpath, mode=0o755):
        full = self.join(relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        os.mkdir(full)
        os.chmod(full, mode)
        return full


class CopyTreeTests(unittest.TestCase):
    def setUp(self):
        self.src = _TempTree()
        self.dst = tempfile.mkdtemp(prefix="ptcwat_dst_")
        os.rmdir(self.dst)  # copy_tree should be able to create the top-level dst

    def tearDown(self):
        import shutil
        for root in (self.src.path, self.dst):
            if os.path.isdir(root):
                shutil.rmtree(root)
            elif os.path.lexists(root):
                os.unlink(root)

    # --- happy path -----------------------------------------------------

    def test_empty_source_directory(self):
        result = copy_tree(self.src.path, self.dst)
        self.assertIsInstance(result, CopyResult)
        self.assertEqual(result.files, 0)
        self.assertEqual(result.directories, 1)
        self.assertEqual(result.symlinks, 0)
        self.assertTrue(os.path.isdir(self.dst))
        self.assertEqual(len(os.listdir(self.dst)), 0)

    def test_nested_files_preserve_content_mode_and_mtime(self):
        content = b"hello attributes\n"
        self.src.write("a/b/file.txt", content)
        f = self.src.join("a/b/file.txt")
        os.chmod(f, 0o640)
        st = os.stat(f)
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))

        result = copy_tree(self.src.path, self.dst)

        self.assertEqual(result.files, 1)
        self.assertEqual(result.directories, 3)  # top + a + b
        dst_file = os.path.join(self.dst, "a/b/file.txt")
        with open(dst_file, "rb") as fh:
            self.assertEqual(fh.read(), content)
        self.assertEqual(stat.S_IMODE(os.stat(dst_file).st_mode), 0o640)
        self.assertEqual(os.stat(dst_file).st_mtime_ns, st.st_mtime_ns)

    # --- symlinks -------------------------------------------------------

    def test_symlink_to_file_copied_as_symlink(self):
        self.src.write("target.txt", b"target")
        self.src.symlink("link.txt", "target.txt")
        result = copy_tree(self.src.path, self.dst)

        dst_link = os.path.join(self.dst, "link.txt")
        self.assertTrue(os.path.islink(dst_link))
        self.assertEqual(os.readlink(dst_link), "target.txt")
        self.assertEqual(result.symlinks, 1)
        self.assertEqual(result.files, 1)

    def test_dangling_symlink_is_copied_verbatim(self):
        self.src.symlink("dangling", "does/not/exist")
        result = copy_tree(self.src.path, self.dst)

        dst_link = os.path.join(self.dst, "dangling")
        self.assertTrue(os.path.islink(dst_link))
        self.assertEqual(os.readlink(dst_link), "does/not/exist")
        self.assertFalse(os.path.exists(dst_link))  # target does not resolve
        self.assertEqual(result.symlinks, 1)

    def test_symlink_to_directory_copied_as_symlink_not_followed(self):
        self.src.mkdir("realdir")
        self.src.write("realdir/inside.txt", b"inside")
        self.src.symlink("dirlink", "realdir")

        result = copy_tree(self.src.path, self.dst)

        dst_link = os.path.join(self.dst, "dirlink")
        self.assertTrue(os.path.islink(dst_link))
        self.assertEqual(os.readlink(dst_link), "realdir")
        # We did NOT descend into the symlink — the realdir copy contains the file.
        self.assertTrue(os.path.isfile(os.path.join(self.dst, "realdir/inside.txt")))
        self.assertEqual(result.symlinks, 1)
        self.assertEqual(result.files, 1)

    def test_absolute_symlink_target_is_preserved_verbatim(self):
        # Absolute targets are kept as-is. We do not rewrite them to be relative.
        target = os.path.join(self.src.path, "abs_target.txt")
        self.src.write("abs_target.txt", b"abs")
        self.src.symlink("abs_link", target)

        copy_tree(self.src.path, self.dst)

        dst_link = os.path.join(self.dst, "abs_link")
        self.assertEqual(os.readlink(dst_link), target)

    # --- directory modes and timestamps ---------------------------------

    def test_directory_mode_preserved(self):
        self.src.mkdir("restricted", mode=0o700)
        copy_tree(self.src.path, self.dst)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dst, "restricted")).st_mode), 0o700)

    def test_top_level_dst_mode_matches_source_when_created(self):
        os.chmod(self.src.path, 0o750)
        copy_tree(self.src.path, self.dst)
        self.assertEqual(stat.S_IMODE(os.stat(self.dst).st_mode), 0o750)

    def test_existing_empty_dst_keeps_its_own_mode(self):
        # Pre-create dst with a different mode; copy_tree should NOT overwrite it.
        os.mkdir(self.dst)
        os.chmod(self.dst, 0o700)
        os.chmod(self.src.path, 0o755)

        copy_tree(self.src.path, self.dst)

        self.assertEqual(stat.S_IMODE(os.stat(self.dst).st_mode), 0o700)

    # --- error paths ----------------------------------------------------

    def test_source_missing_raises_filenotfound(self):
        missing = os.path.join(self.src.path, "nope")
        with self.assertRaises(FileNotFoundError):
            copy_tree(missing, self.dst)

    def test_source_is_a_file_raises_notadir(self):
        f = self.src.write("plain.txt", b"x")
        with self.assertRaises(NotADirectoryError):
            copy_tree(f, self.dst)

    def test_dst_exists_as_file_raises_notadir(self):
        # Recreate dst as a regular file.
        with open(self.dst, "wb") as fh:
            fh.write(b"x")
        with self.assertRaises(NotADirectoryError):
            copy_tree(self.src.path, self.dst)

    def test_dst_nonempty_raises_enotempty(self):
        os.mkdir(self.dst)
        with open(os.path.join(self.dst, "preexisting.txt"), "wb") as fh:
            fh.write(b"x")
        with self.assertRaises(OSError) as ctx:
            copy_tree(self.src.path, self.dst)
        self.assertEqual(ctx.exception.errno, errno.ENOTEMPTY)

    def test_unsupported_file_type_raises(self):
        # FIFOs are in the POSIX spec and trivially creatable, but not in scope.
        fifo = self.src.join("named.pipe")
        os.mkfifo(fifo)
        with self.assertRaises(OSError):
            copy_tree(self.src.path, self.dst)

    # --- counts ---------------------------------------------------------

    def test_counts_match_actual_tree(self):
        self.src.write("f1", b"one")
        self.src.write("sub/f2", b"two")
        self.src.symlink("sub/link", "../f1")
        self.src.mkdir("sub/deep")

        result = copy_tree(self.src.path, self.dst)

        # directories: top + sub + deep = 3
        # files: f1 + f2 = 2
        # symlinks: link = 1
        self.assertEqual(result.directories, 3)
        self.assertEqual(result.files, 2)
        self.assertEqual(result.symlinks, 1)
        self.assertEqual(result.total_bytes, len(b"one") + len(b"two"))

    def test_idempotent_second_copy_into_new_empty_dst(self):
        self.src.write("f", b"abc")
        copy_tree(self.src.path, self.dst)
        # Now do it again into a fresh empty dst.
        import shutil
        shutil.rmtree(self.dst)
        os.mkdir(self.dst)
        result = copy_tree(self.src.path, self.dst)
        self.assertEqual(result.files, 1)
        with open(os.path.join(self.dst, "f"), "rb") as fh:
            self.assertEqual(fh.read(), b"abc")


if __name__ == "__main__":
    unittest.main()
