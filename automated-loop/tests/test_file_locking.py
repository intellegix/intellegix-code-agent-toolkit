"""Tests for file_locking module — LockRegistry and FileManifest."""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from config import FileLockEntry, MultiAgentConfig
from file_locking import FileManifest, LockRegistry


@pytest.fixture
def lock_project(tmp_path: Path) -> Path:
    """Create a project dir with .agents/shared/ structure."""
    shared = tmp_path / ".agents" / "shared"
    shared.mkdir(parents=True)
    return tmp_path


@pytest.fixture
def fast_config() -> MultiAgentConfig:
    """Config with zero delays for fast tests."""
    return MultiAgentConfig(
        dropbox_sync_delay_seconds=0.0,
        lock_retry_attempts=3,
        lock_retry_delay_seconds=0.0,
        lock_ttl_seconds=60,
    )


@pytest.fixture
def registry(lock_project: Path, fast_config: MultiAgentConfig) -> LockRegistry:
    return LockRegistry(lock_project, fast_config)


# ── LockRegistry: Basic Operations ──────────────────────────────────


class TestLockRegistryBasic:
    def test_acquire_uncontested(self, registry: LockRegistry) -> None:
        """Agent can acquire a lock on an unlocked file."""
        assert registry.acquire("/src/main.py", "agent-1", sync_delay=0) is True

    def test_acquire_already_owned(self, registry: LockRegistry) -> None:
        """Agent can re-acquire a lock it already holds."""
        registry.acquire("/src/main.py", "agent-1", sync_delay=0)
        assert registry.acquire("/src/main.py", "agent-1", sync_delay=0) is True

    def test_acquire_contested_by_other(self, registry: LockRegistry) -> None:
        """Agent cannot acquire a lock held by another agent."""
        registry.acquire("/src/main.py", "agent-1", sync_delay=0)
        assert registry.acquire("/src/main.py", "agent-2", sync_delay=0) is False

    def test_release_own_lock(self, registry: LockRegistry) -> None:
        """Agent can release its own lock."""
        registry.acquire("/src/main.py", "agent-1", sync_delay=0)
        assert registry.release("/src/main.py", "agent-1") is True

    def test_release_not_owned(self, registry: LockRegistry) -> None:
        """Agent cannot release another agent's lock."""
        registry.acquire("/src/main.py", "agent-1", sync_delay=0)
        assert registry.release("/src/main.py", "agent-2") is False

    def test_release_nonexistent(self, registry: LockRegistry) -> None:
        """Releasing a non-existent lock returns True (no-op)."""
        assert registry.release("/src/main.py", "agent-1") is True

    def test_release_all(self, registry: LockRegistry) -> None:
        """release_all removes all locks for an agent."""
        registry.acquire("/src/a.py", "agent-1", sync_delay=0)
        registry.acquire("/src/b.py", "agent-1", sync_delay=0)
        registry.acquire("/src/c.py", "agent-2", sync_delay=0)
        count = registry.release_all("agent-1")
        assert count == 2
        # agent-2's lock should remain
        assert registry.is_locked_by_other("/src/c.py", "agent-1") is True

    def test_release_all_no_locks(self, registry: LockRegistry) -> None:
        """release_all returns 0 when agent has no locks."""
        assert registry.release_all("agent-1") == 0


class TestLockRegistryAfterRelease:
    def test_acquire_after_release(self, registry: LockRegistry) -> None:
        """Another agent can acquire after the first releases."""
        registry.acquire("/src/main.py", "agent-1", sync_delay=0)
        registry.release("/src/main.py", "agent-1")
        assert registry.acquire("/src/main.py", "agent-2", sync_delay=0) is True

    def test_get_locks_after_operations(self, registry: LockRegistry) -> None:
        """get_locks returns current lock state."""
        registry.acquire("/src/a.py", "agent-1", sync_delay=0)
        registry.acquire("/src/b.py", "agent-2", sync_delay=0)
        locks = registry.get_locks()
        assert len(locks) == 2


class TestLockRegistryIsLockedByOther:
    def test_not_locked(self, registry: LockRegistry) -> None:
        assert registry.is_locked_by_other("/src/main.py", "agent-1") is False

    def test_locked_by_self(self, registry: LockRegistry) -> None:
        registry.acquire("/src/main.py", "agent-1", sync_delay=0)
        assert registry.is_locked_by_other("/src/main.py", "agent-1") is False

    def test_locked_by_other(self, registry: LockRegistry) -> None:
        registry.acquire("/src/main.py", "agent-1", sync_delay=0)
        assert registry.is_locked_by_other("/src/main.py", "agent-2") is True


