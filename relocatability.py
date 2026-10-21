"""Check that an installed CPython still works after it has been moved."""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from typing import Any, TextIO


STDLIB_PROBES = {
    "bz2": "assert bz2.decompress(bz2.compress(b'relocatable')) == b'relocatable'",
    "ctypes": "assert ctypes.pythonapi is not None",
    # Not hashlib, which quietly falls back to the builtin _sha2 module.
    "_hashlib": "assert _hashlib.openssl_sha256(b'relocatable').hexdigest()",
    "lzma": "assert lzma.decompress(lzma.compress(b'relocatable')) == b'relocatable'",
    "sqlite3": (
        "connection = sqlite3.connect(':memory:'); "
        "assert connection.execute('select 40 + 2').fetchone() == (42,); "
        "connection.close()"
    ),
    "ssl": "ssl.create_default_context()",
    "zlib": "assert zlib.decompress(zlib.compress(b'relocatable')) == b'relocatable'",
}

# ANSI colors for each status in human-readable output.
STATUS_COLORS = {"PASS": "\033[32m", "FAIL": "\033[31m", "SKIP": "\033[33m"}
RESET_COLOR = "\033[0m"

# Seconds to wait for each probe.
TIMEOUT = 30

STARTUP_PROBE = r"""
import json
import os
import sys
import sysconfig

keys = (
    "BINDIR", "BINLIBDEST", "CONFINCLUDEPY", "INCLUDEPY", "LIBDEST",
    "LIBDIR", "LIBPL", "prefix", "exec_prefix",
)
data = {
    "executable": sys.executable,
    "prefix": sys.prefix,
    "exec_prefix": sys.exec_prefix,
    "base_prefix": sys.base_prefix,
    "base_exec_prefix": sys.base_exec_prefix,
    "path": sys.path,
    "config_paths": sysconfig.get_paths(),
    "config_vars": {key: sysconfig.get_config_var(key) for key in keys},
}
print(json.dumps(data, sort_keys=True))
"""


@dataclasses.dataclass(slots=True)
class CheckResult:
    """The result of a single check."""

    name: str
    status: str
    summary: str
    details: list[str] = dataclasses.field(default_factory=list)


class CheckFailed(Exception):
    """A check failed."""

    def __init__(self, summary: str, details: Iterable[str] = ()) -> None:
        super().__init__(summary)
        self.summary = summary
        self.details = list(details)


class CheckSkipped(Exception):
    """A check couldn't run."""


def sanitized_environment() -> dict[str, str]:
    """Return os.environ minus variables that affect Python or the dynamic loader."""
    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("PYTHON", "LD_", "DYLD_"))
    }


def build_environment(build_dir: pathlib.Path) -> dict[str, str]:
    """Return sanitized_environment() plus what *build_dir*'s interpreter needs.

    An --enable-shared build finds libpython in the build directory through
    the Makefile's RUNSHARED (for example, LD_LIBRARY_PATH=<build dir>).
    """
    env = sanitized_environment()
    makefile = build_dir / "Makefile"
    for line in makefile.read_text(encoding="utf-8").splitlines():
        if match := re.fullmatch(r"RUNSHARED=\s*(\w+)=(.+?)\s*", line):
            env[match.group(1)] = match.group(2)
            break
    return env


def path_is_within(path: os.PathLike[str] | str, root: pathlib.Path) -> bool:
    """Return whether *path* resolves inside *root*."""
    try:
        pathlib.Path(path).resolve(strict=False).relative_to(root.resolve())
    except ValueError:
        return False
    return True


def find_interpreter(root: pathlib.Path) -> pathlib.Path:
    """Return the bin/python3 executable installed below *root*."""
    python = root / "bin" / "python3"
    if not python.is_file():
        raise CheckFailed("no installed Python executable found", [os.fspath(python)])
    return python


def find_build_interpreter(build_dir: pathlib.Path) -> pathlib.Path:
    """Return the interpreter built in *build_dir*.

    It's python.exe on case-insensitive file systems, such as macOS's default.
    """
    for name in ("python", "python.exe"):
        python = build_dir / name
        if python.is_file():
            return python
    raise CheckFailed("no Python executable found", [os.fspath(build_dir)])


