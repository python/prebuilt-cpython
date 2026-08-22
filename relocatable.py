"""Make a relocatable build of CPython."""

# /// script
# requires-python = ">=3.14"
import argparse
import os
import pathlib
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
    patch: str,
) -> None:
    """Gather all the release files together."""
    if patch == "origin":
        raise ValueError("the 'origin' patch mode is not supported yet")

    output_dir = output_dir.resolve()
    subprocess.run(
        [
            "make",
            "commoninstall",
            f"prefix={output_dir}",
        ],
        cwd=build_dir,
        check=True,
    )
    patch_path(output_dir, patch)


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
    gather_parser.add_argument(
        "--patch",
        required=True,
        metavar="INSTALL_DIR",
        help=(
            "eventual install directory; relative paths resolve from the current "
            "directory ('origin' is reserved for future use)"
        ),
    )
    namespace = parser.parse_args(args)
    if namespace.build_dir is None:
        namespace.build_dir = namespace.source_dir / "builddir"
    if namespace.command == "gather" and namespace.output_dir is None:
        namespace.output_dir = namespace.source_dir / "dist"
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
                namespace.patch,
            )
        case command:
            raise ValueError(f"Unknown command: {command}")


if __name__ == "__main__":
    main()
