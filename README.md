# prebuilt-cpython
This repository is for the discussion and planning around providing prebuilt developer binaries on python.org for more platforms.

## Objectives

### Goals
- Provide prebuilt binaries for all [tier 1 platforms](https://peps.python.org/pep-0011/#tier-1) via python.org
- The binaries are relocatable (i.e. can be placed anywhere on the file system and still work)
- The binaries do not require an installer (i.e. downloaded in an archive file like a `.zip` or `.tar.xz` and then simply unpacked)
- Work well enough for local development purposes

### Non-Goals
- Be the most performant build possible (other providers are assumed to fill that need)
- Have any opinion on workflow tooling (this is only about the binaries themselves and not any developer workflow on how to manage these binaries for a user)
- Fully self-contained (i.e. the entire interpreter plus standard library does **not** have to be a single file)

## Project Representatives
These are people representing projects that have experience with prebuilt binaries for CPython.

- CPython: @brettcannon
