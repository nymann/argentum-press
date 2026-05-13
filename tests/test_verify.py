"""Verifier tests use a fake gradlew script so they don't need a real JDK
or argentum-engine checkout."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from argentum_press.verify import (
    CompileFail,
    CompileOk,
    CompileVerifier,
    GradleNotFoundError,
)


def _fake_gradle_project(tmp_path: Path, *, exit_code: int, stderr: str = "") -> Path:
    gradlew = tmp_path / "gradlew"
    script = f'#!/bin/sh\necho stdout-line\necho "{stderr}" 1>&2\nexit {exit_code}\n'
    gradlew.write_text(script)
    gradlew.chmod(gradlew.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return tmp_path


def test_zero_exit_yields_compile_ok(tmp_path: Path) -> None:
    project = _fake_gradle_project(tmp_path, exit_code=0)
    verifier = CompileVerifier(project, java_home=None)
    result = verifier.verify()
    assert isinstance(result, CompileOk)


def test_nonzero_exit_yields_compile_fail_with_streams(tmp_path: Path) -> None:
    project = _fake_gradle_project(tmp_path, exit_code=2, stderr="unresolved: nonsense")
    verifier = CompileVerifier(project, java_home=None)
    result = verifier.verify()
    assert isinstance(result, CompileFail)
    assert result.exit_code == 2
    assert "stdout-line" in result.stdout
    assert "unresolved: nonsense" in result.stderr


def test_missing_gradlew_raises(tmp_path: Path) -> None:
    verifier = CompileVerifier(tmp_path, java_home=None)
    with pytest.raises(GradleNotFoundError):
        verifier.verify()
