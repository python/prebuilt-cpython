"""Make a relocatable build of CPython.

This script is meant to serve two purposes:

1. Test/show different approaches to making a relocatable build of CPython.
2. Provide an easy way to test a relocatable build so fixes can get upstreamed.
"""

# /// script
# requires-python = ">=3.14"
# ///

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from typing import TextIO

from relocatability import (
    CheckFailed,
    CheckResult,
    RelocatabilityHarness,
    broken_stdlib_modules,
    render_results,
    use_color,
    run_in_temporary_directory,
)


# Thin 32/64-bit Mach-O files in either byte order, and universal binaries.
MACHO_MAGIC = {
    bytes.fromhex(magic)
    for magic in ("feedface", "cefaedfe", "feedfacf", "cffaedfe", "cafebabe")
}


def placeholder_prefix() -> str:
    """Return the placeholder prefix used when building CPython."""
    return "/the/knights/who/say/ni".ljust(255, "i")


def configured_prefix(build_dir: pathlib.Path) -> str:
    """Return the `--prefix` the build directory was configured with."""
    makefile = build_dir / "Makefile"
    for line in makefile.read_text(encoding="utf-8").splitlines():
        if match := re.fullmatch(r"prefix=\s*(\S.*?)\s*", line):
            return match.group(1)
    raise ValueError(f"no prefix found in {makefile}")


def patch_path(output_dir: pathlib.Path, patch: str) -> None:
    """Replace placeholder prefixes with an eventual installation path."""
    target = os.fsencode(pathlib.Path(patch).resolve())
    placeholder = os.fsencode(placeholder_prefix())
    if len(target) > len(placeholder):
        raise ValueError(
            f"resolved patch path is {len(target)} bytes; "
            f"the maximum is {len(placeholder)} bytes"
        )

    for root, _, filenames in output_dir.walk():
        for filename in filenames:
            path = root / filename
            if path.is_symlink():
                continue
            contents = path.read_bytes()
            if placeholder not in contents:
                continue
            replacement = target
            if b"\0" in contents:
                replacement = target.ljust(len(placeholder), b"\0")
            path.write_bytes(contents.replace(placeholder, replacement))
            if contents[:4] in MACHO_MAGIC:
                # Rewriting a Mach-O file invalidates its (ad-hoc) code
                # signature, and arm64 macOS kills processes that load it.
                subprocess.run(
                    ["codesign", "--force", "--sign", "-", path],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )


def patch_origin(output_dir: pathlib.Path) -> None:
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


def run_configure(source_dir: pathlib.Path, build_dir: pathlib.Path) -> None:
    """Run `configure` in the build directory."""
    configure = source_dir.resolve() / "configure"
    build_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            configure,
            f"--prefix={placeholder_prefix()}",
            "--without-static-libpython",
        ],
        cwd=build_dir,
        check=True,
    )


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
    strategy: str,
    install_dir: pathlib.Path,
    log_file: TextIO | None = None,
) -> None:
    """Gather all the release files together.

    The build is installed exactly as configured (into a staging `DESTDIR`)
    and the resulting prefix is moved to *output_dir*. Only the placeholder
    prefix from `configure` is rewritten; a build configured with any other
    prefix keeps it, so the gathered tree is a moved copy of that install.
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
    if strategy == "origin":
        # python-build-standalone also patches CPython's source, rewrites
        # scripts and metadata, handles Mach-O, and ships shared libpython.
        # Those are intentionally excluded from this post-build ELF experiment.
        patch_origin(output_dir)
    patch_path(output_dir, os.fspath(install_dir))


def run_test(
    source_dir: pathlib.Path,
    build_dir: pathlib.Path,
    strategy: str,
    work_dir: pathlib.Path | None,
    output_format: str,
    full: bool,
) -> int:
    """Gather a build, move it, and check that it still works."""

    def run(workspace: pathlib.Path) -> list[CheckResult]:
        original_prefix = workspace / "original-prefix"
        relocated_prefix = workspace / "relocated prefix with spaces"
        unrelated_cwd = workspace / "unrelated-working-directory"
        unrelated_cwd.mkdir()

        try:
            broken_modules = broken_stdlib_modules(build_dir, unrelated_cwd)
        except (CheckFailed, OSError, subprocess.TimeoutExpired):
            broken_modules = None

        try:
            prefix = configured_prefix(build_dir)
            run_gather(
                source_dir,
                build_dir,
                original_prefix,
                strategy,
                original_prefix,
                sys.stderr if output_format == "json" else None,
            )
            original_prefix.rename(relocated_prefix)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            return [
                CheckResult(
                    "distribution.gather",
                    "FAIL",
                    f"could not prepare the relocated installation: {exc}",
                )
            ]

        if prefix == placeholder_prefix():
            stale_prefix = None
            summary = "gathered the installation and moved it without repatching"
        else:
            # A build we didn't configure keeps its --prefix, so that's the
            # old location to look for.
            stale_prefix = pathlib.PurePosixPath(prefix)
            summary = (
                f"installed the build configured for {prefix} "
                "and moved it without patching"
            )
        results = [CheckResult("distribution.move", "PASS", summary)]
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

    subparsers.add_parser("configure", parents=[common_parser], help="Run `configure`")
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
    patch_group = gather_parser.add_mutually_exclusive_group(required=True)
    patch_group.add_argument(
        "--patch",
        nargs="?",
        const=None,
        default=argparse.SUPPRESS,
        type=pathlib.Path,
        metavar="INSTALL_DIR",
        help=(
            "patch in the eventual install directory (default: OUTPUT); "
            "relative paths resolve from the current directory"
        ),
    )
    test_parser = subparsers.add_parser(
        "test",
        parents=[common_parser],
        help="Gather a build, move it, and check that it still works",
    )
    test_parser.add_argument(
        "--strategy",
        choices=("patch", "origin"),
        default="patch",
        help="gather strategy (default: patch)",
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
    patch_group.add_argument(
        "--origin",
        nargs="?",
        const=None,
        default=argparse.SUPPRESS,
        type=pathlib.Path,
        metavar="INSTALL_DIR",
        help=(
            "patch in the eventual install directory and use $ORIGIN for ELF "
            "library lookup (default: OUTPUT; Linux only; requires patchelf)"
        ),
    )
    namespace = parser.parse_args(args)
    if namespace.build_dir is None:
        namespace.build_dir = namespace.source_dir / "builddir"
    if namespace.command == "gather" and namespace.output_dir is None:
        namespace.output_dir = namespace.source_dir / "dist"
    if namespace.command == "gather":
        if hasattr(namespace, "patch"):
            namespace.patch_strategy = "patch"
            install_dir = namespace.patch
        else:
            namespace.patch_strategy = "origin"
            install_dir = namespace.origin
        if install_dir is None:
            install_dir = namespace.output_dir
        namespace.install_dir = install_dir
    return namespace


def main(args: list[str] | None = None) -> int:
    namespace = parse_args(args)
    match namespace.command:
        case "configure":
            run_configure(namespace.source_dir, namespace.build_dir)
            return 0
        case "make":
            run_make(namespace.source_dir, namespace.build_dir)
            return 0
        case "gather":
            run_gather(
                namespace.source_dir,
                namespace.build_dir,
                namespace.output_dir,
                namespace.patch_strategy,
                namespace.install_dir,
            )
            return 0
        case "test":
            return run_test(
                namespace.source_dir,
                namespace.build_dir,
                namespace.strategy,
                namespace.work_dir,
                namespace.output_format,
                namespace.full,
            )
        case command:
            raise ValueError(f"Unknown command: {command}")


if __name__ == "__main__":
    sys.exit(main())
