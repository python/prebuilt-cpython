"""Make a relocatable build of CPython.

This script is meant to serve two purposes:

1. Test/show different approaches to making a relocatable build of CPython.
2. Provide an easy way to test a relocatable build so fixes can get upstreamed.
"""

# /// script
# requires-python = ">=3.14"
# ///

import argparse
import dataclasses
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from typing import TextIO

from fixup import PLACEHOLDER
from relocatability import (
    CheckFailed,
    CheckResult,
    RelocatabilityHarness,
    broken_stdlib_modules,
    render_results,
    use_color,
    run_in_temporary_directory,
)


FIXUP_SCRIPT = pathlib.Path(__file__).with_name("fixup.py")


@dataclasses.dataclass(frozen=True)
class Dependency:
    """A third-party library that a stdlib extension module needs."""

    name: str
    version: str
    sha256: str
    # Commands run in the unpacked source directory. "{prefix}" and "{jobs}"
    # are replaced with the install directory and the number of make jobs.
    commands: tuple[tuple[str, ...], ...]

    @property
    def basename(self) -> str:
        return f"{self.name}-{self.version}"

    @property
    def url(self) -> str:
        return f"{SOURCE_DEPS_URL}/archive/refs/tags/{self.basename}.tar.gz"


# Libraries for the extension modules that macOS doesn't provide (_ssl,
# _hashlib, _lzma, _zstd and _decimal). Only static libraries are installed,
# so the extension modules don't link against anything outside the
# installation.
# The sources are tags of SOURCE_DEPS_URL, which CPython's Windows build also
# uses. The checksums are of GitHub's archives of those tags.
SOURCE_DEPS_URL = "https://github.com/python/cpython-source-deps"
DEPENDENCIES = (
    Dependency(
        "openssl",
        "3.5.9",
        "d6cf0d9a651f25fe506f6b3dc133a80b9b79a0bc565f570dcf8fdbd597551485",
        (
            (
                "./Configure",
                "no-shared",
                "no-tests",
                "-fPIC",
                "--prefix={prefix}",
                "--libdir=lib",
                "--openssldir={prefix}/ssl",
            ),
            ("make", "-j{jobs}"),
            ("make", "install_sw"),
        ),
    ),
    Dependency(
        "xz",
        # The newer xz tags only have what the Windows build needs.
        "5.2.5",
        "a15c168e39e87d750c3dc766edc7f19bdda57dacf01e509678467eace91ad282",
        (
            # This tag's scripts aren't marked executable.
            ("sh", "-c", "chmod +x configure build-aux/*"),
            (
                "./configure",
                "--prefix={prefix}",
                "--disable-shared",
                "--with-pic",
                "--disable-dependency-tracking",
                "--disable-xz",
                "--disable-xzdec",
                "--disable-lzmadec",
                "--disable-lzmainfo",
                "--disable-lzma-links",
                "--disable-scripts",
                "--disable-doc",
            ),
            ("make", "-j{jobs}"),
            ("make", "install"),
        ),
    ),
    Dependency(
        "zstd",
        "1.5.7",
        "f24b52470d12f466e9fa4fcc94e6c530625ada51d7b36de7fdc6ed7e6f499c8e",
        (
            (
                "make",
                "-C",
                "lib",
                "-j{jobs}",
                "install-static",
                "install-includes",
                "install-pc",
                "PREFIX={prefix}",
            ),
        ),
    ),
    Dependency(
        "mpdecimal",
        "4.0.0",
        "338fac3fb8cdd60f406b6326431338756f58a8af94229ffd9bf1e7c2b1ad71ca",
        (
            ("./configure", "--prefix={prefix}", "--disable-cxx"),
            ("make", "-j{jobs}"),
            ("make", "install"),
        ),
    ),
)


def placeholder_prefix() -> str:
    """Return the placeholder prefix used when building CPython."""
    return PLACEHOLDER


def configured_prefix(build_dir: pathlib.Path) -> str:
    """Return the `--prefix` the build directory was configured with."""
    makefile = build_dir / "Makefile"
    for line in makefile.read_text(encoding="utf-8").splitlines():
        if match := re.fullmatch(r"prefix=\s*(\S.*?)\s*", line):
            return match.group(1)
    raise ValueError(f"no prefix found in {makefile}")


