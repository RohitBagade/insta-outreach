"""Filesystem housekeeping."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)


def prune_dated_folders(base_dir: Path, older_than: date, keep: Iterable[str] = ()) -> int:
    """Delete ``base_dir/YYYY-MM-DD`` folders dated before ``older_than``.

    Anything that is not a dated folder is left alone, as are the names in
    ``keep`` (evidence still cited by an open incident).
    """
    if not base_dir.is_dir():
        return 0
    protected = set(keep)
    removed = 0
    for child in base_dir.iterdir():
        if not child.is_dir() or child.is_symlink() or child.name in protected:
            continue
        try:
            day = datetime.strptime(child.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < older_than:
            try:
                shutil.rmtree(child)
                removed += 1
            except OSError as exc:
                log.warning("could not prune %s: %s", child, exc)
    return removed
