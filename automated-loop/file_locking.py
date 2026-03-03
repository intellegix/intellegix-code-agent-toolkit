"""Dropbox-safe file locking for multi-agent orchestration.

Implements a write-wait-verify protocol that accounts for Dropbox sync delay:
1. Write lock entry to global_locks.json
2. Wait for sync delay (configurable, default 5s)
3. Re-read file and verify our lock is still present (no clobber)

Lock files are stored in .agents/shared/global_locks.json.
File manifests (assigned_files.txt) provide fast-path ownership checks.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import FileLockEntry, MultiAgentConfig

logger = logging.getLogger(__name__)


class LockRegistry:
    """Manages file locks across multiple agents using a shared JSON file.

    Uses a write-wait-verify protocol for Dropbox-safe lock acquisition:
    1. Read current locks, add ours, write back
    2. Sleep for sync delay
    3. Re-read and verify our lock persists (wasn't clobbered by another agent)
    """

    def __init__(
        self,
        project_path: str | Path,
        config: Optional[MultiAgentConfig] = None,
    ) -> None:
        self.project_path = Path(project_path)
        self.config = config or MultiAgentConfig()
        self.locks_dir = self.project_path / self.config.agent_state_dir / "shared"
        self.locks_path = self.locks_dir / "global_locks.json"

    def _read_locks(self) -> dict[str, dict]:
        """Read and return the current locks from disk. Returns empty dict on any error."""
        if not self.locks_path.exists():
            return {}
        try:
            raw = self.locks_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {}
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read locks file: %s", e)
            return {}

    def _write_locks(self, locks: dict[str, dict]) -> None:
        """Write locks to disk using atomic replace (Windows-safe)."""
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self.locks_path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(locks, indent=2), encoding="utf-8"
        )
        tmp_path.replace(self.locks_path)

    def _clean_expired_locks(self, locks: dict[str, dict]) -> dict[str, dict]:
        """Remove any expired locks based on TTL. Returns cleaned dict."""
        now = datetime.now(timezone.utc)
        cleaned: dict[str, dict] = {}
        for file_path, lock_data in locks.items():
            try:
                entry = FileLockEntry.model_validate(lock_data)
                acquired = datetime.fromisoformat(entry.acquired_at)
                if acquired.tzinfo is None:
                    acquired = acquired.replace(tzinfo=timezone.utc)
                elapsed = (now - acquired).total_seconds()
                if elapsed < entry.ttl_seconds:
                    cleaned[file_path] = lock_data
                else:
                    logger.info(
                        "Expired lock: %s owned by %s (%.0fs > %ds TTL)",
                        file_path, entry.owner, elapsed, entry.ttl_seconds,
                    )
            except Exception:
                # Malformed entry — drop it
                logger.warning("Dropping malformed lock entry for %s", file_path)
        return cleaned

    def _normalize_path(self, file_path: str | Path) -> str:
        """Normalize a file path for consistent lock keys."""
        return str(Path(file_path).resolve()).replace("\\", "/")

    def acquire(
        self,
        file_path: str | Path,
        agent_id: str,
        sync_delay: Optional[float] = None,
    ) -> bool:
        """Acquire a lock on a file using write-wait-verify protocol.

        Returns True if lock acquired, False if contested by another agent.
        Retries according to config.lock_retry_attempts.
        """
        normalized = self._normalize_path(file_path)
        delay = sync_delay if sync_delay is not None else self.config.dropbox_sync_delay_seconds

        for attempt in range(1, self.config.lock_retry_attempts + 1):
            # Step 1: Read, clean expired, check if available
            locks = self._read_locks()
            locks = self._clean_expired_locks(locks)

            existing = locks.get(normalized)
            if existing:
                try:
                    entry = FileLockEntry.model_validate(existing)
                    if entry.owner == agent_id:
                        return True  # Already own it
                    if attempt < self.config.lock_retry_attempts:
                        logger.info(
                            "Lock contested: %s held by %s (attempt %d/%d)",
                            normalized, entry.owner, attempt, self.config.lock_retry_attempts,
                        )
                        time.sleep(self.config.lock_retry_delay_seconds)
                        continue
                    return False  # All retries exhausted
                except Exception:
                    pass  # Malformed entry, overwrite it

            # Step 2: Write our lock
            lock_entry = FileLockEntry(
                owner=agent_id,
                acquired_at=datetime.now(timezone.utc).isoformat(),
                ttl_seconds=self.config.lock_ttl_seconds,
            )
            locks[normalized] = lock_entry.model_dump()
            self._write_locks(locks)

            # Step 3: Wait for Dropbox sync
            if delay > 0:
                time.sleep(delay)

            # Step 4: Verify — re-read and confirm our lock is still there
            locks = self._read_locks()
            verified = locks.get(normalized)
            if verified:
                try:
                    entry = FileLockEntry.model_validate(verified)
                    if entry.owner == agent_id:
                        logger.info("Lock acquired: %s by %s", normalized, agent_id)
                        return True
                except Exception:
                    pass

            # Verification failed — another agent clobbered our write
            if attempt < self.config.lock_retry_attempts:
                logger.warning(
                    "Lock verification failed for %s (attempt %d/%d), retrying",
                    normalized, attempt, self.config.lock_retry_attempts,
                )
                time.sleep(self.config.lock_retry_delay_seconds)
            else:
                logger.error(
                    "Lock acquisition failed for %s after %d attempts",
                    normalized, self.config.lock_retry_attempts,
                )

        return False

    def release(self, file_path: str | Path, agent_id: str) -> bool:
        """Release a lock on a file. Only the owning agent can release.

        Returns True if released, False if not owned by this agent.
        """
        normalized = self._normalize_path(file_path)
        locks = self._read_locks()

        existing = locks.get(normalized)
        if not existing:
            return True  # No lock to release

        try:
            entry = FileLockEntry.model_validate(existing)
            if entry.owner != agent_id:
                logger.warning(
                    "Cannot release lock on %s: owned by %s, not %s",
                    normalized, entry.owner, agent_id,
                )
                return False
        except Exception:
            pass  # Malformed — remove it anyway

        del locks[normalized]
        self._write_locks(locks)
        logger.info("Lock released: %s by %s", normalized, agent_id)
        return True

    def release_all(self, agent_id: str) -> int:
        """Release all locks held by a specific agent. Returns count released."""
        locks = self._read_locks()
        to_remove = []
        for file_path, lock_data in locks.items():
            try:
                entry = FileLockEntry.model_validate(lock_data)
                if entry.owner == agent_id:
                    to_remove.append(file_path)
            except Exception:
                to_remove.append(file_path)  # Malformed — clean up

        for fp in to_remove:
            del locks[fp]

        if to_remove:
            self._write_locks(locks)
            logger.info("Released %d locks for %s", len(to_remove), agent_id)
        return len(to_remove)

    def get_locks(self) -> dict[str, FileLockEntry]:
        """Return all currently active (non-expired) locks."""
        locks = self._read_locks()
        locks = self._clean_expired_locks(locks)
        result: dict[str, FileLockEntry] = {}
        for fp, data in locks.items():
            try:
                result[fp] = FileLockEntry.model_validate(data)
            except Exception:
                pass
        return result

    def is_locked_by_other(self, file_path: str | Path, agent_id: str) -> bool:
        """Check if a file is locked by a different agent."""
        normalized = self._normalize_path(file_path)
        locks = self._read_locks()
        locks = self._clean_expired_locks(locks)
        existing = locks.get(normalized)
        if not existing:
            return False
        try:
            entry = FileLockEntry.model_validate(existing)
            return entry.owner != agent_id
        except Exception:
            return False


class FileManifest:
    """Manages the list of files assigned to an agent.

    Each agent has an assigned_files.txt in its workspace directory.
    Provides fast-path ownership checks without hitting global_locks.json.
    """

    def __init__(self, manifest_path: str | Path) -> None:
        self.manifest_path = Path(manifest_path)
        self._files: Optional[set[str]] = None

    def _normalize(self, file_path: str | Path) -> str:
        """Normalize a path for consistent comparison."""
        return str(Path(file_path).resolve()).replace("\\", "/")

    def load(self) -> set[str]:
        """Load and return the set of assigned file paths."""
        if not self.manifest_path.exists():
            self._files = set()
            return self._files
        try:
            raw = self.manifest_path.read_text(encoding="utf-8")
            self._files = {
                self._normalize(line.strip())
                for line in raw.splitlines()
                if line.strip() and not line.strip().startswith("#")
            }
        except OSError as e:
            logger.warning("Failed to read manifest: %s", e)
            self._files = set()
        return self._files

    def contains(self, file_path: str | Path) -> bool:
        """Check if a file is in the manifest."""
        if self._files is None:
            self.load()
        return self._normalize(file_path) in self._files

    def save(self, files: set[str] | list[str]) -> None:
        """Write a set of file paths to the manifest."""
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        lines = sorted(files)
        self.manifest_path.write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        self._files = {self._normalize(f) for f in files}

    def files(self) -> set[str]:
        """Return the current set of assigned files."""
        if self._files is None:
            self.load()
        return self._files.copy()
