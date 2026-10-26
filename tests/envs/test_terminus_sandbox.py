# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from terminus_env.server import hf_sandbox, local_sandbox


@pytest.fixture
def sandbox(monkeypatch):
    monkeypatch.setattr(local_sandbox.shutil, "which", lambda _: "/usr/bin/bwrap")
    sandbox = local_sandbox.LocalSandbox()
    yield sandbox
    sandbox.kill()


def test_local_timeout_returns_partial_output(sandbox, monkeypatch):
    monkeypatch.setattr(
        sandbox,
        "_bwrap_command",
        lambda _: [
            sys.executable,
            "-c",
            "import os,time; os.write(1,b'partial'); os.write(2,b'warning'); time.sleep(10)",
        ],
    )
    result = sandbox.run_shell("unused", timeout_s=0.2)
    assert not result.success
    assert result.stdout == "partial"
    assert result.stderr == "warning"
    assert "timed out" in result.error


def test_local_real_process_timeout(sandbox, monkeypatch):
    monkeypatch.setattr(
        sandbox,
        "_bwrap_command",
        lambda _: [sys.executable, "-c", "import time; time.sleep(5)"],
    )
    result = sandbox.run_shell("unused", timeout_s=0.05)
    assert not result.success
    assert "timed out" in result.error


def test_local_subprocess_does_not_inherit_host_environment(sandbox, monkeypatch):
    monkeypatch.setenv("TERMINUS_TEST_SECRET", "synthetic-canary")
    monkeypatch.setattr(
        sandbox,
        "_bwrap_command",
        lambda _: [
            sys.executable,
            "-c",
            "import os; assert 'TERMINUS_TEST_SECRET' not in os.environ",
        ],
    )
    assert sandbox.run_shell("unused").success


@pytest.mark.parametrize("value", [False, "false", "0", "off", "no", " FALSE "])
def test_hf_token_forwarding_false_values(value, monkeypatch):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(job_id="fake")

    monkeypatch.setattr(hf_sandbox, "Sandbox", SimpleNamespace(create=create))
    monkeypatch.setenv("HF_SANDBOX_FORWARD_HF_TOKEN", "true")
    hf_sandbox.HFSandbox(forward_hf_token=value)
    assert calls[0]["forward_hf_token"] is False


def test_default_dependency_configuration():
    import tomllib

    env_dir = Path(__file__).resolve().parents[2] / "envs" / "terminus_env"
    project = tomllib.loads((env_dir / "pyproject.toml").read_text())
    assert "hf-sandbox>=0.1.1" in project["project"]["dependencies"]
    assert "hf-sandbox>=0.1.1" in (env_dir / "server/requirements.txt").read_text()


@pytest.mark.parametrize("phase", ["setup", "terminal"])
def test_local_timeout_stays_in_environment_result(sandbox, monkeypatch, phase):
    from openenv.core.env_server.mcp_types import CallToolAction
    from terminus_env.server.terminus_env_environment import TerminusEnvironment

    monkeypatch.setattr(
        sandbox,
        "_bwrap_command",
        lambda command: [
            sys.executable,
            "-c",
            "import time; time.sleep(2)" if command == "hang" else "pass",
        ],
    )
    run_shell = sandbox.run_shell
    monkeypatch.setattr(
        sandbox,
        "run_shell",
        lambda command, **kwargs: run_shell(command, timeout_s=0.2),
    )
    monkeypatch.setattr(
        "terminus_env.server.terminus_env_environment.LocalSandbox",
        lambda **kwargs: sandbox,
    )
    env = TerminusEnvironment()
    observation = env.reset(
        sandbox_backend="local", setup=["hang"] if phase == "setup" else []
    )
    if phase == "setup":
        assert observation.done
        assert observation.metadata["status"] == "error"
        result = env.state.setup_results[0]
    else:
        env.step(CallToolAction(tool_name="terminal", arguments={"command": "hang"}))
        result = env.state.commands[0]
    assert not result.success
    assert "timed out" in result.error
