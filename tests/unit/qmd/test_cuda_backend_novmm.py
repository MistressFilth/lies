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


#: The prebuilt fallback under the *other* variant. `node-llama-cpp`
#: ships `linux-x64-cuda` and `linux-x64-cuda-ext`, both exposing the
#: same `getBinsDir()`, and picks one at runtime. When a local build
#: exists it is used; when it does not -- which is exactly what a `bun
#: install` leaves behind -- the prebuilt in `linux-x64-cuda-ext` serves.
#: Checking only the local build misses the revert this test exists to
#: catch.
FALLBACK = Path(
    os.environ.get(
        "LIES_NLC_FALLBACK",
        "~/.bun/install/global/node_modules/@node-llama-cpp/"
        f"{VARIANT}-ext/bins/{VARIANT}/fallback/libggml-cuda.so",
    )
).expanduser()


def _existing_backends() -> list[tuple[str, Path]]:
    """Every CUDA backend this host could load, local build or prebuilt."""
    found = []
    for copy in COPIES:
        lib = LOCAL_BUILDS / VARIANT / copy / "libggml-cuda.so"
        if lib.is_file():
            found.append((f"localBuilds/{copy}", lib))
    if FALLBACK.is_file():
        found.append(("prebuilt-fallback", FALLBACK))
    return found


def test_no_cuda_backend_still_carries_the_vmm_pool() -> None:
    """No backend that could serve a query may reserve 32 GB of address space.

    Covers the local build *and* the prebuilt fallback. Checking only the
    local build left the real failure mode open: `bun install` removes
    `llama/localBuilds/`, the prebuilt takes over with VMM intact, and a
    test that skips on a missing local build reports green throughout.
    """
    backends = _existing_backends()
    if not backends:
        pytest.skip(
            f"no node-llama-cpp CUDA backend found (looked in {LOCAL_BUILDS} and {FALLBACK})"
        )

    still_vmm = [f"{label}" for label, lib in backends if _carries_vmm(lib)]
    assert not still_vmm, (
        "the CUDA backend still contains the VMM pool in: "
        + ", ".join(still_vmm)
        + ". Re-apply with `make qmd-backend-fix`, then `make qmd-backend-check`. "
        "A `bun install` restores the prebuilt and silently brings the abort back."
    )


def test_both_backend_copies_are_present_together() -> None:
    """A rebuild updates `bin/`; the process loads `Release/`.

    A half-applied patch leaves `bin/` VMM-free and `Release/` not, and
    every check that inspects the rebuilt file passes. Naming both here
    is the cheap guard against repeating that.
    """
    local = {label for label, _ in _existing_backends() if label.startswith("localBuilds/")}
    if not local:
        pytest.skip(f"no local node-llama-cpp CUDA build at {LOCAL_BUILDS}")

    expected = {f"localBuilds/{copy}" for copy in COPIES}
    assert local == expected, (
        f"only {sorted(local)} of {sorted(expected)} present under "
        f"{LOCAL_BUILDS / VARIANT}; patching one leaves the other "
        "VMM-enabled, and the loaded copy is the one that matters"
    )