def set_relative_rpath(output_dir: pathlib.Path) -> None:
    """Make installed ELF files load libraries relative to themselves."""
    lib_dir = output_dir / "lib"
    for root, _, filenames in output_dir.walk():
        for filename in filenames:
            path = root / filename
            if path.is_symlink():
                continue
            with path.open("rb") as file:
                header = file.read(18)
            if len(header) < 18 or header[:4] != b"\x7fELF":
                continue
            # Only executables and shared objects (ET_EXEC and ET_DYN) have an
            # rpath; patchelf rejects relocatable objects such as python.o.
            byteorder = "little" if header[5] == 1 else "big"
            if int.from_bytes(header[16:18], byteorder) not in (2, 3):
                continue
            relative_lib = os.path.relpath(lib_dir, path.parent)
            rpath = "$ORIGIN"
            if relative_lib != ".":
                rpath = f"{rpath}/{relative_lib}"
            subprocess.run(
                ["patchelf", "--force-rpath", "--set-rpath", rpath, path],
                check=True,
            )


def download(url: str, sha256: str, downloads_dir: pathlib.Path) -> pathlib.Path:
    """Download *url* into *downloads_dir* unless it's already there."""
    path = downloads_dir / url.rpartition("/")[2]
    if not path.exists():
        downloads_dir.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".partial")
        with (
            urllib.request.urlopen(url, timeout=60) as response,
            partial.open("wb") as file,
        ):
            shutil.copyfileobj(response, file)
        partial.rename(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != sha256:
        raise ValueError(f"{path} has SHA-256 {digest}, expected {sha256}")
    return path


def prepend_flags(flags: str, variable: str) -> str:
    """Put *flags* before the value of the environment *variable*."""
    return " ".join(filter(None, [flags, os.environ.get(variable, "").strip()]))


def run_deps(deps_dir: pathlib.Path) -> None:
    """Build the third-party libraries in DEPENDENCIES into *deps_dir*."""
    deps_dir = deps_dir.resolve()
    install_dir = deps_dir / "install"
    jobs = os.process_cpu_count() or 1
    env = os.environ | {"CFLAGS": prepend_flags("-O2 -fPIC", "CFLAGS")}
    for dependency in DEPENDENCIES:
        stamp = install_dir / f".{dependency.basename}.built"
        if stamp.exists():
            continue
        archive = download(dependency.url, dependency.sha256, deps_dir / "downloads")
        source_dir = deps_dir / "src" / dependency.basename
        if source_dir.exists():
            shutil.rmtree(source_dir)
        with tarfile.open(archive) as tar:
            tar.extractall(source_dir.parent, filter="data")
        (source_dir.parent / f"cpython-source-deps-{dependency.basename}").rename(
            source_dir
        )
        for command in dependency.commands:
            subprocess.run(
                [arg.format(prefix=install_dir, jobs=jobs) for arg in command],
                cwd=source_dir,
                env=env,
                check=True,
            )
        stamp.touch()
    # Some of the builds can't skip their shared libraries. Remove them so
    # CPython links the static ones.
    for path in (install_dir / "lib").iterdir():
        if path.name.endswith(".dylib") or ".so" in path.suffixes:
            path.unlink()


def run_configure(
    source_dir: pathlib.Path,
    build_dir: pathlib.Path,
    deps_dir: pathlib.Path | None = None,
) -> None:
    """Run `configure` in the build directory."""
    configure = source_dir.resolve() / "configure"
    build_dir.mkdir(parents=True, exist_ok=True)
    args = [
        configure,
        f"--prefix={placeholder_prefix()}",
        "--without-static-libpython",
        "--enable-relocatable",
    ]
    env = None
    if deps_dir is not None:
        install_dir = deps_dir.resolve() / "install"
        # --with-openssl is needed without pkg-config; the other libraries are
        # found through the compiler and linker search paths.
        args += [
            f"--with-openssl={install_dir}",
            "CPPFLAGS=" + prepend_flags(f"-I{install_dir}/include", "CPPFLAGS"),
            "LDFLAGS=" + prepend_flags(f"-L{install_dir}/lib", "LDFLAGS"),
        ]
        pkg_config_dirs = [os.fspath(install_dir / "lib" / "pkgconfig")]
        if pkg_config_path := os.environ.get("PKG_CONFIG_PATH"):
            pkg_config_dirs.append(pkg_config_path)
        env = os.environ | {"PKG_CONFIG_PATH": os.pathsep.join(pkg_config_dirs)}
    subprocess.run(args, cwd=build_dir, env=env, check=True)


def run_make(source_dir: pathlib.Path, build_dir: pathlib.Path) -> None:
    """Run `make` in the build directory."""
    subprocess.run(
        ["make", f"-j{os.process_cpu_count() or 1}", "-s"],
        cwd=build_dir,
        check=True,
    )


def run_gather(
    source_dir: pathlib.Path,
    build_dir: pathlib.Path,
    output_dir: pathlib.Path,
    relative_rpath: bool = False,
    log_file: TextIO | None = None,
) -> None:
    """Gather all the release files together.

    The build is installed exactly as configured (into a staging `DESTDIR`)
    and the resulting prefix is moved to *output_dir*. The tree keeps the
    prefix it was configured with; fixup.py replaces the placeholder
    prefix once the tree is where it will live.
    """
    output_dir = output_dir.resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    prefix = configured_prefix(build_dir)
    with tempfile.TemporaryDirectory(
        prefix=".gather-", dir=output_dir.parent
    ) as staging:
        # The top-level `install` target (unlike `commoninstall`) also
        # installs build-details.json and the unversioned bininstall links.
        subprocess.run(
            ["make", "-s", "install", "ENSUREPIP=no", f"DESTDIR={staging}"],
            cwd=build_dir,
            check=True,
            stdout=log_file,
            stderr=log_file,
        )
        (pathlib.Path(staging) / prefix.lstrip("/")).rename(output_dir)
        leftovers = [
            os.fspath(root / name)
            for root, _, filenames in pathlib.Path(staging).walk()
            for name in filenames
        ]
        if leftovers:
            shutil.rmtree(output_dir)
            raise ValueError(
                f"`make install` wrote files outside the prefix {prefix!r} "
                f"(e.g. a separate --exec-prefix), which cannot be gathered: "
                f"{leftovers[:5]}"
            )
    if relative_rpath:
        # python-build-standalone also patches CPython's source, rewrites
        # scripts and metadata, handles Mach-O, and ships shared libpython.
        # Those are intentionally excluded from this post-build ELF experiment.
        set_relative_rpath(output_dir)


def run_test(
    source_dir: pathlib.Path,
    build_dir: pathlib.Path,
    relative_rpath: bool,
    run_fixup: bool,
    work_dir: pathlib.Path | None,
    output_format: str,
    full: bool,
) -> int:
    """Gather a build, move it, run fixup.py, and check that it works."""

    def run(workspace: pathlib.Path) -> list[CheckResult]:
        original_prefix = workspace / "original-prefix"
        relocated_prefix = workspace / "relocated prefix with spaces"
        unrelated_cwd = workspace / "unrelated-working-directory"
        unrelated_cwd.mkdir()

        try:
            broken_modules = broken_stdlib_modules(build_dir, unrelated_cwd)
        except (CheckFailed, OSError, subprocess.TimeoutExpired):
            broken_modules = None

        # `make install` prints thousands of lines, which would bury the results.
        gather_log = workspace / "gather.log"
        fixup_output = []
        try:
            prefix = configured_prefix(build_dir)
            with gather_log.open("w", encoding="utf-8") as log_file:
                run_gather(
                    source_dir,
                    build_dir,
                    original_prefix,
                    relative_rpath,
                    log_file,
                )
            original_prefix.rename(relocated_prefix)
            # fixup.py only knows the placeholder prefix.
            with_fixup = run_fixup and prefix == placeholder_prefix()
            if with_fixup:
                # Run it the way a user would, with a Python that isn't the
                # one in the tree.
                fixup_output = subprocess.run(
                    [sys.executable, FIXUP_SCRIPT, relocated_prefix],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.splitlines()
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            details = []
            if stderr := getattr(exc, "stderr", None):
                details.append(stderr.strip())
            if gather_log.exists():
                lines = gather_log.read_text(errors="replace").splitlines()
                if lines:
                    tail = "\n".join(lines[-20:])
                    details.append(f"end of `make install` output:\n{tail}")
            return [
                CheckResult(
                    "distribution.gather",
                    "FAIL",
                    f"could not prepare the relocated installation: {exc}",
                    details,
                )
            ]

        if with_fixup:
            stale_prefix = None
            summary = "gathered the installation, moved it and ran fixup.py"
        elif prefix == placeholder_prefix():
            stale_prefix = None
            summary = "gathered the installation and moved it without running fixup.py"
        else:
            # A build we didn't configure keeps its --prefix, so that's the
            # old location to look for.
            stale_prefix = pathlib.PurePosixPath(prefix)
            summary = (
                f"installed the build configured for {prefix} "
                "and moved it without running fixup.py"
            )
        results = [CheckResult("distribution.move", "PASS", summary, fixup_output)]
        if stale_prefix is not None and os.path.lexists(stale_prefix):
            # The moved build could load libpython or the stdlib from the
            # old prefix and every other check would still pass.
            results.append(
                CheckResult(
                    "distribution.isolation",
                    "FAIL",
                    "the configured prefix still exists, so passing runtime "
                    "checks may be using it rather than the relocated tree",
                    [f"rename or remove {prefix} and rerun"],
                )
            )
        results.extend(
            RelocatabilityHarness(
                relocated_prefix,
                original_prefix,
                source_dir,
                build_dir,
                unrelated_cwd,
                full=full,
                broken_modules=broken_modules,
                configured_prefix=stale_prefix,
            ).run()
        )
        return results

    results, preserved_path = run_in_temporary_directory(run, work_dir)
    print(render_results(results, output_format, color=use_color(sys.stdout)))
    if preserved_path is not None:
        print(f"Artifacts preserved in {preserved_path}", file=sys.stderr)
    return int(any(result.status == "FAIL" for result in results))


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument(
        "source_dir",
        nargs="?",
        type=pathlib.Path,
        default=pathlib.Path.cwd(),
        help="CPython source checkout (default: current directory)",
    )
    common_parser.add_argument(
        "--build-dir",
        type=pathlib.Path,
        help="build directory (default: SOURCE_DIR/builddir)",
    )

    deps_parser = subparsers.add_parser(
        "deps",
        parents=[common_parser],
        help="Build third-party libraries for stdlib extension modules",
    )
    deps_parser.add_argument(
        "--deps-dir",
        type=pathlib.Path,
        help="where to build and install them (default: SOURCE_DIR/deps)",
    )
    configure_parser = subparsers.add_parser(
        "configure", parents=[common_parser], help="Run `configure`"
    )
    configure_parser.add_argument(
        "--deps-dir",
        type=pathlib.Path,
        help="use the libraries built by the `deps` subcommand in this directory",
    )
    subparsers.add_parser("make", parents=[common_parser], help="Run `make`")
    gather_parser = subparsers.add_parser(
        "gather",
        parents=[common_parser],
        help="Gather all the release files together",
    )
    gather_parser.add_argument(
        "--output",
        dest="output_dir",
        type=pathlib.Path,
        help="distribution directory (default: SOURCE_DIR/dist)",
    )
    relative_rpath_help = (
        "set an $ORIGIN-relative RPATH on the installed ELF files, so they "
        "load libraries from inside the tree (Linux only; requires patchelf)"
    )
    gather_parser.add_argument(
        "--relative-rpath", action="store_true", help=relative_rpath_help
    )
    test_parser = subparsers.add_parser(
        "test",
        parents=[common_parser],
        help="Gather a build, move it, run fixup.py, and check that it works",
    )
    test_parser.add_argument(
        "--relative-rpath", action="store_true", help=relative_rpath_help
    )
    test_parser.add_argument(
        "--no-fixup",
        dest="run_fixup",
        action="store_false",
        help="don't run fixup.py on the moved tree",
    )
    test_parser.add_argument(
        "--work-dir",
        type=pathlib.Path,
        help="keep the temporary files under this directory",
    )
    test_parser.add_argument(
        "--format",
        dest="output_format",
        choices=("human", "json"),
        default="human",
        help="output format (default: human)",
    )
    test_parser.add_argument(
        "--full",
        action="store_true",
        help="also run `python -m test --fast-ci` from the relocated tree",
    )
    namespace = parser.parse_args(args)
    if namespace.build_dir is None:
        namespace.build_dir = namespace.source_dir / "builddir"
    if namespace.command == "deps" and namespace.deps_dir is None:
        namespace.deps_dir = namespace.source_dir / "deps"
    if namespace.command == "gather" and namespace.output_dir is None:
        namespace.output_dir = namespace.source_dir / "dist"
    return namespace


def main(args: list[str] | None = None) -> int:
    namespace = parse_args(args)
    match namespace.command:
        case "deps":
            run_deps(namespace.deps_dir)
            return 0
        case "configure":
            run_configure(namespace.source_dir, namespace.build_dir, namespace.deps_dir)
            return 0
        case "make":
            run_make(namespace.source_dir, namespace.build_dir)
            return 0
        case "gather":
            run_gather(
                namespace.source_dir,
                namespace.build_dir,
                namespace.output_dir,
                namespace.relative_rpath,
            )
            return 0
        case "test":
            return run_test(
                namespace.source_dir,
                namespace.build_dir,
                namespace.relative_rpath,
                namespace.run_fixup,
                namespace.work_dir,
                namespace.output_format,
                namespace.full,
            )
        case command:
            raise ValueError(f"Unknown command: {command}")


if __name__ == "__main__":
    sys.exit(main())
