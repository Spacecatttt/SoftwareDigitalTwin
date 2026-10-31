"""Builds the DOCX report from the Markdown source:  python report/build_report.py"""
import subprocess
from pathlib import Path

import pypandoc
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

HERE = Path(__file__).resolve().parent
SRC = HERE / "Звіт_ЛР3.md"
OUT = HERE / "Звіт_ЛР3.docx"
REF = HERE / "reference.docx"


def _font(style, name="Times New Roman", size=14, bold=None, color=None):
    style.font.name = name
    style.font.size = Pt(size)
    if bold is not None:
        style.font.bold = bold
    if color is not None:
        style.font.color.rgb = RGBColor.from_string(color)
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(qn(attr), name)
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme"):   # theme fonts win otherwise
        rfonts.attrib.pop(qn(attr), None)


def build_reference():
    """Reference document for pandoc: Times New Roman 14, 1.5 spacing, margins 25/15/20/20 mm."""
    subprocess.run([pypandoc.get_pandoc_path(), "-o", str(REF), "--print-default-data-file", "reference.docx"],
                   check=True)
    doc = Document(str(REF))
    for s in doc.sections:
        s.left_margin, s.right_margin, s.top_margin, s.bottom_margin = Cm(2.5), Cm(1.5), Cm(2), Cm(2)
    st = {s.name: s for s in doc.styles}
    for name in ("Normal", "Body Text", "First Paragraph", "Compact"):
        if name in st:
            _font(st[name])
            pf = st[name].paragraph_format
            pf.line_spacing = 1.5
            pf.space_after = Pt(0)
            if name in ("Body Text", "First Paragraph"):
                pf.first_line_indent = Cm(1.25)
                pf.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    for name, size in (("Title", 16), ("Subtitle", 14), ("Heading 1", 15), ("Heading 2", 14), ("Heading 3", 14)):
        if name in st:
            _font(st[name], size=size, bold=True, color="000000")
            st[name].paragraph_format.space_before = Pt(12)
            st[name].paragraph_format.space_after = Pt(6)
    for name in ("Source Code", "Verbatim Char"):
        if name in st:
            _font(st[name], name="Consolas", size=10)
    if "Source Code" in st:
        st["Source Code"].paragraph_format.line_spacing = 1.0
    for name in ("Caption", "Image Caption", "Table Caption"):
        if name in st:
            _font(st[name], size=12, bold=False, color="000000")
            st[name].font.italic = False
            st[name].paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.save(str(REF))


def postprocess():
    """Tables: 11 pt single spacing with borders; figures centred."""
    doc = Document(str(OUT))
    for table in doc.tables:
        table.style = doc.styles["Table"] if "Table" in [s.name for s in doc.styles] else table.style
        tbl = table._tbl
        borders = tbl.tblPr.find(qn("w:tblBorders"))
        if borders is None:
            borders = tbl.tblPr.makeelement(qn("w:tblBorders"), {})
            tbl.tblPr.append(borders)
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
            el = borders.makeelement(qn(f"w:{edge}"), {qn("w:val"): "single", qn("w:sz"): "4", qn("w:color"): "808080"})
            borders.append(el)
        for cell in table.rows[0].cells:      # header row: bold on light grey
            tc_pr = cell._tc.get_or_add_tcPr()
            tc_pr.append(tc_pr.makeelement(qn("w:shd"), {qn("w:val"): "clear", qn("w:color"): "auto", qn("w:fill"): "E7E6E6"}))
            for par in cell.paragraphs:
                for r in par.runs:
                    r.font.bold = True
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    p.paragraph_format.line_spacing = 1.0
                    p.paragraph_format.first_line_indent = Cm(0)
                    p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
                    for r in p.runs:
                        r.font.size = Pt(11)
    for p in doc.paragraphs:
        if p._p.xpath(".//w:drawing"):
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.first_line_indent = Cm(0)
    doc.save(str(OUT))


if __name__ == "__main__":
    build_reference()
    pypandoc.convert_file(str(SRC), "docx", outputfile=str(OUT),
                          extra_args=[f"--reference-doc={REF}", f"--resource-path={HERE}"])
    postprocess()
    print("->", OUT)
