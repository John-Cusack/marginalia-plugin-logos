# Vendored `marginalia-ai-sdk` (pre-release only)

Nothing here is plugin code. `marginalia-ai-sdk/` is a copy of `packages/sdk`
from [MarginaliaAI](https://github.com/John-Cusack/MarginaliaAI) `main` at commit
`1e9782f2d235f24e2590641b345bfdca90582ef4` (SDK `0.6.0`), taken with
`git archive`. It is here only because that SDK is not on PyPI yet, and this
repository's lockfile and CI need something to resolve. Once the commit is on
GitHub, a `git` source pinned to it replaces this directory; once the SDK is
published, nothing does.

## The one edit

`pyproject.toml` declares `name = "marginalia-ai-sdk"`, which upstream still
calls `research-engine-sdk`. The MarginaliaAI package family renamed its
distributions before first release, and this plugin depends on the new name; the
copy has to answer to it or nothing resolves. The import package is untouched:
it is `research_engine_sdk`, as upstream ships it.

Nothing else differs. Every file except `pyproject.toml` is byte-identical to
upstream:

```bash
git -C ../MarginaliaAI archive 1e9782f packages/sdk | tar -x -C /tmp --strip-components=1
diff -r --exclude=pyproject.toml /tmp/sdk vendor/marginalia-ai-sdk   # no output
diff /tmp/sdk/pyproject.toml vendor/marginalia-ai-sdk/pyproject.toml # the name line only
```

Upstream's `packages/core` still requires `research-engine-sdk`, so it needs the
same rename before core and this plugin can be installed together from an index.

The plugin's wheel never contains this directory: the wheel requires
`marginalia-ai-sdk>=0.6,<0.7` from the package index like any other dependency.

## Removing it

Once `marginalia-ai-sdk` 0.6.0 is published:

1. delete this directory;
2. delete `[tool.uv.sources]` from `pyproject.toml` and run `uv lock`;
3. delete the "Build SDK artifact" steps in `.github/workflows/ci.yml`;
4. delete the `pre-release:sdk` block in `README.md`.

The release workflow refuses to publish until all of that is done.
