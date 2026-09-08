# pydantic-guidance

Boundary-aware Pydantic usage guidance for Python. The pydantic-guidance
linter (PG) steers Pydantic code toward the documented best practices: build a
`TypeAdapter` once at module scope, keep `TypeAdapter` out of field
annotations, use `@model_validator` over the deprecated `@root_validator`, and
reserve `BaseModel` for data that earns it.

Ships as a flake8 extension with full `--select` / `--extend-select` / `# noqa` /
`per-file-ignores` support.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Installation

### Add to a project with uv

```console
uv add pydantic-guidance
```

This adds `pydantic-guidance` to `[project.dependencies]` in your
`pyproject.toml` and installs the package (including flake8, pydantic, and the
flake8 entry point) into the project virtual environment.

For development-only usage (linting in CI but excluded from published package
dependencies):

```console
uv add --dev pydantic-guidance
```

This adds the dependency to `[dependency-groups] dev` instead of
`[project.dependencies]`.

### Add manually to pyproject.toml

```toml
[project]
dependencies = [
    "pydantic-guidance>=0.1.0",
]
```

Then run:

```console
uv sync
```

### Install from a Git source

```console
uv add "pydantic-guidance @ git+https://github.com/Aledade-Sandbox/pydantic-guidance"
```

Or in `pyproject.toml`:

```toml
[project]
dependencies = ["pydantic-guidance"]

[tool.uv.sources]
pydantic-guidance = { git = "https://github.com/Aledade-Sandbox/pydantic-guidance" }
```

### Use from a Makefile without installing

Run `flake8` with the PG entry point without adding
`pydantic-guidance` to the project virtual environment. Make the repo
URL reusable so a release bump touches one line:

```makefile
PG_REPO ?= git+https://github.com/Aledade-Sandbox/pydantic-guidance@v0.1.1

lint: ## Run flake8 with PG+PYD selectors sourced from the pydantic-guidance repo
	uvx --from "pydantic-guidance @ $(PG_REPO)" \
	    flake8 --select=PG,PYD --exclude='.venv,*/.venv,.claude' .
```

`uvx` builds an ephemeral venv from the `git+https://` source, installs the
package and its dependencies, runs `flake8`, then discards the venv. uv caches
the build keyed by URL, so repeat invocations reuse it without rebuilding.

This repository is INTERNAL, so the source is a `git+https://` URL rather than
a `codeload` archive tarball: `uvx` clones over your authenticated git
credentials, whereas `archive/refs/tags/*.tar.gz` URLs are anonymous and return
a login page for a private repo. Make the repo public to switch to the lighter
archive-tarball form.

`uvx` is the Makefile analogue of pre-commit's hook install: pre-commit
creates an isolated venv, pip-installs the dependencies + the package, and
execs the entry on staged files. `uvx --from <url>` performs the same three
steps on demand. Override the version without editing the Makefile:

```console
make lint PG_REPO=git+https://github.com/Aledade-Sandbox/pydantic-guidance@v0.1.1
```

## Pre-commit

Add the following to `.pre-commit-config.yaml` to run the linter on every
commit:

```yaml
repos:
  - repo: https://github.com/Aledade-Sandbox/pydantic-guidance
    rev: v0.1.1
    hooks:
      # Pydantic-guidance (PG) and PYD checks together.
      - id: pydantic-guidance
```

Each hook installs the package from the same repo at `additional_dependencies`
install time, so no prior `uv add` or virtual environment is required.

The hook `types_or` is `[python, pyi]`. For Jupyter notebooks or `pyproject.toml`,
extend it explicitly:

```yaml
hooks:
  - id: pydantic-guidance
    types_or: [python, pyi, jupyter, pyproject]
```

## Flake8 Extension

Installing `pydantic-guidance` registers one first-party flake8
checker plugin automatically via entry points, and pulls in the official
[`flake8-pydantic`](https://pypi.org/project/flake8-pydantic/) plugin as a
runtime dependency for additional Pydantic-specific lint coverage:

| Entry point | Plugin class | Error prefix | Checks |
|---|---|---|---|
| `PG` | `PGPlugin` | PG001–PG003 (default-on), PG101 (opt-in) | Pydantic guidance: TypeAdapter placement, deprecated validators, BaseModel surface |
| `PYD` | `flake8-pydantic` (third-party) | PYDxxx | Pydantic-specific lint (model config, validators, fields) |

Verify registration:

```console
uv run flake8 --version
```

`flake8-pydantic` and `pydantic-guidance` both appear in the version
output when installed correctly.

### Running

```console
# Run pydantic-guidance checks only (PG first-party + PYD upstream)
uv run flake8 --select=PG,PYD .

# Include the opt-in advisory hint PG101
uv run flake8 --select=PG,PYD --extend-select=PG101 .

# Run alongside all other flake8 checks
uv run flake8 .
```

### Error Codes

PG001–PG003 are default-on (part of the standard `--select` set). PG101 is an
advisory hint, opt-in via `--extend-select=PG101`. Hard versus soft is purely
the code number; flake8 `--select` / `--extend-select` controls activation and
`# noqa: PGxxx` works per line.

| Code | Activation | Violation | Rule reference |
|------|-----------|-----------|----------------|
| PG001 | default-on | `TypeAdapter(...)` constructed inside a function — build once at module scope and reuse | [PG001.md](pydantic_guidance/_rules/PG001.md) |
| PG002 | default-on | `TypeAdapter` used as a field annotation — use `RootModel` for a reusable named root type | [PG002.md](pydantic_guidance/_rules/PG002.md) |
| PG003 | default-on | Deprecated `@root_validator` — use `@model_validator(mode='before'\|'after')` | [PG003.md](pydantic_guidance/_rules/PG003.md) |
| PG101 | opt-in | `BaseModel` subclass uses no Pydantic surface — a stdlib `@dataclass` is lighter for internal state | [PG101.md](pydantic_guidance/_rules/PG101.md) |

### Suppression

Standard flake8 suppression works automatically:

```python
adapter = TypeAdapter(list[int])  # noqa: PG001  (per-call adapter is intentional)
```

```ini
# setup.cfg or .flake8
[flake8]
per-file-ignores =
    tests/*: PG001,PG002,PYD001
```

### Configuration

The PG plugin reads `.true-spec/project/true-spec.toml` for config gating via
the `[hooks]` section, gated by `structured_data_enforcement`. It defaults to
`true`, so the plugin fires even when the file is absent or malformed (safe
defaults apply):

```toml
# .true-spec/project/true-spec.toml
[hooks]
structured_data_enforcement = true   # gate for the PG guidance linter
```

| Setting | Effect |
|---|---|
| `structured_data_enforcement = false` | PG yields zero errors |

PG is boundary-aware and carries no pydantic-vs-dataclass mode switch: the
former `[rules]` keys (`structured_data`, `allow_raw_collections`,
`allow_any_type`) are removed. Activation of individual codes is controlled
through flake8's `--select` / `--extend-select` / `# noqa` instead.

Override the config root via flake8 option:

```console
uv run flake8 --pg-config-root=/path/to/project .
```

### IDE Setup

For VS Code (ms-python.flake8), PyCharm (flake8 inspection), and Neovim
(ALE) configuration, see [`docs/ide-setup.md`](docs/ide-setup.md).

## Development

```console
git clone https://github.com/Aledade-Sandbox/pydantic-guidance
cd pydantic-guidance/main
uv sync
uv run pytest -v
uv run behave tests/features/
```
