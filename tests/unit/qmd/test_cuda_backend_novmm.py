"""The CUDA backend this host loads must have the VMM pool compiled out.

`qmd embed` hard-aborts on WSL2 when node-llama-cpp's CUDA backend
reserves `CUDA_POOL_VMM_MAX_SIZE` (32 GB of virtual address space) and
the reservation fails. The fix is a rebuild with `GGML_CUDA_NO_VMM=ON`,
applied by `tools/nlc_novmm.sh`.

**That fix does not survive `bun install`.** The rebuilt library lives
inside a bun global install, so a package refresh restores the original
VMM-enabled backend and the abort comes back with nothing in the repo
to notice. This test is that notice.

Skip semantics, deliberately asymmetric:

  * no CUDA backend on this host  -> skip (CI, CPU-only machines, macOS)
  * a CUDA backend exists but carries the VMM pool -> FAIL

so that adding it to the default run costs a few milliseconds on a
machine without CUDA and catches a silent revert on one with it.

Both copies are checked because `localBuilds/` holds `bin/` and
`Release/`: the rebuild writes `bin/`, the process loads `Release/`.
Checking only the rebuilt one is how the abort survived a patch that
looked correct.

`tools/nlc_novmm.sh verify` additionally probes `qmd doctor` for GPU
acceleration. That takes ~10s, which is over the per-test budget, so it
is a Makefile target (`make qmd-backend-check`) rather than a test --
and it matters because zero aborts is also what "the GPU is switched
off entirely" looks like.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

#: Where node-llama-cpp's local build lands. Overridable so a
#: non-standard install works without editing the test, and so the skip
#: path is exercisable.
LOCAL_BUILDS = Path(
    os.environ.get(
        "LIES_NLC_LOCAL_BUILDS",
        "~/.bun/install/global/node_modules/node-llama-cpp/llama/localBuilds",
    )
).expanduser()

VARIANT = os.environ.get("LIES_NLC_VARIANT", "linux-x64-cuda")

#: The two copies a rebuild leaves behind. `bin/` is the rebuild output;
#: `Release/` is what the process actually loads.
COPIES = ("bin", "Release")

#: The VMM pool's typeinfo symbol. Present in the stock backend, absent
#: once `GGML_CUDA_NO_VMM=ON` compiles it out.
_VMM_MARKER = b"ggml_cuda_pool_vmm"


def _carries_vmm(path: Path) -> bool:
    """Whether the library still contains the VMM pool.

    Read in chunks rather than via `strings`: this runs on a ~40 MB
    binary inside the per-test budget, and mmap keeps it to a memory
    scan rather than a subprocess.
    """
    with path.open("rb") as handle:
        previous = b""
        while chunk := handle.read(1 << 20):
            if _VMM_MARKER in previous + chunk:
                return True
            previous = chunk[-len(_VMM_MARKER) :]
    return False


def _existing_backends() -> list[tuple[str, Path]]:
    found = []
    for copy in COPIES:
        lib = LOCAL_BUILDS / VARIANT / copy / "libggml-cuda.so"
        if lib.is_file():
            found.append((copy, lib))
    return found


def test_a_built_cuda_backend_has_the_vmm_pool_compiled_out() -> None:
    """No copy of the CUDA backend may still reserve 32 GB of address space.

    Fails with the path, because "which copy is live" was the entire
    difficulty here -- `Release/` is loaded and `bin/` is not, and they
    are easy to confuse.
    """
    backends = _existing_backends()
    if not backends:
        pytest.skip(f"no node-llama-cpp CUDA build at {LOCAL_BUILDS}")

    still_vmm = [f"{copy}/{lib.name}" for copy, lib in backends if _carries_vmm(lib)]

    assert not still_vmm, (
        "the CUDA backend still contains the VMM pool in: "
        + ", ".join(still_vmm)
        + f" (under {LOCAL_BUILDS / VARIANT}). Re-apply with "
        "`tools/nlc_novmm.sh apply`, then `tools/nlc_novmm.sh verify`. A "
        "`bun install` restores the original backend and silently brings "
        "the abort back."
    )


def test_both_backend_copies_are_present_together() -> None:
    """A rebuild updates `bin/`; the process loads `Release/`.

    A half-applied patch leaves `bin/` VMM-free and `Release/` not, and
    every check that inspects the rebuilt file passes. Naming both here
    is the cheap guard against repeating that.
    """
    present = {copy for copy, _ in _existing_backends()}
    if not present:
        pytest.skip(f"no node-llama-cpp CUDA build at {LOCAL_BUILDS}")

    assert present == set(COPIES), (
        f"only {sorted(present)} of {list(COPIES)} present under "
        f"{LOCAL_BUILDS / VARIANT}; patching one leaves the other "
        "VMM-enabled, and the loaded copy is the one that matters"
    )
