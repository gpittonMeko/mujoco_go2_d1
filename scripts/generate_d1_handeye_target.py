#!/usr/bin/env python3
"""Genera il target A4 metrico per la hand-eye D456/D1."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import cv2
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output" / "pdf" / "d1_handeye_aruco_4x4_50_id0_60mm.pdf"


def main() -> None:
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    marker = cv2.aruco.generateImageMarker(dictionary, 0, 1200, borderBits=1)
    ok, encoded = cv2.imencode(".png", marker)
    if not ok:
        raise RuntimeError("aruco_png_encode_failed")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    page_w, page_h = A4
    pdf = canvas.Canvas(str(OUTPUT), pagesize=A4, pageCompression=1)
    pdf.setTitle("D1 Hand-eye target - ArUco 4x4_50 ID 0 - 60 mm")

    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawCentredString(page_w / 2, page_h - 24 * mm, "D1 HAND-EYE CALIBRATION TARGET")
    pdf.setFont("Helvetica", 11)
    pdf.drawCentredString(page_w / 2, page_h - 32 * mm, "ArUco DICT_4X4_50 - ID 0 - black marker side: 60.0 mm")

    marker_side = 60 * mm
    quiet = 10 * mm
    box_side = marker_side + 2 * quiet
    x = (page_w - marker_side) / 2
    y = page_h - 125 * mm
    pdf.setFillColorRGB(1, 1, 1)
    pdf.rect(x - quiet, y - quiet, box_side, box_side, fill=1, stroke=0)
    pdf.drawImage(ImageReader(BytesIO(encoded.tobytes())), x, y, marker_side, marker_side, mask=None)
    pdf.setStrokeColorRGB(0.65, 0.65, 0.65)
    pdf.setLineWidth(0.25)
    pdf.rect(x - quiet, y - quiet, box_side, box_side, fill=0, stroke=1)

    pdf.setFillColorRGB(0, 0, 0)
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawCentredString(page_w / 2, y - 18 * mm, "PRINT AT 100% / ACTUAL SIZE - DO NOT FIT TO PAGE")
    pdf.setFont("Helvetica", 10)
    pdf.drawCentredString(page_w / 2, y - 25 * mm, "After printing, measure the black square: it must be 60.0 x 60.0 mm.")

    ruler_y = y - 45 * mm
    ruler_x = (page_w - 100 * mm) / 2
    pdf.setLineWidth(1.0)
    pdf.line(ruler_x, ruler_y, ruler_x + 100 * mm, ruler_y)
    for i in range(11):
        tick = 5 * mm if i in (0, 10) else 3 * mm
        pdf.line(ruler_x + i * 10 * mm, ruler_y - tick / 2, ruler_x + i * 10 * mm, ruler_y + tick / 2)
    pdf.setFont("Helvetica", 9)
    pdf.drawCentredString(page_w / 2, ruler_y - 8 * mm, "Control line: exactly 100 mm")

    text = pdf.beginText(24 * mm, 43 * mm)
    text.setFont("Helvetica", 9.5)
    text.setLeading(5 * mm)
    for line in (
        "Mount this sheet flat and rigid. Keep it fixed for the entire calibration.",
        "The D456 must see the complete marker, including the white quiet zone.",
        "Use 8-12 different wrist poses with both translation and rotation changes.",
        "The marker is needed only during hand-eye calibration, never during grasping.",
    ):
        text.textLine(line)
    pdf.drawText(text)
    pdf.setFont("Helvetica", 8)
    pdf.drawRightString(page_w - 15 * mm, 12 * mm, "mujoco_go2_d1 - target spec v1")
    pdf.showPage()
    pdf.save()
    print(OUTPUT)


if __name__ == "__main__":
    main()
