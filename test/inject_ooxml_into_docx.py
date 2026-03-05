#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Insert a WordprocessingML (OOXML) fragment (e.g., a <w:tbl> table) into an existing .docx,
and embed two images with proper relationships, replacing placeholder rIds in the XML.

Requirements:
  pip install lxml

Usage:
  python inject_ooxml_into_docx.py \
      --template template.docx \
      --out output.docx \
      --xml fragment.xml \
      --img1 id_distance.png \
      --img2 neg_ratio.png

Notes:
- The XML fragment should include the w:tbl element (as in your previous message).
- The fragment can contain placeholder relationship IDs: rIdFig1 and rIdFig2.
  This script will create real image relationships and replace those placeholders.
"""

import argparse
import os
import re
import zipfile
from dataclasses import dataclass
from typing import Tuple

from lxml import etree


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"

NSMAP = {
    "w": W_NS,
    "r": R_NS,
    "rel": REL_NS,
    "a": A_NS,
    "wp": WP_NS,
    "pic": PIC_NS,
}

IMAGE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"


@dataclass
class DocxPaths:
    document_xml: str = "word/document.xml"
    rels_xml: str = "word/_rels/document.xml.rels"
    media_dir: str = "word/media/"


def _read_zip_text(zf: zipfile.ZipFile, path: str) -> str:
    return zf.read(path).decode("utf-8")


def _read_file_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _next_rid(rels_root: etree._Element) -> str:
    """
    Find next available rId number in document.xml.rels (e.g., rId12 -> next is rId13).
    """
    max_id = 0
    for rel in rels_root.findall(f"{{{REL_NS}}}Relationship"):
        rid = rel.get("Id", "")
        m = re.match(r"rId(\d+)$", rid)
        if m:
            max_id = max(max_id, int(m.group(1)))
    return f"rId{max_id + 1}"


def _add_image_relationship(rels_root: etree._Element, rid: str, target: str) -> None:
    rel = etree.Element(f"{{{REL_NS}}}Relationship")
    rel.set("Id", rid)
    rel.set("Type", IMAGE_REL_TYPE)
    rel.set("Target", target)  # e.g., "media/image1.png"
    rels_root.append(rel)


def _unique_media_name(existing_names: set, desired_name: str) -> str:
    """
    Ensure media filename is unique inside word/media/.
    """
    base, ext = os.path.splitext(desired_name)
    name = desired_name
    i = 1
    while name in existing_names:
        name = f"{base}_{i}{ext}"
        i += 1
    existing_names.add(name)
    return name


def _list_existing_media(zf: zipfile.ZipFile, media_dir: str) -> set:
    names = set()
    for n in zf.namelist():
        if n.startswith(media_dir):
            names.add(os.path.basename(n))
    return names


def _parse_fragment_tbl(fragment_xml: str) -> etree._Element:
    """
    Parse the fragment containing <w:tbl ...> ... </w:tbl>.
    We wrap it in a temporary root with known namespaces so lxml can parse reliably.
    """
    wrapper = f"""
    <root xmlns:w="{W_NS}"
          xmlns:r="{R_NS}"
          xmlns:wp="{WP_NS}"
          xmlns:a="{A_NS}"
          xmlns:pic="{PIC_NS}">
    {fragment_xml}
    </root>
    """
    root = etree.fromstring(wrapper.encode("utf-8"))
    tbl = root.find("w:tbl", namespaces=NSMAP)
    if tbl is None:
        raise ValueError("XML fragment does not contain a <w:tbl> root element.")
    return tbl


def _append_to_body(doc_root: etree._Element, element: etree._Element) -> None:
    """
    Append element to <w:body>, placing it before <w:sectPr> if present.
    """
    body = doc_root.find("w:body", namespaces=NSMAP)
    if body is None:
        raise ValueError("Invalid document.xml: missing <w:body>.")
    sectpr = body.find("w:sectPr", namespaces=NSMAP)
    if sectpr is not None:
        # insert right before sectPr (must remain last)
        body.insert(body.index(sectpr), element)
    else:
        body.append(element)


def inject_fragment_into_docx(
    template_docx: str,
    out_docx: str,
    fragment_xml_path: str,
    img1_path: str,
    img2_path: str,
) -> None:
    paths = DocxPaths()

    if not os.path.exists(template_docx):
        raise FileNotFoundError(f"Template docx not found: {template_docx}")
    for p in [fragment_xml_path, img1_path, img2_path]:
        if not os.path.exists(p):
            raise FileNotFoundError(f"File not found: {p}")

    fragment_xml = _read_file_text(fragment_xml_path)

    # We'll rebuild a new docx zip by copying everything from template,
    # but replacing document.xml and document.xml.rels, and adding media files.
    with zipfile.ZipFile(template_docx, "r") as zin:
        doc_xml_str = _read_zip_text(zin, paths.document_xml)
        rels_xml_str = _read_zip_text(zin, paths.rels_xml)

        doc_root = etree.fromstring(doc_xml_str.encode("utf-8"))
        rels_root = etree.fromstring(rels_xml_str.encode("utf-8"))

        # Determine unique media names inside word/media/
        existing_media = _list_existing_media(zin, paths.media_dir)
        img1_name = _unique_media_name(existing_media, os.path.basename(img1_path))
        img2_name = _unique_media_name(existing_media, os.path.basename(img2_path))

        # Create relationships for the images
        rid1 = _next_rid(rels_root)
        rid2 = _next_rid(rels_root)  # after adding rid1? safer: compute after append
        # Compute rid2 based on current max after rid1 appended:
        _add_image_relationship(rels_root, rid1, f"media/{img1_name}")
        rid2 = _next_rid(rels_root)
        _add_image_relationship(rels_root, rid2, f"media/{img2_name}")

        # Replace placeholders in fragment (if present)
        fragment_xml = fragment_xml.replace("rIdFig1", rid1).replace("rIdFig2", rid2)

        # Parse the <w:tbl> element and append to document body
        tbl_elem = _parse_fragment_tbl(fragment_xml)
        _append_to_body(doc_root, tbl_elem)

        # Serialize updated XML
        new_doc_xml = etree.tostring(doc_root, encoding="UTF-8", xml_declaration=True, standalone="yes")
        new_rels_xml = etree.tostring(rels_root, encoding="UTF-8", xml_declaration=True, standalone="yes")

        # Write output docx
        with zipfile.ZipFile(out_docx, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            # Copy everything as-is first, except what we will replace/add
            replaced = {paths.document_xml, paths.rels_xml}
            for item in zin.infolist():
                if item.filename in replaced:
                    continue
                # We will also overwrite media names if collisions happened, but we ensured uniqueness
                data = zin.read(item.filename)
                zout.writestr(item, data)

            # Write updated parts
            zout.writestr(paths.document_xml, new_doc_xml)
            zout.writestr(paths.rels_xml, new_rels_xml)

            # Add images into word/media/
            with open(img1_path, "rb") as f:
                zout.writestr(f"{paths.media_dir}{img1_name}", f.read())
            with open(img2_path, "rb") as f:
                zout.writestr(f"{paths.media_dir}{img2_name}", f.read())

    print(f"[OK] Wrote: {out_docx}")
    print(f"     Image rels: {rid1} -> media/{img1_name}, {rid2} -> media/{img2_name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True, help="Path to an existing .docx used as template")
    ap.add_argument("--out", required=True, help="Output .docx path")
    ap.add_argument("--xml", required=True, help="Path to XML fragment file (contains <w:tbl>...)")
    ap.add_argument("--img1", required=True, help="First image path (left)")
    ap.add_argument("--img2", required=True, help="Second image path (right)")
    args = ap.parse_args()

    inject_fragment_into_docx(
        template_docx=args.template,
        out_docx=args.out,
        fragment_xml_path=args.xml,
        img1_path=args.img1,
        img2_path=args.img2,
    )


if __name__ == "__main__":
    main()