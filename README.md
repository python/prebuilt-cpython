# prebuilt-cpython
This repository is for the discussion and planning around providing prebuilt developer binaries on python.org for more platforms.

## Objectives

### Goals
- Provide prebuilt binaries for all [tier 1 platforms](https://peps.python.org/pep-0011/#tier-1) via python.org (other tiers are a bonus)
- The binaries are relocatable (i.e. can be placed anywhere on the file system by an installer and still work)
- Work well enough for local development purposes

### Non-Goals
- Be the most performant build possible (other providers are assumed to fill that need)
- Have any opinion on workflow tooling (this is only about the binaries themselves and not any developer workflow on how to manage these binaries for a user)
- Fully self-contained (i.e. the entire interpreter plus standard library does **not** have to be a single file or not require _some_ patching by the installer, although that would be nice)

## Workflow
Discussions that have no answer yet go to [General discussions category](https://github.com/python/prebuilt-cpython/discussions/categories/general). As decisions are made (with a [two week period for discussions](https://github.com/python/prebuilt-cpython/discussions/13) to stay [unanswered](https://github.com/python/prebuilt-cpython/discussions?discussions_q=is%3Aopen+is%3Aunanswered)), the discussions will be marked as [answered](https://github.com/python/prebuilt-cpython/discussions?discussions_q=is%3Aanswered). Anything that requires an action be taken will have an [issue](https://github.com/python/prebuilt-cpython/issues) created.

The general expectation is that no long-lived code will live in this repo and no patches against CPython will be kept here. Instead, code will just go straight upstream or be considered temporarily here until a final approach is chosen and then be moved upstream.

## Build script

The relocatable.py script in this repo is meant to serve two purposes:

1. To test different approaches to building CPython into a relocatable build.
2. Figure out where upstream changes need to be made so a relocatable can be successful.

It's broken up into subcommands to configure, make, gather, and test a relocatable build. Assuming you run the script from within a source checkout of CPython, your typical command sequence will be:

```shell
../prebuilt-cpython/relocatable.py configure
../prebuilt-cpython/relocatable.py make
../prebuilt-cpython/relocatable.py gather <patching approach>
../prebuilt-cpython/relocatable.py test
```

The defaults will result in builddir/ containing the build and dist/ containing the files that comprise a relocatable build.

The key thing to note is which patching approach to specify with `relocatable.py gather`. There are:

- `--patch` which rewrites all found paths of the placeholder `--prefix` argument to the target install directory (which defaults to dist/); this is meant to follow the approach used by conda.
- `--origin` which uses `patchelf` to do a more targeted path rewrite upfront and then to patch any remaining paths; this is meant to follow the approach used by python-build-standalone.

Either way, `gather` runs CPython's top-level `make install ENSUREPIP=no` into a staging `DESTDIR` and moves the installed prefix into place, so the gathered tree contains everything a normal install provides (such as `build-details.json` and the unversioned `python3` links). Only the placeholder prefix is rewritten, and patched Mach-O files are re-signed ad hoc, since macOS refuses to run binaries whose code signature no longer matches.

### Testing a relocated build

`relocatable.py test` checks whether a build still works after it has been moved. It gathers the build into a temporary directory (dist/ is left alone), renames that directory to a path of a different length with spaces in it, and runs these checks without patching anything again:

- the interpreter starts
- `sys` and `sysconfig` paths point inside the moved tree
- no installed file still mentions the old prefix
- a few stdlib extension modules still work (ones that already fail in the build directory are skipped)
- those paths are still right when Python is run through a symlink
- a venv created from it works

Each check prints `PASS`, `FAIL`, or `SKIP`. Some checks fail today. That's expected: they show what CPython still needs to fix.

```shell
../prebuilt-cpython/relocatable.py test
```

Use `--strategy origin` to gather with `--origin` (the default is `--patch`), `--full` to also run `python -m test --fast-ci`, `--work-dir DIR` to keep the temporary files, and `--format json` for JSON output.

If the build wasn't configured by `relocatable.py configure`, its `--prefix` is left alone and treated as the old location. If that directory still exists, the `distribution.isolation` check fails, because the moved build might be using files from it without anyone noticing.

## Project Representatives
These are people representing projects that have experience with prebuilt binaries for CPython.

- [CPython](https://github.com/python/cpython): @brettcannon, @zooba, @emmatyping, @ned-deily
- [python-build-standalone](https://github.com/astral-sh/python-build-standalone): @zanieb, @geofft, @jjhelmus
- [BeeWare](https://beeware.org/): @freakboy3742, @mhsmith
