"""Tiny XML helpers.

Camera XML is namespaced (``{http://www.hikvision.com/ver20/XMLSchema}hdd``)
and the namespace URI changes between firmware versions, so every lookup here
matches on the local tag name only.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Iterator


def local_name(tag: str) -> str:
    """'{namespace}hdd' -> 'hdd'"""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse(text: str) -> ET.Element:
    """Parse XML, raising ValueError with a readable message on bad input."""
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"not valid XML: {exc}") from exc


def find_all(root: ET.Element, name: str) -> Iterator[ET.Element]:
    """Yield every descendant whose local tag name matches, ignoring namespaces."""
    for element in root.iter():
        if local_name(element.tag) == name:
            yield element


def children_as_dict(element: ET.Element) -> dict[str, str]:
    """Flatten an element's direct children into {local_name: text}."""
    return {
        local_name(child.tag): (child.text or "").strip()
        for child in element
    }
