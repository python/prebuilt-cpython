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

It's broken up into three subcommands to configure, make, and gather the files for a relocatable build. Assuming you run the script from within a source checkout of CPython, your typical command sequence will be:

```shell
../prebuilt-cpython/relocatable.py configure
../prebuilt-cpython/relocatable.py make
../prebuilt-cpython/relocatable.py gather <patching approach>
```

The defaults will result in builddir/ containing the build and dist/ containing the files that comprise a relocatable build.

The key thing to note is which patching approach to specify with `relocatable.py gather`. There are:

- `--patch` which rewrites all found paths of the placeholder `--prefix` argument to the target install directory (which defaults to dist/); this is meant to follow the approach used by conda.
- `--origin` which uses `patchelf` to do a more targeted path rewrite upfront and then to patch any remaining paths; this is meant to follow the approach used by python-build-standalone.

## Project Representatives
These are people representing projects that have experience with prebuilt binaries for CPython.

- [CPython](https://github.com/python/cpython): @brettcannon, @zooba, @emmatyping, @ned-deily
- [python-build-standalone](https://github.com/astral-sh/python-build-standalone): @zanieb, @geofft, @jjhelmus
- [BeeWare](https://beeware.org/): @freakboy3742, @mhsmith
