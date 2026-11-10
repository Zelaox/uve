from __future__ import annotations

import xml.etree.ElementTree as ET

from agent_training.uve.adapters.ui_navigation_recovery import (
    any_selector_matches,
    text_markers_present,
)


def _tree(*nodes: tuple[str, dict[str, str]]) -> ET.Element:
    root = ET.Element("hierarchy")
    for tag, attrs in nodes:
        ET.SubElement(root, tag, attrs)
    return root


def test_ready_selector_detects_main_tabs():
    tree = _tree(("node", {"resource-id": "example-tab-home", "text": ""}))
    assert any_selector_matches(tree, [{"resource-id": "example-tab-home"}])


def test_settings_markers_without_tabs():
    tree = _tree(("node", {"text": "NOTIFICATIONS"}))
    assert text_markers_present(tree, ["NOTIFICATIONS"])
    assert not any_selector_matches(tree, [{"resource-id": "example-tab-home"}])


def test_blocking_overlay_marker():
    tree = _tree(
        (
            "node",
            {
                "text": "expo-notifications: Android Push notifications (remote notifications) functionality",
            },
        )
    )
    assert text_markers_present(tree, ["expo-notifications"], contains=True)
