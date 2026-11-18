"""Set the prefix of an unpacked build made by `relocatable.py gather`.

The build was configured with a placeholder prefix. This script replaces the
placeholder with the tree's actual location in the text files that contain
it. Binary files are never modified.

Run it once, after unpacking the tree where it is going to live, with any
Python 3.12 or newer other than the one in the tree:

    python3 fixup.py PATH/TO/TREE

It fails if no text file in the tree has the placeholder, which is the case
for a tree it has already been run on.
"""

# /// script
# requires-python = ">=3.12"
# ///

import argparse
import os
import pathlib
import re
import sys

PLACEHOLDER = "/the/knights/who/say/ni".ljust(255, "i")
# The first line of the Python scripts that CPython installs.
SHEBANG = re.compile(
    rb"#!(%s/bin/python[^/\s]*)\n" % re.escape(os.fsencode(PLACEHOLDER))
)
# Linux before 5.1 truncates a longer "#!" line.
SHEBANG_LIMIT = 127


def fix_shebang(contents: bytes, placeholder: bytes, prefix: bytes) -> bytes:
    """Replace a "#!" line that wouldn't work with the new prefix.

    A "#!" line can't name an interpreter whose path has spaces, and its
    length is limited. Such a script is run by /bin/sh instead, with a
    first statement that the shell reads as `exec` and Python as a string.
    """
    match = SHEBANG.match(contents)
    if match is None:
        return contents
    python = match[1].replace(placeholder, prefix)
    if not re.search(rb"\s", python) and len(python) + 2 <= SHEBANG_LIMIT:
        return contents
    return b"#!/bin/sh\n'exec' '%s' \"$0\" \"$@\"\n" % python + contents[match.end() :]


def fixup(tree: pathlib.Path) -> tuple[list[pathlib.Path], list[pathlib.Path]]:
    """Replace the placeholder prefix in the text files of *tree*.

    Return the text files that were rewritten and the binary files that
    still contain the placeholder.
    """
    tree = tree.resolve(strict=True)
    if unsafe := set(os.fspath(tree)) & set("'\"\\\n"):
        # The prefix ends up inside Python string literals in _sysconfigdata.
        raise ValueError(f"{tree} contains {''.join(sorted(unsafe))!r}")
    placeholder = os.fsencode(PLACEHOLDER)
    prefix = os.fsencode(tree)
    text = {}
    binary = []
    for root, _, filenames in tree.walk():
        for filename in filenames:
            path = root / filename
            if path.is_symlink():
                continue
            contents = path.read_bytes()
            if placeholder not in contents:
                continue
            if b"\0" in contents:
                binary.append(path)
            else:
                text[path] = contents
    if not text:
        raise ValueError(
            f"no text file in {tree} has the placeholder prefix; "
            "it has already been fixed up or wasn't built by relocatable.py"
        )
    for path, contents in text.items():
        contents = fix_shebang(contents, placeholder, prefix)
        path.write_bytes(contents.replace(placeholder, prefix))
    return sorted(text), sorted(binary)


def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.partition("\n")[0])
    parser.add_argument("tree", type=pathlib.Path, help="the unpacked installation")
    namespace = parser.parse_args(args)
    try:
        text, binary = fixup(namespace.tree)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    tree = namespace.tree.resolve()
    print(f"Set the prefix to {tree} in {len(text)} text files:")
    for path in text:
        print(f"  {path.relative_to(tree)}")
    if binary:
        print(f"Left {len(binary)} binary files that contain the placeholder alone:")
        # A .pyc file records where its source was when it was compiled.
        if pyc := sum(path.suffix == ".pyc" for path in binary):
            print(f"  {pyc} .pyc files")
        for path in binary:
            if path.suffix != ".pyc":
                print(f"  {path.relative_to(tree)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