# ── LockRegistry: TTL Expiry ────────────────────────────────────────


class TestLockRegistryTTL:
    def test_expired_lock_cleaned(
        self, lock_project: Path, fast_config: MultiAgentConfig
    ) -> None:
        """Expired locks are cleaned on read."""
        reg = LockRegistry(lock_project, fast_config)

        # Write a lock with past timestamp (expired by TTL=60)
        expired_time = (
            datetime.now(timezone.utc) - timedelta(seconds=120)
        ).isoformat()
        locks = {
            "/src/old.py": {
                "owner": "agent-1",
                "acquired_at": expired_time,
                "ttl_seconds": 60,
            }
        }
        reg._write_locks(locks)

        # Should be cleaned on acquire attempt
        assert reg.acquire("/src/old.py", "agent-2", sync_delay=0) is True

    def test_non_expired_lock_persists(self, registry: LockRegistry) -> None:
        """Non-expired locks are not cleaned."""
        registry.acquire("/src/main.py", "agent-1", sync_delay=0)
        locks = registry.get_locks()
        assert len(locks) == 1

    def test_clean_expired_removes_old(self, registry: LockRegistry) -> None:
        """_clean_expired_locks drops entries past TTL."""
        expired_time = (
            datetime.now(timezone.utc) - timedelta(seconds=120)
        ).isoformat()
        locks = {
            "/a.py": {"owner": "x", "acquired_at": expired_time, "ttl_seconds": 60},
            "/b.py": {
                "owner": "y",
                "acquired_at": datetime.now(timezone.utc).isoformat(),
                "ttl_seconds": 60,
            },
        }
        cleaned = registry._clean_expired_locks(locks)
        assert len(cleaned) == 1
        assert "/b.py" in cleaned


# ── LockRegistry: Corrupt/Missing Files ─────────────────────────────


class TestLockRegistryCorrupt:
    def test_corrupt_locks_json(
        self, lock_project: Path, fast_config: MultiAgentConfig
    ) -> None:
        """Corrupt locks file returns empty dict."""
        reg = LockRegistry(lock_project, fast_config)
        reg.locks_path.parent.mkdir(parents=True, exist_ok=True)
        reg.locks_path.write_text("not json!", encoding="utf-8")
        assert reg._read_locks() == {}

    def test_locks_not_dict(
        self, lock_project: Path, fast_config: MultiAgentConfig
    ) -> None:
        """Locks file with non-dict returns empty."""
        reg = LockRegistry(lock_project, fast_config)
        reg.locks_path.parent.mkdir(parents=True, exist_ok=True)
        reg.locks_path.write_text('["list"]', encoding="utf-8")
        assert reg._read_locks() == {}

    def test_missing_locks_file(
        self, lock_project: Path, fast_config: MultiAgentConfig
    ) -> None:
        """Missing locks file returns empty dict."""
        reg = LockRegistry(lock_project, fast_config)
        assert reg._read_locks() == {}

    def test_malformed_lock_entry_cleaned(self, registry: LockRegistry) -> None:
        """Malformed entries are dropped during clean."""
        locks = {
            "/a.py": {"bad": "data"},  # No owner/acquired_at
        }
        cleaned = registry._clean_expired_locks(locks)
        assert len(cleaned) == 0

    def test_acquire_overwrites_malformed(self, registry: LockRegistry) -> None:
        """Acquire succeeds when existing entry is malformed."""
        registry._write_locks({"/src/main.py": {"bad": "data"}})
        assert registry.acquire("/src/main.py", "agent-1", sync_delay=0) is True


# ── LockRegistry: Dropbox Sync Delay ────────────────────────────────


class TestLockRegistrySync:
    @patch("file_locking.time.sleep")
    def test_sync_delay_called(
        self, mock_sleep, lock_project: Path
    ) -> None:
        """Sync delay is applied during acquire."""
        config = MultiAgentConfig(
            dropbox_sync_delay_seconds=5.0,
            lock_retry_attempts=1,
            lock_retry_delay_seconds=0,
        )
        reg = LockRegistry(lock_project, config)
        reg.acquire("/src/main.py", "agent-1")
        mock_sleep.assert_any_call(5.0)

    @patch("file_locking.time.sleep")
    def test_zero_sync_delay_no_sleep(
        self, mock_sleep, lock_project: Path
    ) -> None:
        """Zero sync delay skips sleep."""
        config = MultiAgentConfig(
            dropbox_sync_delay_seconds=0.0,
            lock_retry_attempts=1,
            lock_retry_delay_seconds=0,
        )
        reg = LockRegistry(lock_project, config)
        reg.acquire("/src/main.py", "agent-1", sync_delay=0)
        # Only retry delay sleep should be called, not sync delay
        for call in mock_sleep.call_args_list:
            assert call[0][0] == 0 or call[0][0] == 0.0


