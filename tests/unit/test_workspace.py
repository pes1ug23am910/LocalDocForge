"""Job workspace isolation, atomic publish, collision policies, cleanup."""

import os
import time

import pytest

from localdocforge.jobs.workspace import (
    CollisionPolicy,
    JobWorkspace,
    OutputCollisionError,
    atomic_publish,
    cleanup_stale_workspaces,
    contained_output_path,
)
from localdocforge.security.paths import PathSecurityError, ensure_contained, is_within


class TestWorkspace:
    def test_workspace_created_and_cleaned(self, tmp_path):
        with JobWorkspace(root=tmp_path) as ws:
            assert ws.path.is_dir()
            assert ws.path.parent == tmp_path
            marker = ws.path / "scratch.bin"
            marker.write_bytes(b"data")
        assert not ws.path.exists()

    def test_cleanup_on_exception(self, tmp_path):
        with pytest.raises(RuntimeError):
            with JobWorkspace(root=tmp_path) as ws:
                (ws.path / "x").write_bytes(b"y")
                raise RuntimeError("job failed")
        assert not ws.path.exists()

    def test_subdir_containment(self, tmp_path):
        with JobWorkspace(root=tmp_path) as ws:
            sub = ws.subdir("pages")
            assert is_within(sub, ws.path)
            with pytest.raises(PathSecurityError):
                ws.contain(ws.path / ".." / "escape.txt")

    def test_unique_ids(self, tmp_path):
        a, b = JobWorkspace(root=tmp_path), JobWorkspace(root=tmp_path)
        try:
            assert a.path != b.path
        finally:
            a.cleanup()
            b.cleanup()

    def test_posix_permissions(self, tmp_path):
        if os.name != "posix":
            pytest.skip("POSIX-only permission check")
        with JobWorkspace(root=tmp_path) as ws:
            assert (ws.path.stat().st_mode & 0o777) == 0o700


class TestStaleCleanup:
    def test_old_workspaces_removed_fresh_kept(self, tmp_path):
        old = JobWorkspace(root=tmp_path)
        fresh = JobWorkspace(root=tmp_path)
        ancient = time.time() - 48 * 3600
        os.utime(old.path, (ancient, ancient))
        removed = cleanup_stale_workspaces(tmp_path, max_age_seconds=24 * 3600)
        assert removed == 1
        assert not old.path.exists()
        assert fresh.path.exists()
        fresh.cleanup()

    def test_unrelated_dirs_untouched(self, tmp_path):
        other = tmp_path / "user-data"
        other.mkdir()
        ancient = time.time() - 999 * 3600
        os.utime(other, (ancient, ancient))
        cleanup_stale_workspaces(tmp_path, max_age_seconds=1)
        assert other.exists()


class TestAtomicPublish:
    def _source(self, tmp_path, content=b"%PDF-fake"):
        src = tmp_path / "staging" / "out.pdf"
        src.parent.mkdir(exist_ok=True)
        src.write_bytes(content)
        return src

    def test_publish_moves_content(self, tmp_path):
        src = self._source(tmp_path)
        dest = tmp_path / "final" / "result.pdf"
        written = atomic_publish(src, dest)
        assert written == dest
        assert dest.read_bytes() == b"%PDF-fake"

    def test_fail_policy_preserves_existing(self, tmp_path):
        src = self._source(tmp_path)
        dest = tmp_path / "result.pdf"
        dest.write_bytes(b"original")
        with pytest.raises(OutputCollisionError):
            atomic_publish(src, dest, collision=CollisionPolicy.FAIL)
        assert dest.read_bytes() == b"original"

    def test_rename_policy_finds_free_name(self, tmp_path):
        dest = tmp_path / "result.pdf"
        dest.write_bytes(b"original")
        first = atomic_publish(self._source(tmp_path), dest, collision=CollisionPolicy.RENAME)
        assert first == tmp_path / "result (1).pdf"
        second = atomic_publish(
            self._source(tmp_path, b"third"), dest, collision=CollisionPolicy.RENAME
        )
        assert second == tmp_path / "result (2).pdf"
        assert dest.read_bytes() == b"original"

    def test_overwrite_policy_replaces(self, tmp_path):
        dest = tmp_path / "result.pdf"
        dest.write_bytes(b"old")
        atomic_publish(self._source(tmp_path, b"new"), dest, collision=CollisionPolicy.OVERWRITE)
        assert dest.read_bytes() == b"new"

    def test_no_staging_litter(self, tmp_path):
        dest = tmp_path / "result.pdf"
        atomic_publish(self._source(tmp_path), dest)
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".ldf-staging")]
        assert leftovers == []

    def test_missing_source(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            atomic_publish(tmp_path / "ghost.pdf", tmp_path / "out.pdf")

    def test_directory_destination_rejected(self, tmp_path):
        src = self._source(tmp_path)
        with pytest.raises(IsADirectoryError):
            atomic_publish(src, tmp_path)


class TestOutputContainment:
    def test_allowed_root_accepts(self, tmp_path):
        result = contained_output_path(tmp_path / "sub" / "x.pdf", [tmp_path])
        assert is_within(result, tmp_path)

    def test_outside_all_roots_rejected(self, tmp_path):
        outside = tmp_path.parent / "elsewhere-nonexistent" / "x.pdf"
        with pytest.raises(PathSecurityError):
            contained_output_path(outside, [tmp_path])

    def test_traversal_rejected(self, tmp_path):
        with pytest.raises(PathSecurityError):
            contained_output_path(tmp_path / ".." / "x.pdf", [tmp_path])

    def test_ensure_contained_returns_resolved(self, tmp_path):
        resolved = ensure_contained(tmp_path / "a" / ".." / "b.txt", tmp_path)
        assert resolved == (tmp_path / "b.txt").resolve()
