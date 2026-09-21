"""open_history(): every write must land in the file that was verified.

Run from the repository root:  python3 -m unittest discover -s tests -v
Standard library only. Each test gets its own XDG_CACHE_HOME.
"""
import hashlib
import importlib.util
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_collect(cache_home):
    with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": cache_home}):
        spec = importlib.util.spec_from_file_location("collect_under_test", os.path.join(ROOT, "collect.py"))
        module = importlib.util.module_from_spec(spec)
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    return module


def digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def make_decoy(path):
    """Another SQLite database of the same user: the file an attacker would
    like the collector to write into."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE precious (x)")
    con.execute("INSERT INTO precious VALUES ('keep me')")
    con.commit()
    con.close()
    return digest(path)


class OpenHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.collect = load_collect(self.tmp.name)
        self.cache = self.collect.CACHE_DIR
        self.db = os.path.join(self.cache, self.collect.HISTORY_DB)
        self.decoy = os.path.join(self.tmp.name, "decoy.sqlite")
        self.decoy_digest = make_decoy(self.decoy)

    def assertDecoyUntouched(self):
        self.assertEqual(digest(self.decoy), self.decoy_digest, "the decoy database was modified")
        self.assertEqual(sorted(os.listdir(self.tmp.name)), sorted(["decoy.sqlite", os.path.basename(self.cache)]),
                         "a side file appeared next to the decoy")
        con = sqlite3.connect(self.decoy)
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
        finally:
            con.close()
        self.assertEqual(tables, {"precious"})

    def test_opens_private_database_without_side_files(self):
        con = self.collect.open_history()
        try:
            con.execute("BEGIN")
            con.execute("INSERT INTO samples (ts, iface, rx, tx) VALUES (1.0, 'ether1', 1, 2)")
            con.execute("COMMIT")
            self.assertEqual(con.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 1)
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "memory")
            # while the connection is open: still no -journal/-wal/-shm
            self.assertEqual(os.listdir(self.cache), [self.collect.HISTORY_DB])
        finally:
            con.close()
        st = os.lstat(self.db)
        self.assertTrue(stat.S_ISREG(st.st_mode))
        self.assertEqual(st.st_mode & 0o777, 0o600)
        self.assertEqual(os.lstat(self.cache).st_mode & 0o777, 0o700)
        self.assertEqual(os.listdir(self.cache), [self.collect.HISTORY_DB])

    def test_symlink_in_place_is_refused(self):
        self.collect.ensure_cache_dir()
        os.symlink(self.decoy, self.db)
        with self.assertRaises(OSError):
            self.collect.open_history()
        self.assertDecoyUntouched()

    def test_hard_link_to_another_database_is_refused(self):
        self.collect.ensure_cache_dir()
        os.link(self.decoy, self.db)
        with self.assertRaises(OSError):
            self.collect.open_history()
        os.unlink(self.db)
        self.assertDecoyUntouched()

    def _open_with_swap(self, swap):
        """Run open_history() with the database entry swapped after validation and
        immediately before SQLite opens it: the exact window of the reported race."""
        real_connect = sqlite3.connect
        swapped = []

        def racing_connect(*args, **kwargs):
            swap()
            swapped.append(True)
            return real_connect(*args, **kwargs)

        with mock.patch.object(self.collect.sqlite3, "connect", side_effect=racing_connect):
            with self.assertRaises(OSError) as caught:
                self.collect.open_history()
        self.assertTrue(swapped, "the swap hook never ran")
        self.assertIn("replaced", str(caught.exception))

    def test_race_swap_to_symlink_between_validation_and_open(self):
        def swap():
            os.unlink(self.db)
            os.symlink(self.decoy, self.db)

        self._open_with_swap(swap)
        self.assertDecoyUntouched()

    def test_race_swap_to_other_file_between_validation_and_open(self):
        # prepared before connect() is patched: make_decoy() itself uses sqlite3.connect
        self.collect.ensure_cache_dir()
        other = os.path.join(self.cache, "other.sqlite")
        self.other_digest = make_decoy(other)

        def swap():
            os.replace(other, self.db)

        self._open_with_swap(swap)
        self.assertEqual(digest(self.db), self.other_digest, "the swapped-in database was written to")
        self.assertDecoyUntouched()

    def test_foreign_journal_is_removed_without_following_it(self):
        self.collect.ensure_cache_dir()
        os.symlink(self.decoy, self.db + "-journal")
        con = self.collect.open_history()
        try:
            con.execute("INSERT INTO samples (ts, iface, rx, tx) VALUES (1.0, 'ether1', 1, 2)")
        finally:
            con.close()
        self.assertFalse(os.path.lexists(self.db + "-journal"))
        self.assertDecoyUntouched()

    def test_directory_that_is_a_symlink_is_refused(self):
        real = os.path.join(self.tmp.name, "elsewhere")
        os.mkdir(real, 0o700)
        link = os.path.join(self.tmp.name, "link")
        os.symlink(real, link)
        with self.assertRaises(OSError):
            self.collect.open_private_dir(link)
        os.unlink(link)
        os.rmdir(real)

    def test_directory_open_to_others_is_refused(self):
        loose = os.path.join(self.tmp.name, "loose")
        os.mkdir(loose, 0o755)
        with self.assertRaises(OSError):
            self.collect.open_private_dir(loose)
        os.rmdir(loose)

    def test_garbage_database_is_set_aside(self):
        self.collect.ensure_cache_dir()
        with open(self.db, "wb") as f:
            f.write(b"this is not a database" * 64)
        os.chmod(self.db, 0o600)
        with self.assertRaises(sqlite3.DatabaseError):
            self.collect.open_history()
        self.assertTrue(os.path.exists(self.db + ".broken"))
        con = self.collect.open_history()  # the next tick starts clean
        con.close()


if __name__ == "__main__":
    unittest.main()
