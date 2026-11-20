# ruff: noqa: S603, S607
# Subprocess argv is constructed solely from fixed local regression fixtures; no shell/user input.
"""Regression tests for files/tls_snapshot.py, the exact script the chart embeds in the
worker and receipt-ingress init containers (MSG-08H review P1).

The fixtures reproduce the Kubernetes AtomicWriter layout (<src>/..data -> ..<gen>,
<src>/<key> -> ..data/<key>) and switch generations at chosen points between file reads,
deterministically. Standard library + the `openssl` CLI only.

Run: python3 -m unittest discover -s ci/regression -p 'test_*.py'   (from the chart dir)
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CHART = Path(__file__).resolve().parents[2]
SCRIPT = CHART / "files" / "tls_snapshot.py"

_spec = importlib.util.spec_from_file_location("tls_snapshot", SCRIPT)
assert _spec and _spec.loader
ts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ts)


def make_pair(directory: Path, cn: str) -> tuple[bytes, bytes]:
    key, crt = directory / f"{cn}.key", directory / f"{cn}.crt"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            f"/CN={cn}.test.invalid",
            "-keyout",
            str(key),
            "-out",
            str(crt),
        ],
        check=True,
        capture_output=True,
    )
    return crt.read_bytes(), key.read_bytes()


class SecretVolume:
    """A Secret volume in the kubelet AtomicWriter layout."""

    def __init__(self, root: Path, names: list[str]) -> None:
        self.root = root
        self.names = names
        self.root.mkdir()
        self.n = 0

    def write_generation(self, files: dict[str, bytes], *, remove_old: bool = True) -> str:
        self.n += 1
        gen = f"..2026_11_15_00_00_{self.n:02d}.{self.n:09d}"
        gdir = self.root / gen
        gdir.mkdir()
        for name, content in files.items():
            (gdir / name).write_bytes(content)
            os.chmod(gdir / name, 0o440)
        old = os.readlink(self.root / "..data") if (self.root / "..data").is_symlink() else None
        tmp = self.root / "..data_tmp"
        os.symlink(gen, tmp)
        os.replace(tmp, self.root / "..data")  # atomic generation switch, like the kubelet
        for name in files:
            link = self.root / name
            if not link.is_symlink():
                os.symlink(f"..data/{name}", link)
        if old and remove_old:
            shutil.rmtree(self.root / old)
        return gen


class TlsSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.keys = Path(tempfile.mkdtemp(prefix="tls-snapshot-keys-"))
        cls.A = make_pair(cls.keys, "gen-a")
        cls.B = make_pair(cls.keys, "gen-b")
        cls.C = make_pair(cls.keys, "gen-c")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.keys, ignore_errors=True)

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tls-snapshot-"))
        self.src = SecretVolume(self.tmp / "src", ["tls.crt", "tls.key"])
        self.dest = self.tmp / "dest"
        self.dest.mkdir()
        self.sleeps: list[float] = []

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ helpers

    def gen(self, pair: tuple[bytes, bytes], **kw: bool) -> str:
        return self.src.write_generation({"tls.crt": pair[0], "tls.key": pair[1]}, **kw)

    def run_snapshot(self, read=None, attempts: int = 5) -> str:
        kwargs = {"read": read} if read else {}
        return ts.snapshot(
            str(self.src.root),
            str(self.dest),
            "tls.crt",
            "tls.key",
            attempts=attempts,
            delay=0.25,
            sleep=self.sleeps.append,
            **kwargs,
        )

    def published(self) -> tuple[bytes, bytes]:
        cur = self.dest / "current"
        return (cur / "tls.crt").read_bytes(), (cur / "tls.key").read_bytes()

    def assert_valid_published_pair(self) -> None:
        cur = self.dest / "current"
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_cert_chain(str(cur / "tls.crt"), str(cur / "tls.key"))  # raises on mismatch
        for name in ("tls.crt", "tls.key"):
            st = os.stat(cur / name)
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
            self.assertEqual(st.st_uid, os.getuid())
        self.assertEqual(stat.S_IMODE(os.stat(os.path.realpath(cur)).st_mode), 0o700)
        self.assertTrue(os.path.islink(cur))

    def swap_after(
        self,
        first_name: str,
        new_pair: tuple[bytes, bytes],
        times: int = 1,
        remove_old: bool = True,
    ):
        """read() wrapper: after reading `first_name`, switch to a new generation."""
        state = {"left": times}

        def read(path: str) -> bytes:
            data = ts._read_bytes(path)
            if os.path.basename(path) == first_name and state["left"] > 0:
                state["left"] -= 1
                self.gen(new_pair, remove_old=remove_old)
            return data

        return read

    # -------------------------------------------------------------------- tests

    def test_fixture_reproduces_the_reported_mixed_pair_with_naive_reads(self) -> None:
        self.gen(self.A)
        crt = (self.src.root / "tls.crt").read_bytes()  # naive per-file read ...
        self.gen(self.B)  # ... generation switch ...
        key = (self.src.root / "tls.key").read_bytes()  # ... new key
        self.assertEqual((crt, key), (self.A[0], self.B[1]))  # the P1 hazard is real

    def test_single_generation_is_published_0600_and_valid(self) -> None:
        self.gen(self.A)
        self.run_snapshot()
        self.assertEqual(self.published(), self.A)
        self.assert_valid_published_pair()
        self.assertEqual(self.sleeps, [])

    def test_switch_between_reads_retries_the_whole_set(self) -> None:
        self.gen(self.A)
        self.run_snapshot(read=self.swap_after("tls.crt", self.B, remove_old=False))
        self.assertEqual(self.published(), self.B)  # both from the NEW generation
        self.assert_valid_published_pair()
        self.assertEqual(self.sleeps, [0.25])  # exactly one retry

    def test_vanished_generation_retries_the_whole_set(self) -> None:
        self.gen(self.A)
        # The switch deletes the generation being read: the key read hits FileNotFoundError.
        self.run_snapshot(read=self.swap_after("tls.crt", self.B, remove_old=True))
        self.assertEqual(self.published(), self.B)
        self.assert_valid_published_pair()

    def test_continuous_churn_fails_and_keeps_the_previous_complete_set(self) -> None:
        self.gen(self.A)
        self.run_snapshot()
        before = self.published()
        with self.assertRaises(ts.SnapshotError):
            self.run_snapshot(read=self.swap_after("tls.crt", self.C, times=99), attempts=3)
        self.assertEqual(self.published(), before)  # nothing mixed, previous set intact
        self.assert_valid_published_pair()
        self.assertEqual(self.sleeps, [0.25, 0.25])
        self.assertEqual(
            sorted(p.name for p in self.dest.iterdir() if p.name.startswith(".gen-")),
            [os.readlink(self.dest / "current")],
        )  # no staged leftovers

    def test_mismatched_pair_in_one_generation_is_never_published(self) -> None:
        self.gen((self.A[0], self.B[1]))  # operator error: cert A with key B
        with self.assertRaises(ts.SnapshotError) as info:
            self.run_snapshot(attempts=2)
        self.assertIn("do not form a valid pair", str(info.exception))
        self.assertFalse((self.dest / "current").exists())
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_missing_data_link_fails_closed(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        (plain / "tls.crt").write_bytes(self.A[0])
        (plain / "tls.key").write_bytes(self.A[1])
        with self.assertRaises(ts.SnapshotError):
            ts.snapshot(str(plain), str(self.dest), "tls.crt", "tls.key", attempts=1)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_restart_rerun_switches_atomically_and_removes_leftovers(self) -> None:
        self.gen(self.A)
        first = self.run_snapshot()
        # Simulate a crash of an earlier run: an orphaned staging dir and temp symlink.
        (self.dest / ".gen-orphan").mkdir()
        os.symlink(".gen-orphan", self.dest / ".current.tmp-dead")
        self.gen(self.B)
        second = self.run_snapshot()  # init container rerun after pod restart
        self.assertNotEqual(first, second)
        self.assertEqual(self.published(), self.B)
        self.assert_valid_published_pair()
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), sorted(["current", second]))

    def test_optional_ca_is_part_of_the_same_generation(self) -> None:
        self.src.write_generation({"tls.crt": self.A[0], "tls.key": self.A[1], "ca.crt": self.A[0]})
        ts.snapshot(str(self.src.root), str(self.dest), "tls.crt", "tls.key", "ca.crt", attempts=1)
        self.assertEqual((self.dest / "current" / "ca.crt").read_bytes(), self.A[0])

    def test_cli_as_embedded_by_the_chart(self) -> None:
        """`python -I -c <file> --source ... --dest ...`, exactly as rendered."""
        self.gen(self.A)
        code = SCRIPT.read_text()
        ok = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                code,
                "--source",
                str(self.src.root),
                "--dest",
                str(self.dest),
                "--cert",
                "tls.crt",
                "--key",
                "tls.key",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assert_valid_published_pair()
        self.gen((self.A[0], self.B[1]))
        bad = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                code,
                "--source",
                str(self.src.root),
                "--dest",
                str(self.dest),
                "--cert",
                "tls.crt",
                "--key",
                "tls.key",
                "--attempts",
                "2",
                "--delay",
                "0",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(bad.returncode, 1)
        self.assertNotIn("PRIVATE KEY", bad.stderr + bad.stdout)  # no key material printed
        self.assertEqual(self.published(), self.A)  # previous valid set still published


if __name__ == "__main__":
    unittest.main()
