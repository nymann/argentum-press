"""Compile-verifier — wraps `./gradlew :mtg-sets:compileKotlin` in a project.

Returns a Result so the caller can decide what to do on failure. Today the
pipeline crashes loudly on Failed; the LLM repair turn will plug in here.

Argentum-engine's Gradle build needs JDK 21. We discover an OpenJDK 21 home
from common Homebrew layouts and pass it via JAVA_HOME unless the caller
overrides — the current shell may be on a different JDK without breaking us.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

GRADLE_TASK = ":mtg-sets:compileKotlin"


@dataclass(frozen=True, slots=True)
class CompileOk:
    pass


@dataclass(frozen=True, slots=True)
class CompileFail:
    exit_code: int
    stdout: str
    stderr: str


CompileResult = CompileOk | CompileFail


class GradleNotFoundError(RuntimeError):
    pass


class CompileVerifier:
    def __init__(
        self,
        project_dir: Path,
        *,
        gradle_task: str = GRADLE_TASK,
        java_home: str | None = None,
        timeout_seconds: float = 600.0,
    ) -> None:
        self.project_dir = project_dir
        self.gradle_task = gradle_task
        self.java_home = java_home or _discover_jdk21()
        self.timeout_seconds = timeout_seconds

    def verify(self) -> CompileResult:
        gradlew = self.project_dir / "gradlew"
        if not gradlew.exists():
            raise GradleNotFoundError(f"no gradlew at {gradlew}")
        env = dict(os.environ)
        if self.java_home:
            env["JAVA_HOME"] = self.java_home
        completed = subprocess.run(
            [str(gradlew), self.gradle_task],
            cwd=self.project_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode == 0:
            return CompileOk()
        return CompileFail(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def _discover_jdk21() -> str | None:
    """Best-effort search for an OpenJDK 21 install. Returns None if none found
    in the obvious places; callers can pass java_home explicitly to override."""
    candidates = [
        "/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home",
        "/usr/local/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    # Fall back to scanning Homebrew Cellar for any 21.x.
    for cellar in ("/opt/homebrew/Cellar/openjdk@21", "/usr/local/Cellar/openjdk@21"):
        cellar_path = Path(cellar)
        if cellar_path.is_dir():
            for child in sorted(cellar_path.iterdir(), reverse=True):
                home = child / "libexec" / "openjdk.jdk" / "Contents" / "Home"
                if home.exists():
                    return str(home)
    # As a last resort, see whether `java` on PATH is 21.
    java = shutil.which("java")
    if java:
        # We don't actually want to fork to probe; punt to the caller.
        return None
    return None
