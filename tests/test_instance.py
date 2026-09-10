"""F-85: one instance per user account."""

from __future__ import annotations

import json
import os

import pytest

from echoact import instance, paths


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path))
    paths.data_dir.cache_clear()
    yield
    paths.data_dir.cache_clear()


def test_a_second_launch_finds_the_first(tmp_path) -> None:
    activations: list[int] = []
    first = instance.acquire(on_activate=lambda: activations.append(1))
    try:
        assert instance.notify_existing() is True
        with pytest.raises(instance.AlreadyRunning):
            instance.acquire()
        # The handler runs on the listener thread; give it a moment.
        for _ in range(200):
            if activations:
                break
            import time

            time.sleep(0.005)
        assert activations, "the running instance was not asked to show itself"
    finally:
        first.release()


def test_a_stale_lock_does_not_block_a_launch(tmp_path) -> None:
    """A forced termination leaves a lock behind. Refusing to start after a
    crash would be worse than the problem the lock solves."""
    paths.ensure_tree()
    paths.lock_path().write_text(
        json.dumps({"pid": 999999, "port": 1, "token": "nope"}), encoding="utf-8"
    )
    assert instance.notify_existing() is False
    lock = instance.acquire()
    try:
        assert lock.port > 0
        written = json.loads(paths.lock_path().read_text(encoding="utf-8"))
        assert written["pid"] == os.getpid()
    finally:
        lock.release()


def test_a_wrong_token_is_ignored_silently(tmp_path) -> None:
    """The activation port answers exactly one message and identifies
    itself to nobody else."""
    import socket

    lock = instance.acquire()
    try:
        with socket.create_connection(("127.0.0.1", lock.port), 1.5) as s:
            s.settimeout(1.0)
            s.sendall(b"echoact-activate wrong-token")
            try:
                reply = s.recv(16)
            except OSError:
                reply = b""
        assert reply == b""
    finally:
        lock.release()


def test_release_removes_the_lock_file(tmp_path) -> None:
    lock = instance.acquire()
    assert paths.lock_path().exists()
    lock.release()
    assert not paths.lock_path().exists()
    assert instance.notify_existing() is False


def test_release_leaves_a_later_instances_lock_alone(tmp_path) -> None:
    """Our own release must not delete a lock a successor already wrote."""
    lock = instance.acquire()
    paths.lock_path().write_text(
        json.dumps({"pid": os.getpid() + 1, "port": 2, "token": "other"}), encoding="utf-8"
    )
    lock.release()
    assert paths.lock_path().exists()