class TestLockRegistryRetry:
    @patch("file_locking.time.sleep")
    def test_retry_on_contested(
        self, mock_sleep, lock_project: Path
    ) -> None:
        """Contested lock retries before failing."""
        config = MultiAgentConfig(
            dropbox_sync_delay_seconds=0.0,
            lock_retry_attempts=3,
            lock_retry_delay_seconds=0.0,
        )
        reg = LockRegistry(lock_project, config)
        reg.acquire("/src/main.py", "agent-1", sync_delay=0)
        result = reg.acquire("/src/main.py", "agent-2", sync_delay=0)
        assert result is False


# ── LockRegistry: Path Normalization ────────────────────────────────


class TestLockRegistryPaths:
    def test_backslash_normalization(self, registry: LockRegistry) -> None:
        """Windows backslashes are normalized to forward slashes."""
        registry.acquire("C:\\src\\main.py", "agent-1", sync_delay=0)
        assert registry.is_locked_by_other("C:/src/main.py", "agent-2") is True

    def test_path_resolve(self, registry: LockRegistry) -> None:
        """Relative paths are resolved consistently."""
        p = registry._normalize_path("src/../src/main.py")
        assert ".." not in p


# ── LockRegistry: Verification Failure ──────────────────────────────


class TestLockRegistryVerification:
    def test_verification_clobber_fails(
        self, lock_project: Path
    ) -> None:
        """If another agent clobbers our lock between write and verify, acquire fails."""
        # Use 1 retry attempt so clobber on verification = immediate failure
        config = MultiAgentConfig(
            dropbox_sync_delay_seconds=0.0,
            lock_retry_attempts=1,
            lock_retry_delay_seconds=0.0,
            lock_ttl_seconds=60,
        )
        reg = LockRegistry(lock_project, config)
        original_read = reg._read_locks

        call_count = [0]

        def clobbering_read():
            call_count[0] += 1
            result = original_read()
            # On the verification read (2nd call), simulate clobber
            if call_count[0] >= 2:
                norm = reg._normalize_path("/src/main.py")
                result[norm] = {
                    "owner": "agent-2",
                    "acquired_at": datetime.now(timezone.utc).isoformat(),
                    "ttl_seconds": 60,
                }
            return result

        reg._read_locks = clobbering_read
        result = reg.acquire("/src/main.py", "agent-1", sync_delay=0)
        assert result is False


# ── FileManifest ────────────────────────────────────────────────────


class TestFileManifest:
    def test_load_empty(self, tmp_path: Path) -> None:
        """Missing manifest loads as empty set."""
        m = FileManifest(tmp_path / "assigned_files.txt")
        assert m.load() == set()

    def test_save_and_load(self, tmp_path: Path) -> None:
        """Save files and reload."""
        path = tmp_path / "assigned_files.txt"
        m = FileManifest(path)
        m.save(["src/a.py", "src/b.py"])
        m2 = FileManifest(path)
        files = m2.load()
        assert len(files) == 2

    def test_contains_present(self, tmp_path: Path) -> None:
        """contains() returns True for files in manifest."""
        path = tmp_path / "assigned_files.txt"
        m = FileManifest(path)
        # Write file paths that will resolve correctly
        abs_a = str(tmp_path / "src" / "a.py")
        m.save([abs_a])
        assert m.contains(abs_a) is True

    def test_contains_absent(self, tmp_path: Path) -> None:
        """contains() returns False for files not in manifest."""
        path = tmp_path / "assigned_files.txt"
        m = FileManifest(path)
        m.save(["src/a.py"])
        assert m.contains("src/z.py") is False

    def test_comments_ignored(self, tmp_path: Path) -> None:
        """Lines starting with # are ignored."""
        path = tmp_path / "assigned_files.txt"
        path.write_text("# Comment\nsrc/a.py\n", encoding="utf-8")
        m = FileManifest(path)
        files = m.load()
        assert len(files) == 1

    def test_blank_lines_ignored(self, tmp_path: Path) -> None:
        """Blank lines are ignored."""
        path = tmp_path / "assigned_files.txt"
        path.write_text("\nsrc/a.py\n\nsrc/b.py\n\n", encoding="utf-8")
        m = FileManifest(path)
        files = m.load()
        assert len(files) == 2

    def test_files_returns_copy(self, tmp_path: Path) -> None:
        """files() returns a copy, not the internal set."""
        path = tmp_path / "assigned_files.txt"
        m = FileManifest(path)
        m.save(["src/a.py"])
        f1 = m.files()
        f1.add("extra")
        assert "extra" not in m.files()

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        """save() creates parent directories if needed."""
        path = tmp_path / "deep" / "nested" / "assigned_files.txt"
        m = FileManifest(path)
        m.save(["a.py"])
        assert path.exists()

    def test_load_oserror_returns_empty(self, tmp_path: Path) -> None:
        """OSError during read returns empty set."""
        path = tmp_path / "assigned_files.txt"
        m = FileManifest(path)
        # Create a directory where a file is expected
        path.mkdir(parents=True)
        files = m.load()
        assert files == set()

    def test_contains_autoloads(self, tmp_path: Path) -> None:
        """contains() auto-loads the manifest if not yet loaded."""
        path = tmp_path / "assigned_files.txt"
        abs_a = str(tmp_path / "src" / "a.py")
        path.write_text(f"{abs_a}\n", encoding="utf-8")
        m = FileManifest(path)
        # Don't call load() explicitly
        assert m.contains(abs_a) is True


