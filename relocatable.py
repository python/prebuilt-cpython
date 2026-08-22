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
import shutil
import subprocess


def placeholder_prefix() -> str:
    """Return the placeholder prefix used when building CPython."""
    return "/the/knights/who/say/ni".ljust(255, "i")


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


def patch_origin(output_dir: pathlib.Path) -> None:
    """Make installed ELF files load libraries relative to themselves."""
    lib_dir = output_dir / "lib"
    for root, _, filenames in output_dir.walk():
        for filename in filenames:
            path = root / filename
            if path.is_symlink():
                continue
            with path.open("rb") as file:
                if file.read(4) != b"\x7fELF":
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
) -> None:
    """Gather all the release files together."""
    output_dir = output_dir.resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    subprocess.run(
        [
            "make",
            "commoninstall",
            f"prefix={output_dir}",
        ],
        cwd=build_dir,
        check=True,
    )
    if strategy == "origin":
        # python-build-standalone also patches CPython's source, rewrites
        # scripts and metadata, handles Mach-O, and ships shared libpython.
        # Those are intentionally excluded from this post-build ELF experiment.
        patch_origin(output_dir)
    patch_path(output_dir, os.fspath(install_dir))


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
        type=pathlib.Path,
        metavar="INSTALL_DIR",
        help=(
            "patch in the eventual install directory (default: OUTPUT); "
            "relative paths resolve from the current directory"
        ),
    )
    patch_group.add_argument(
        "--origin",
        nargs="?",
        const=None,
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
        if namespace.patch is not None:
            namespace.patch_strategy = "patch"
            install_dir = namespace.patch
        else:
            namespace.patch_strategy = "origin"
            install_dir = namespace.origin
        if install_dir is None:
            install_dir = namespace.output_dir
        namespace.install_dir = install_dir
    return namespace


def main(args: list[str] | None = None) -> None:
    namespace = parse_args(args)
    match namespace.command:
        case "configure":
            run_configure(namespace.source_dir, namespace.build_dir)
        case "make":
            run_make(namespace.source_dir, namespace.build_dir)
        case "gather":
            run_gather(
                namespace.source_dir,
                namespace.build_dir,
                namespace.output_dir,
                namespace.patch_strategy,
                namespace.install_dir,
            )
        case command:
            raise ValueError(f"Unknown command: {command}")


if __name__ == "__main__":
    main()
