from __future__ import annotations

import os
from types import SimpleNamespace

from hermes_cli import env_loader


def test_apply_macos_system_proxy_reads_enabled_http_https(monkeypatch):
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.setattr(env_loader.sys, "platform", "darwin")

    sample = """<dictionary> {
  HTTPEnable : 1
  HTTPProxy : 127.0.0.1
  HTTPPort : 7897
  HTTPSEnable : 1
  HTTPSProxy : 127.0.0.1
  HTTPSPort : 7897
}"""

    def fake_run(args, **kwargs):
        assert args == ["scutil", "--proxy"]
        return SimpleNamespace(returncode=0, stdout=sample)

    monkeypatch.setattr("subprocess.run", fake_run)
    env_loader._apply_macos_system_proxy()

    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:7897"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7897"


def test_apply_macos_system_proxy_keeps_explicit_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://explicit.example:8888")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.setattr(env_loader.sys, "platform", "darwin")

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("explicit proxy env should skip scutil lookup")

    monkeypatch.setattr("subprocess.run", should_not_run)
    env_loader._apply_macos_system_proxy()

    assert os.environ["HTTP_PROXY"] == "http://explicit.example:8888"
    assert "HTTPS_PROXY" not in os.environ


def test_apply_macos_system_proxy_noops_off_macos(monkeypatch):
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.setattr(env_loader.sys, "platform", "linux")
    env_loader._apply_macos_system_proxy()
    assert "HTTP_PROXY" not in os.environ
    assert "HTTPS_PROXY" not in os.environ
