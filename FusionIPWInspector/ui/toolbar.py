"""Toolbar integration: one promoted "IPW Inspector" button in Manufacture > Inspect.

The Inspect panel (id ``CAMInspectPanel``) is shared by the Milling, Turning,
Inspection, Fabrication and Utilities tabs of the Manufacture workspace, so a
single control shows up wherever a machinist would look for Measure.
"""
from __future__ import annotations

import adsk.core

from ..utils import log

CAM_WORKSPACE_ID = 'CAMEnvironment'
INSPECT_PANEL_ID = 'CAMInspectPanel'
FALLBACK_TAB_ID = 'MillingTab'
ANCHOR_COMMAND_ID = 'MeasureCommand'


def add_button(ui: adsk.core.UserInterface, command_definition: adsk.core.CommandDefinition) -> bool:
    """Place the command next to Measure in the Manufacture Inspect panel."""
    panel = _find_inspect_panel(ui)
    if panel is None:
        log.error('Manufacture workspace or its Inspect panel was not found; toolbar button not added')
        return False
    existing = panel.controls.itemById(command_definition.id)
    if existing:
        existing.deleteMe()
    control = panel.controls.addCommand(command_definition, ANCHOR_COMMAND_ID, False)
    if control is None:
        control = panel.controls.addCommand(command_definition)
    control.isPromoted = True
    control.isPromotedByDefault = True
    log.info('toolbar button added to %s' % panel.id)
    return True


def remove_button(ui: adsk.core.UserInterface, command_id: str) -> None:
    panel = _find_inspect_panel(ui)
    if panel is None:
        return
    control = panel.controls.itemById(command_id)
    if control:
        control.deleteMe()
        log.info('toolbar button removed')


def _find_inspect_panel(ui: adsk.core.UserInterface):
    workspace = ui.workspaces.itemById(CAM_WORKSPACE_ID)
    if workspace is None:
        return None
    tab = workspace.toolbarTabs.itemById(FALLBACK_TAB_ID)
    if tab is not None:
        panel = tab.toolbarPanels.itemById(INSPECT_PANEL_ID)
        if panel is not None:
            return panel
    for t in range(workspace.toolbarTabs.count):
        panel = workspace.toolbarTabs.item(t).toolbarPanels.itemById(INSPECT_PANEL_ID)
        if panel is not None:
            return panel
    return None
