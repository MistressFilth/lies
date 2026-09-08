# IDE Setup Guide for pydantic-guidance

`pydantic-guidance` provides one first-party flake8 checker plugin:

- **PG** (`PGPlugin`) — pydantic-guidance hints (codes PG001–PG003
  default-on, PG101 opt-in)

The plugin surfaces inline diagnostics in VS Code, PyCharm, and Neovim via the
standard flake8 protocol. No additional plugin code is required beyond
installing `pydantic-guidance`.

Installing `pydantic-guidance` also pulls in the official
[`flake8-pydantic`](https://pypi.org/project/flake8-pydantic/) plugin as a
runtime dependency. Its `PYDxxx` codes appear in the same flake8 diagnostic
stream as PG and require no extra configuration; suppress individual rules
with `# noqa: PYDxxx` or via standard `extend-ignore` / `per-file-ignores`.

## Installation

```bash
pip install pydantic-guidance
# or
uv add pydantic-guidance
```

After installation, flake8 discovers the PG plugin automatically via its
`flake8.extension` entry point.

## Configuration

### Config root

The plugin resolves `pg-config-root` to locate
`.true-spec/project/true-spec.toml`. Set this option in your flake8 config file
pointing to the project root directory.

### VS Code and Neovim (ALE)

Add to `.flake8`:

```ini
[flake8]
pg-config-root = /path/to/project/root
```

### PyCharm

Add to `setup.cfg`:

```ini
[flake8]
pg-config-root = /path/to/project/root
```

## true-spec.toml Options

Configure enforcement via the `[hooks]` section of
`.true-spec/project/true-spec.toml`:

```toml
[hooks]
structured_data_enforcement = true   # set false to disable all PG checks
```

When `true-spec.toml` is absent or malformed, the plugin applies safe defaults
(gate `true`) and continues emitting diagnostics without propagating
exceptions to the flake8 host process.

PG is boundary-aware and carries no pydantic-vs-dataclass mode switch. Per-code
activation is controlled through flake8's `--select` / `--extend-select` /
`# noqa` rather than a config key. PG101 is opt-in:

```ini
[flake8]
extend-select = PG101
```

## Violation Codes

### PG (pydantic-guidance)

| Code   | Activation  | Description                                                              |
|--------|-------------|--------------------------------------------------------------------------|
| PG001  | default-on  | `TypeAdapter(...)` constructed inside a function; build once at module scope |
| PG002  | default-on  | `TypeAdapter` used as a field annotation; use `RootModel` instead         |
| PG003  | default-on  | Deprecated `@root_validator`; use `@model_validator(mode=...)`            |
| PG101  | opt-in      | `BaseModel` subclass uses no Pydantic surface; a stdlib `@dataclass` is lighter |

For the upstream `PYDxxx` rule catalog, see
[flake8-pydantic on PyPI](https://pypi.org/project/flake8-pydantic/).

## IDE-Specific Setup

### VS Code

1. Install the [ms-python.flake8](https://marketplace.visualstudio.com/items?itemName=ms-python.flake8) extension.
2. Configure the extension to use your project's virtual environment.
3. Add `pg-config-root` to `.flake8` as shown above.
4. PG diagnostics appear inline as you edit.

### PyCharm

1. Enable flake8 under **Settings > Tools > External Tools** or via the
   **Python > Flake8** inspection, pointing to the project venv's flake8 binary.
2. Add `pg-config-root` to `setup.cfg` under `[flake8]`.
3. PG codes surface as inspections.

### Neovim (ALE)

1. Install [ALE](https://github.com/dense-analysis/ale) and configure it to use
   flake8 as a Python linter.
2. Add `pg-config-root` to `.flake8`.
3. PG codes appear in ALE's diagnostics list on file save.
