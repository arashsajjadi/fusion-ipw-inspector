"""Hidden "IPW Inspector Self Test" command (no toolbar button; reachable from the
S shortcut search and from Advanced > Diagnostics in the inspector)."""
from __future__ import annotations

from typing import List, Optional

import adsk.cam
import adsk.core

from ..diagnostics import log, self_test
from ..stock.acquisition import StockSession, StockSessions
from ..stock.post_export import _expected_stock_box, model_bounding_box_mm
from ..stock.provider import StockResult
from ..utils.fusion_units import wcs_matrix_rows_mm

COMMAND_ID = 'FusionIPWInspector_SelfTest'
COMMAND_NAME = 'IPW Inspector Self Test'

_handlers: List[adsk.core.EventHandler] = []


class SelfTestCommand:
    def __init__(self, app: adsk.core.Application, sessions: StockSessions) -> None:
        self.app = app
        self.ui = app.userInterface
        self.sessions = sessions
        self._definition: Optional[adsk.core.CommandDefinition] = None

    def register(self) -> None:
        existing = self.ui.commandDefinitions.itemById(COMMAND_ID)
        if existing:
            existing.deleteMe()
        self._definition = self.ui.commandDefinitions.addButtonDefinition(
            COMMAND_ID, COMMAND_NAME, 'Checks the IPW Inspector transform math, units, setup WCS and loaded stock.')
        created = _CreatedHandler(self)
        self._definition.commandCreated.add(created)
        _handlers.append(created)

    def unregister(self) -> None:
        if self._definition:
            try:
                self._definition.deleteMe()
            except Exception:
                pass
            self._definition = None
        _handlers.clear()

    def run_and_show(self, doc: adsk.core.Document, setup: Optional[adsk.cam.Setup], result: Optional[StockResult]) -> str:
        checks = self_test.run_pure_checks()
        cam, _ = StockSession.products(doc) if doc else (None, None)
        setups = []
        if cam is not None:
            for s in cam.setups:
                try:
                    setups.append((s.name, wcs_matrix_rows_mm(s.workCoordinateSystem), s.allOperations.count))
                except Exception as exc:
                    setups.append((s.name, [0.0] * 16, 0))
                    log.error('reading WCS of %s failed' % s.name, exc)
        stock = None
        if result is not None and result.ok and setup is not None:
            stock = {
                'stock_path': result.stock_path,
                'part_path': result.part_path,
                'unit_scale_mm': result.unit_scale_mm,
                'frame': result.frame,
                'model_box_world_mm': model_bounding_box_mm(setup),
                'expected_box': _expected_stock_box(setup),
            }
        checks.extend(self_test.run_fusion_checks(setups, stock))
        text = self_test.report_text(checks)
        log.info('self test:\n' + text)
        self.ui.messageBox(text, COMMAND_NAME)
        return text


class _CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self, owner: SelfTestCommand) -> None:
        super().__init__()
        self.owner = owner

    def notify(self, args: adsk.core.CommandCreatedEventArgs) -> None:
        handler = _ExecuteHandler(self.owner)
        args.command.execute.add(handler)
        _handlers.append(handler)


class _ExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self, owner: SelfTestCommand) -> None:
        super().__init__()
        self.owner = owner

    def notify(self, args: adsk.core.CommandEventArgs) -> None:
        try:
            doc = self.owner.app.activeDocument
            session = self.owner.sessions.for_document(doc) if doc else None
            cam, _ = StockSession.products(doc) if doc else (None, None)
            setup = None
            if session and session.result and cam:
                for s in cam.setups:
                    if s.operationId == session.result.setup_id:
                        setup = s
            self.owner.run_and_show(doc, setup, session.result if session else None)
        except Exception as exc:
            log.error('self test failed to run', exc)
            self.owner.ui.messageBox('The self test could not run:\n%s' % exc, COMMAND_NAME)
