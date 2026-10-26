# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""Real process tests; Linux bwrap tests require working namespaces."""

import importlib.util
import shutil
import sys
import time
from pathlib import Path

import pytest

# Load the dependency-free backend without importing optional environment providers.
_spec = importlib.util.spec_from_file_location(
    "terminus_local_processes_backend",
    Path(__file__).resolve().parents[2] / "envs/terminus_env/server/local_sandbox.py",
)
backend = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = backend
_spec.loader.exec_module(backend)


def test_bwrap_found_outside_sandbox_path(tmp_path, monkeypatch):
    executable = tmp_path / "bwrap"
    executable.write_text('#!/bin/sh\nprintf "%s" "$PATH"\n')
    executable.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", ".")
    sandbox = backend.LocalSandbox()
    try:
        monkeypatch.chdir("/")
        result = sandbox.run_shell("unused")
        assert result.success
        assert result.stdout == "/usr/local/bin:/usr/bin:/bin"
        assert Path(sandbox._bwrap_command("unused")[0]).is_absolute()
    finally:
        sandbox.kill()


def test_timeout_kills_real_descendants_with_open_pipes(tmp_path, monkeypatch):
    monkeypatch.setattr(backend.shutil, "which", lambda _: "/usr/bin/bwrap")
    sandbox = backend.LocalSandbox()
    marker = tmp_path / "survived"
    code = (
        "import os,time,pathlib; "
        "pid=os.fork(); "
        f"time.sleep(2) if pid == 0 else time.sleep(10); pathlib.Path({str(marker)!r}).touch()"
    )
    monkeypatch.setattr(
        sandbox, "_bwrap_command", lambda _: [sys.executable, "-c", code]
    )
    try:
        start = time.monotonic()
        result = sandbox.run_shell("unused", timeout_s=0.2)
        elapsed = time.monotonic() - start
        time.sleep(2.1)
        assert not marker.exists(), "descendant mutated state after timeout"
        assert elapsed < 1.5, "inherited output pipes delayed timeout cleanup"
        assert not result.success
        assert "timed out" in result.error
    finally:
        sandbox.kill()


@pytest.fixture
def real_bwrap():
    if sys.platform != "linux" or not shutil.which("bwrap"):
        pytest.skip("requires Linux and bubblewrap")
    sandbox = backend.LocalSandbox()
    try:
        probe = sandbox.run_shell("true")
        if not probe.success:
            pytest.fail(f"bwrap installed but runtime unavailable: {probe.stderr}")
        yield sandbox
    finally:
        sandbox.kill()


@pytest.mark.parametrize("parent_waits", [True, False])
@pytest.mark.parametrize("detach", [True, False])
def test_bwrap_descendant_lifetime(real_bwrap, parent_waits, detach):
    # Grandchild inherits output and tries a delayed write, even after setsid.
    launcher = "setsid " if detach else ""
    command = (
        "readlink /proc/self/ns/pid > /home/user/pid_namespace; "
        f"{launcher}bash -c '(sleep 2; touch /home/user/survived) & "
        "touch /home/user/started; wait' & "
        "while [ ! -e /home/user/started ]; do sleep 0.01; done; echo ready; "
        + ("wait" if parent_waits else "exit 0")
    )
    start = time.monotonic()
    result = real_bwrap.run_shell(command, timeout_s=0.3)
    elapsed = time.monotonic() - start
    time.sleep(2.1)
    assert not (real_bwrap._home / "survived").exists()
    namespace = (real_bwrap._home / "pid_namespace").read_text().strip()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            process_namespace = str((entry / "ns/pid").readlink())
        except (FileNotFoundError, PermissionError):
            continue
        assert process_namespace != namespace, "sandbox process survived cleanup"
    assert elapsed < 1.5
    assert "ready" in result.stdout
    if parent_waits:
        assert not result.success
        assert "timed out" in result.error
    else:
        assert result.success
    assert real_bwrap.run_shell("test ! -e /home/user/survived").success


def test_real_bwrap_preserves_scrubbing_and_shell_arguments(real_bwrap, monkeypatch):
    monkeypatch.setenv("TERMINUS_TEST_SECRET", "synthetic-canary")
    result = real_bwrap.run_shell(
        'test -z "${TERMINUS_TEST_SECRET+x}" && '
        "printf '%s' 'literal; touch /home/user/injected'"
    )
    assert result.success
    assert result.stdout == "literal; touch /home/user/injected"
    assert not (real_bwrap._home / "injected").exists()