def broken_stdlib_modules(build_dir: pathlib.Path, cwd: pathlib.Path) -> dict[str, str]:
    """Return the STDLIB_PROBES modules that fail in the build directory.

    Running the build directory's interpreter means that anything broken by
    `make install`, patching, or the move is still reported as a failure.
    Each module maps to the last line of its error output.
    """
    python = find_build_interpreter(build_dir)
    env = build_environment(build_dir)
    startup = subprocess.run(
        [python, "-I", "-c", "pass"],
        cwd=cwd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=TIMEOUT,
    )
    if startup.returncode:
        raise CheckFailed("the interpreter in the build directory did not run")
    broken = {}
    for module, probe in STDLIB_PROBES.items():
        try:
            completed = subprocess.run(
                [python, "-I", "-c", f"import {module}; {probe}"],
                cwd=cwd,
                env=env,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            broken[module] = f"timed out after {TIMEOUT}s"
            continue
        if completed.returncode:
            lines = completed.stderr.strip().splitlines() or [
                f"exit status {completed.returncode}"
            ]
            broken[module] = lines[-1]
    return broken


class RelocatabilityHarness:
    """Checks for a CPython install that has already been moved."""

    root: pathlib.Path
    original_prefix: pathlib.Path
    # --prefix of a build not configured by relocatable.py, if any.
    configured_prefix: pathlib.PurePath | None
    source_dir: pathlib.Path
    build_dir: pathlib.Path
    work_dir: pathlib.Path
    full: bool
    broken_modules: dict[str, str] | None
    env: dict[str, str]
    results: list[CheckResult]
    python: pathlib.Path | None
    snapshot: dict[str, Any] | None

    def __init__(
        self,
        root: pathlib.Path,
        original_prefix: pathlib.Path,
        source_dir: pathlib.Path,
        build_dir: pathlib.Path,
        work_dir: pathlib.Path,
        *,
        full: bool = False,
        broken_modules: dict[str, str] | None = None,
        configured_prefix: pathlib.PurePath | None = None,
    ) -> None:
        self.root = root.resolve()
        self.original_prefix = original_prefix.resolve()
        self.configured_prefix = configured_prefix
        self.source_dir = source_dir.resolve()
        self.build_dir = build_dir.resolve()
        self.work_dir = work_dir.resolve()
        self.full = full
        self.broken_modules = broken_modules
        self.env = sanitized_environment()
        self.results = []
        self.python = None
        self.snapshot = None

    def run(self) -> list[CheckResult]:
        """Run all the checks and return their results."""
        self._check("runtime.startup", self._check_startup)
        self._check("runtime.paths", self._check_runtime_paths)
        self._check("metadata.prefix-leaks", self._check_prefix_leaks)
        for module in STDLIB_PROBES:
            self._check(
                f"stdlib.{module}",
                lambda module=module: self._check_stdlib(module),
            )
        self._check("runtime.symlink", self._check_symlink)
        self._check("runtime.venv", self._check_venv)
        if self.full:
            self._check("stdlib.full-suite", self._check_full_suite)
        return self.results

    def _check(self, name: str, check: Callable[[], str]) -> None:
        try:
            summary = check()
        except CheckSkipped as exc:
            self.results.append(CheckResult(name, "SKIP", str(exc)))
        except CheckFailed as exc:
            self.results.append(CheckResult(name, "FAIL", exc.summary, exc.details))
        except Exception as exc:
            self.results.append(
                CheckResult(name, "FAIL", f"unexpected {type(exc).__name__}: {exc}")
            )
        else:
            self.results.append(CheckResult(name, "PASS", summary))

    def _stale_prefixes(self) -> dict[str, str]:
        """Return the old prefixes that shouldn't show up after the move."""
        prefixes = {
            os.fspath(self.original_prefix): "original installation prefix",
            "/the/knights/who/say/ni": "configure placeholder prefix",
        }
        if self.configured_prefix is not None:
            prefixes[os.fspath(self.configured_prefix)] = "configured prefix"
        return prefixes

    def _is_verbatim_source_copy(self, path: pathlib.Path) -> bool:
        """Return whether *path* is an unmodified copy of a Lib/ source file.

        These can't know where the build was installed, so something like
        /usr/local showing up in one (say, in a docstring) isn't a leak.
        """
        for libdest in self.root.glob("lib/python3*"):
            try:
                relative = path.relative_to(libdest)
            except ValueError:
                continue
            source = self.source_dir / "Lib" / relative
            try:
                return source.read_bytes() == path.read_bytes()
            except OSError:
                return False
        return False

    def _run(
        self,
        command: list[str | os.PathLike[str]],
        *,
        cwd: pathlib.Path | None = None,
        timeout: int = TIMEOUT,
    ) -> subprocess.CompletedProcess[str]:
        args = [os.fspath(part) for part in command]
        try:
            return subprocess.run(
                args,
                cwd=cwd or self.work_dir,
                env=self.env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CheckFailed(
                f"timed out after {timeout}s", [f"command: {shlex.join(args)}"]
            ) from exc

    def _run_python(
        self,
        args: list[str],
        *,
        cwd: pathlib.Path | None = None,
        timeout: int = TIMEOUT,
    ) -> subprocess.CompletedProcess[str]:
        if self.python is None:
            raise CheckSkipped("the relocated interpreter did not start")
        return self._run([self.python, *args], cwd=cwd, timeout=timeout)

    @staticmethod
    def _require_success(
        completed: subprocess.CompletedProcess[str], summary: str
    ) -> None:
        if completed.returncode:
            details = [f"command: {shlex.join(completed.args)}"]
            if completed.stdout.strip():
                details.append(f"stdout:\n{completed.stdout.rstrip()}")
            if completed.stderr.strip():
                details.append(f"stderr:\n{completed.stderr.rstrip()}")
            raise CheckFailed(summary, details)

    def _snapshot(self, python: pathlib.Path) -> dict[str, Any]:
        """Run STARTUP_PROBE with *python* and return its output."""
        completed = self._run([python, "-I", "-c", STARTUP_PROBE])
        self._require_success(completed, "the relocated interpreter did not start")
        try:
            return json.loads(completed.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise CheckFailed(
                "the startup probe did not produce valid JSON",
                [completed.stdout.rstrip(), completed.stderr.rstrip()],
            ) from exc

    def _path_failures(self, snapshot: dict[str, Any]) -> list[str]:
        """Return the paths in *snapshot* that are outside the moved tree."""
        failures: list[str] = []
        scalar_names = (
            "executable",
            "prefix",
            "exec_prefix",
            "base_prefix",
            "base_exec_prefix",
        )
        for name in scalar_names:
            if name not in snapshot:
                continue
            value = snapshot[name]
            if not isinstance(value, str) or not path_is_within(value, self.root):
                failures.append(f"{name}={value!r}")

        for name, value in snapshot["config_paths"].items():
            if isinstance(value, str) and os.path.isabs(value):
                if not path_is_within(value, self.root):
                    failures.append(f"sysconfig path {name}={value!r}")

        for name, value in snapshot["config_vars"].items():
            if isinstance(value, str) and os.path.isabs(value):
                if not path_is_within(value, self.root):
                    failures.append(f"sysconfig variable {name}={value!r}")

        for value in snapshot["path"]:
            if value and os.path.isabs(value) and not path_is_within(value, self.root):
                failures.append(f"sys.path entry {value!r}")
        return failures

    def _check_startup(self) -> str:
        self.python = find_interpreter(self.root)
        self.snapshot = self._snapshot(self.python)
        return os.fspath(self.python)

    def _check_runtime_paths(self) -> str:
        if self.snapshot is None:
            raise CheckSkipped("the startup probe did not complete")
        failures = self._path_failures(self.snapshot)
        if failures:
            raise CheckFailed(
                f"{len(failures)} runtime path(s) escape the relocated tree",
                failures,
            )
        return "runtime and sysconfig paths resolve inside the relocated tree"

    def _check_prefix_leaks(self) -> str:
        binary_needles = {
            os.fsencode(prefix): label
            for prefix, label in self._stale_prefixes().items()
        }
        text_needles = dict(binary_needles)
        text_needles[os.fsencode(self.source_dir)] = "CPython source directory"
        text_needles[os.fsencode(self.build_dir)] = "CPython build directory"
        failures: list[str] = []
        leaking: set[pathlib.Path] = set()
        files_checked = 0
        for path in self.root.walk():
            root, _, filenames = path
            for filename in filenames:
                candidate = root / filename
                if candidate.is_symlink():
                    target = os.readlink(candidate)
                    if not candidate.exists():
                        failures.append(f"{candidate}: broken symlink to {target!r}")
                        leaking.add(candidate)
                    for needle, label in text_needles.items():
                        if os.fsdecode(needle) in target:
                            failures.append(f"{candidate}: symlink contains {label}")
                            leaking.add(candidate)
                    continue
                if candidate.suffix in {".a", ".pyc"}:
                    continue
                files_checked += 1
                try:
                    contents = candidate.read_bytes()
                except OSError as exc:
                    failures.append(f"{candidate}: could not read: {exc}")
                    leaking.add(candidate)
                    continue
                needles = binary_needles if b"\0" in contents[:8192] else text_needles
                found = [label for needle, label in needles.items() if needle in contents]
                if found == ["configured prefix"] and self._is_verbatim_source_copy(
                    candidate
                ):
                    continue
                failures.extend(f"{candidate}: contains {label}" for label in found)
                if found:
                    leaking.add(candidate)
        if failures:
            shown = failures[:50]
            if len(failures) > len(shown):
                shown.append(f"... and {len(failures) - len(shown)} more")
            raise CheckFailed(
                f"{len(leaking)} installed file(s) retain a pre-move prefix",
                shown,
            )
        return f"scanned {files_checked} installed files"

    def _check_stdlib(self, module: str) -> str:
        if self.broken_modules is not None and module in self.broken_modules:
            raise CheckSkipped(
                f"already fails in the build directory: {self.broken_modules[module]}"
            )
        probe = f"import {module}; {STDLIB_PROBES[module]}"
        completed = self._run_python(["-I", "-c", probe])
        self._require_success(completed, f"{module} failed after relocation")
        return f"{module} works after relocation"

    def _check_symlink(self) -> str:
        if self.snapshot is None:
            raise CheckSkipped("the startup probe did not complete")
        link = self.work_dir / "python-through-symlink"
        link.unlink(missing_ok=True)
        link.symlink_to(self.python)
        snapshot = self._snapshot(link)
        # sys.executable is the symlink itself, which is outside the tree.
        del snapshot["executable"]
        # runtime.paths already reports paths that are wrong either way.
        known = set(self._path_failures(self.snapshot))
        failures = [f for f in self._path_failures(snapshot) if f not in known]
        if failures:
            raise CheckFailed(
                f"{len(failures)} path(s) escape the relocated tree "
                "only when run through a symlink",
                failures,
            )
        return "paths resolve inside the relocated tree when run through a symlink"

    def _check_venv(self) -> str:
        venv = self.work_dir / "relocated-venv"
        shutil.rmtree(venv, ignore_errors=True)
        completed = self._run_python(
            ["-I", "-m", "venv", "--without-pip", os.fspath(venv)]
        )
        self._require_success(completed, "venv creation failed")
        venv_python = venv / "bin" / "python"
        probe = "import sys; print(sys.prefix); print(sys.base_prefix)"
        completed = self._run([venv_python, "-I", "-c", probe])
        self._require_success(completed, "the relocated venv interpreter failed")
        lines = completed.stdout.strip().splitlines()
        failures = []
        if len(lines) != 2:
            failures.append(f"unexpected output: {completed.stdout!r}")
        else:
            if not path_is_within(lines[0], venv):
                failures.append(f"venv sys.prefix={lines[0]!r}")
            if not path_is_within(lines[1], self.root):
                failures.append(f"venv sys.base_prefix={lines[1]!r}")
        if failures:
            raise CheckFailed("the venv contains stale paths", failures)
        return "a venv created by the relocated interpreter works"

    def _check_full_suite(self) -> str:
        completed = self._run_python(
            ["-I", "-m", "test", "--fast-ci", "--timeout=900"],
            timeout=3600,
        )
        self._require_success(completed, "the CPython test suite failed")
        return "python -m test --fast-ci passed"


def use_color(stream: TextIO) -> bool:
    """Return whether to color output written to *stream*.

    Follows https://no-color.org and leaves dumb terminals alone.
    """
    return (
        stream.isatty()
        and not os.environ.get("NO_COLOR")
        and os.environ.get("TERM") != "dumb"
    )


def render_results(
    results: list[CheckResult], output_format: str, *, color: bool = False
) -> str:
    """Format the results as text or JSON; *color* only affects text."""
    if output_format == "json":
        return json.dumps(list(map(dataclasses.asdict, results)), indent=2)

    lines = []
    for result in results:
        status = f"{result.status:4}"
        if color:
            status = f"{STATUS_COLORS[result.status]}{status}{RESET_COLOR}"
        lines.append(f"{status} {result.name}: {result.summary}")
        lines.extend(f"     {line}" for line in result.details)
    counts = {
        status: sum(result.status == status for result in results)
        for status in ("PASS", "FAIL", "SKIP")
    }
    lines.append(
        "Summary: "
        f"{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped"
    )
    return "\n".join(lines)


def run_in_temporary_directory(
    callback: Callable[[pathlib.Path], list[CheckResult]],
    parent: pathlib.Path | None,
) -> tuple[list[CheckResult], pathlib.Path | None]:
    """Run *callback* in a temporary directory, kept only if *parent* is given."""
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
        path = pathlib.Path(tempfile.mkdtemp(prefix="relocatability-", dir=parent))
        return callback(path), path

    with tempfile.TemporaryDirectory(prefix="cpython-relocatability-") as name:
        return callback(pathlib.Path(name)), None