# ── LockRegistry: Multiple Files ────────────────────────────────────


class TestLockRegistryMultipleFiles:
    def test_multiple_files_same_agent(self, registry: LockRegistry) -> None:
        """Agent can lock multiple files."""
        assert registry.acquire("/a.py", "agent-1", sync_delay=0) is True
        assert registry.acquire("/b.py", "agent-1", sync_delay=0) is True
        assert registry.acquire("/c.py", "agent-1", sync_delay=0) is True
        locks = registry.get_locks()
        assert len(locks) == 3

    def test_different_agents_different_files(self, registry: LockRegistry) -> None:
        """Different agents can lock different files."""
        assert registry.acquire("/a.py", "agent-1", sync_delay=0) is True
        assert registry.acquire("/b.py", "agent-2", sync_delay=0) is True
        locks = registry.get_locks()
        assert len(locks) == 2

    def test_get_locks_returns_entries(self, registry: LockRegistry) -> None:
        """get_locks returns FileLockEntry instances."""
        registry.acquire("/a.py", "agent-1", sync_delay=0)
        locks = registry.get_locks()
        for entry in locks.values():
            assert isinstance(entry, FileLockEntry)


# ── LockRegistry: Atomic Write ──────────────────────────────────────


class TestLockRegistryAtomicWrite:
    def test_write_uses_replace(self, registry: LockRegistry) -> None:
        """_write_locks uses Path.replace() for atomic write."""
        registry._write_locks({"test": {"owner": "a", "acquired_at": "now", "ttl_seconds": 60}})
        assert registry.locks_path.exists()
        # tmp file should be cleaned up
        assert not registry.locks_path.with_suffix(".tmp").exists()

    def test_write_creates_directory(
        self, tmp_path: Path, fast_config: MultiAgentConfig
    ) -> None:
        """_write_locks creates parent directories if needed."""
        reg = LockRegistry(tmp_path / "new_project", fast_config)
        reg._write_locks({"test": {"owner": "a", "acquired_at": "now", "ttl_seconds": 60}})
        assert reg.locks_path.exists()


# ── Config Defaults ─────────────────────────────────────────────────


class TestMultiAgentConfig:
    def test_defaults(self) -> None:
        """MultiAgentConfig has sensible defaults."""
        c = MultiAgentConfig()
        assert c.enabled is False
        assert c.max_agents == 4
        assert c.dropbox_sync_delay_seconds == 5.0
        assert c.lock_ttl_seconds == 1800
        assert c.agent_state_dir == ".agents"

    def test_validation_bounds(self) -> None:
        """Config fields enforce bounds."""
        with pytest.raises(Exception):
            MultiAgentConfig(max_agents=0)
        with pytest.raises(Exception):
            MultiAgentConfig(max_agents=100)
        with pytest.raises(Exception):
            MultiAgentConfig(lock_ttl_seconds=10)  # Below 60

    def test_file_lock_entry(self) -> None:
        """FileLockEntry model validates."""
        e = FileLockEntry(
            owner="agent-1",
            acquired_at=datetime.now(timezone.utc).isoformat(),
        )
        assert e.ttl_seconds == 1800
        assert e.owner == "agent-1"
