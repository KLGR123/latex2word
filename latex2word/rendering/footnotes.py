from __future__ import annotations

import copy
from typing import Any, Dict, Optional

from docx import Document
from lxml import etree

from .context import RenderContext
from .settings import FONTS, NS_W, SIZES
from .word_xml import fix_lxml_run_fonts

FN_TAG = f"{{{NS_W}}}footnote"
FN_REF_TAG = f"{{{NS_W}}}footnoteReference"
FN_ID_ATTR = f"{{{NS_W}}}id"
FN_TYPE_ATTR = f"{{{NS_W}}}type"
FN_SKIP_TYPES = frozenset({"separator", "continuationSeparator"})

FN_SKELETON_XML = (
    '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:footnote w:id="-1" w:type="separator">'
    '<w:p><w:r><w:separator/></w:r></w:p>'
    '</w:footnote>'
    '<w:footnote w:id="0" w:type="continuationSeparator">'
    '<w:p><w:r><w:continuationSeparator/></w:r></w:p>'
    '</w:footnote>'
    '</w:footnotes>'
)


def init_footnotes(doc: Document, ctx: RenderContext) -> None:
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import XmlPart
    from docx.oxml import parse_xml

    part = doc.part
    fn_element = None
    for rel in part.rels.values():
        if rel.reltype == RT.FOOTNOTES:
            target_part = rel.target_part
            if hasattr(target_part, "_element"):
                fn_element = target_part._element
            elif hasattr(target_part, "blob"):
                upgraded_part = XmlPart.load(
                    target_part.partname,
                    target_part.content_type,
                    target_part.blob,
                    part.package,
                )
                rel._target = upgraded_part
                fn_element = upgraded_part.element
                print("  [footnotes] Upgraded existing footnotes part to XML-backed part.")
            break

    if fn_element is None:
        fn_partname = PackURI("/word/footnotes.xml")
        fn_contenttype = (
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.footnotes+xml"
        )
        fn_element = parse_xml(FN_SKELETON_XML)
        fn_opc_part = XmlPart(fn_partname, fn_contenttype, fn_element, part.package)
        part.relate_to(fn_opc_part, RT.FOOTNOTES)
        print("  [footnotes] Created new footnotes part.")

    ctx.footnotes_root = fn_element
    max_id = max(
        (
            int(fn.get(FN_ID_ATTR, "0"))
            for fn in ctx.footnotes_root.findall(FN_TAG)
            if fn.get(FN_TYPE_ATTR, "") not in FN_SKIP_TYPES
        ),
        default=0,
    )
    ctx.footnote_next_id = max(max_id + 1, 1)
    print(f"  [footnotes] Initialised -- next ID = {ctx.footnote_next_id}.")


def fix_footnote_fonts(fn_elem) -> None:
    fn_size = SIZES.body - 1
    for r_elem in fn_elem.iter(f"{{{NS_W}}}r"):
        fix_lxml_run_fonts(r_elem, FONTS.body, fn_size)


def merge_pandoc_footnotes(doc_tree, fn_xml_bytes: Optional[bytes], ctx: RenderContext) -> None:
    if fn_xml_bytes is None or ctx.footnotes_root is None:
        return
    try:
        fn_tree = etree.fromstring(fn_xml_bytes)
    except Exception as exc:
        print(f"  [footnotes] Could not parse footnotes.xml: {exc}")
        return

    pandoc_fns: Dict[int, Any] = {
        int(fn.get(FN_ID_ATTR, "")): fn
        for fn in fn_tree.findall(FN_TAG)
        if fn.get(FN_TYPE_ATTR, "") not in FN_SKIP_TYPES
        and fn.get(FN_ID_ATTR, "").lstrip("-").isdigit()
    }
    if not pandoc_fns:
        return

    id_map = {old: ctx.footnote_next_id + i for i, old in enumerate(sorted(pandoc_fns))}
    ctx.footnote_next_id += len(id_map)

    for ref in doc_tree.iter(FN_REF_TAG):
        try:
            new_id = id_map.get(int(ref.get(FN_ID_ATTR, "")))
            if new_id is not None:
                ref.set(FN_ID_ATTR, str(new_id))
        except (ValueError, TypeError):
            pass

    for old_id, new_id in id_map.items():
        fn_elem = copy.deepcopy(pandoc_fns[old_id])
        fn_elem.set(FN_ID_ATTR, str(new_id))
        fix_footnote_fonts(fn_elem)
        ctx.footnotes_root.append(fn_elem)
