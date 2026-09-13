"""Citation: a path + source discriminator for retrieved pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Citation:
    """A page citation with explicit source.

    ``source`` discriminates between the two physical roots the
    dispatcher reads from:

    - ``"library"``: lives under
      ``Library.collections_root/<coll>/<file>``
      (``src/lies/library/paths.py:45``).
    - ``"wiki"``: lives under ``wiki.wiki_dir``.

    Hard cutover — this field is required, no defaults. Same path from
    both roots produces two distinct Citation objects.
    """

    path: str
    source: Literal["library", "wiki"]
