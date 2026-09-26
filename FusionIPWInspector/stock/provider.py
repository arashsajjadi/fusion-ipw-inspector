"""Common vocabulary of the stock providers.

A provider turns "the stock of this setup" into a pickable mesh in the
viewport and reports where it came from. The inspector never needs to know
which provider succeeded; it only reads ``StockResult``.

Provider chain (first success wins):

    PostStockProvider      Fusion's post engine exports the in-process stock
    SavedStockProvider     a file saved from Simulation (auto-detected or picked)

Both keep the geometry in the setup's WCS coordinates internally, which is the
frame Fusion writes the files in, and hand a world-space placement to the
temporary mesh so picked points come back in world space.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..core.transform import SetupFrame

SOURCE_CURRENT = 'current'     # exported by Fusion for this setup right now
SOURCE_SAVED = 'saved'         # loaded from a file the user saved from Simulation
SOURCE_NONE = 'none'           # nothing loaded; model geometry only

SOURCE_LABELS = {
    SOURCE_CURRENT: 'Current IPW',
    SOURCE_SAVED: 'Saved IPW',
    SOURCE_NONE: 'No IPW available',
}


@dataclass
class StockResult:
    """Outcome of one acquisition attempt."""

    source: str                              # SOURCE_* constant
    setup_name: str = ''
    setup_id: int = 0
    frame: Optional[SetupFrame] = None       # frame the mesh file is expressed in
    stock_path: str = ''                     # STL on disk (cache), '' when none
    part_path: str = ''                      # part STL exported alongside, if any
    unit_scale_mm: float = 1.0               # file unit -> mm
    triangle_count: int = 0
    mean_edge_mm: float = 0.0
    is_plain_box: bool = False               # stock is an unmachined box (12 triangles)
    fingerprint: str = ''                    # content hash, used to skip re-imports
    mesh_body_valid: bool = False            # a pickable temporary mesh exists
    provider: str = ''                       # provider name, shown only in diagnostics
    notes: List[str] = field(default_factory=list)
    error: str = ''                          # user-facing failure text, '' on success

    @property
    def ok(self) -> bool:
        return self.source != SOURCE_NONE and not self.error

    def label(self) -> str:
        base = SOURCE_LABELS.get(self.source, self.source)
        if self.source == SOURCE_NONE and self.error:
            return '%s: %s' % (base, self.error)
        return base


class StockError(Exception):
    """User-facing failure: the message says what happened and what to do next."""
