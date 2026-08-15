"""Convert Logos rich-text XML to Markdown using xml.etree.ElementTree."""

from __future__ import annotations

import xml.etree.ElementTree as ET


def xml_to_markdown(xml_str: str) -> str:
    if not xml_str or not xml_str.strip():
        return ""
    if not xml_str.strip().startswith("<"):
        return xml_str

    try:
        text = xml_str.strip()
        if not text.startswith("<?xml"):
            text = f"<_root>{text}</_root>"
        root = ET.fromstring(text)
        return _process_element(root).strip()
    except ET.ParseError:
        import re

        return re.sub(r"<[^>]+>", "", xml_str).strip()


def _process_element(elem: ET.Element) -> str:
    tag = _local_name(elem.tag)

    if tag in ("_root", "RichText", "Content", "Body", "Runs"):
        return _process_children(elem)

    if tag == "Paragraph":
        return _process_children(elem) + "\n\n"

    if tag == "Run":
        return _process_run(elem)

    if tag == "Reference":
        return _process_reference(elem)

    parts: list[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        parts.append(_process_element(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _process_children(elem: ET.Element) -> str:
    parts: list[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        parts.append(_process_element(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _process_run(elem: ET.Element) -> str:
    text = elem.attrib.get("Text", "") or _process_children(elem)
    if not text:
        return ""

    if elem.attrib.get("FontBold") in ("true", "True"):
        text = f"**{text}**"
    if elem.attrib.get("FontItalic") in ("true", "True"):
        text = f"*{text}*"
    if elem.attrib.get("Superscript") in ("true", "True"):
        text = f"^{text}^"
    return text


def _process_reference(elem: ET.Element) -> str:
    text = elem.attrib.get("Text", "") or _process_children(elem) or ""
    target = elem.attrib.get("Reference", "") or elem.attrib.get("Href", "")
    if target:
        return f"[{text}]({target})"
    return text


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag
