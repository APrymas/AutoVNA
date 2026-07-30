from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


def generate_pdf_report(
    output_path: Path,
    title: str,
    device_info: dict[str, Any],
    measurement_info: dict[str, Any],
    calibration_info: dict[str, Any] | None,
    marker_rows: list[list[str]],
    uncertainty_note: str,
    description: dict[str, str] | None = None,
    plot_paths: Iterable[Path] = (),
    image_paths: Iterable[Path] = (),
    saved_files: Iterable[Path] = (),
    language: str = "pl",
) -> Path:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak
    except ImportError as exc:
        raise RuntimeError("Install the reportlab package to generate PDF reports.") from exc

    en = language == "en"
    text = {
        "page": "Page" if en else "Strona",
        "report_date": "Report date" if en else "Data raportu",
        "device": "Device" if en else "Urządzenie",
        "field": "Field" if en else "Pole",
        "value": "Value" if en else "Wartość",
        "unavailable": "unavailable" if en else "niedostępne",
        "measurement": "Measurement parameters" if en else "Parametry pomiaru",
        "calibration": "Calibration" if en else "Kalibracja",
        "no_cal": "No calibration profile is available for the current sweep." if en else "Brak profilu kalibracji dla aktualnego zakresu.",
        "markers": "Markers and uncertainty" if en else "Znaczniki i niepewność",
        "marker": "Marker" if en else "Znacznik",
        "description": "Test conditions and description" if en else "Warunki i opis badania",
        "photos": "Test setup photographs" if en else "Zdjęcia stanowiska",
        "image_error": "Could not embed image" if en else "Nie udało się osadzić obrazu",
        "files": "Series files" if en else "Pliki serii",
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    base_font, bold_font = "Helvetica", "Helvetica-Bold"
    candidates = [
        (Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/arialbd.ttf")),
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
    ]
    for regular, bold in candidates:
        if regular.exists() and bold.exists():
            pdfmetrics.registerFont(TTFont("NanoRegular", str(regular)))
            pdfmetrics.registerFont(TTFont("NanoBold", str(bold)))
            base_font, bold_font = "NanoRegular", "NanoBold"
            break
    for style in styles.byName.values():
        style.fontName = base_font
    styles["Title"].fontName = bold_font
    styles["Heading1"].fontName = bold_font
    styles.add(ParagraphStyle(name="SmallNano", parent=styles["BodyText"], fontSize=8.2, leading=10.2))

    doc = SimpleDocTemplate(
        str(output_path), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm, title=title, author="AutoNanoVNA",
    )

    def footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont(base_font, 8)
        canvas.drawString(15 * mm, 8 * mm, "AutoNanoVNA")
        canvas.drawRightString(195 * mm, 8 * mm, f"{text['page']} {doc_obj.page}")
        canvas.restoreState()

    story: list[Any] = [
        Paragraph(title, styles["Title"]),
        Paragraph(f"{text['report_date']}: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", styles["BodyText"]),
        Spacer(1, 5 * mm), Paragraph(text["device"], styles["Heading1"]),
    ]
    device_rows = [[text["field"], text["value"]]] + [[str(k), str(v or text["unavailable"])] for k, v in device_info.items()]
    table = Table(device_rows, colWidths=[55 * mm, 125 * mm])
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), base_font), ("FONTNAME", (0, 0), (-1, 0), bold_font),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D9EAF7")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story += [table, Spacer(1, 5 * mm), Paragraph(text["measurement"], styles["Heading1"])]
    meas_rows = [[text["field"], text["value"]]] + [[str(k), str(v)] for k, v in measurement_info.items()]
    mt = Table(meas_rows, colWidths=[70 * mm, 110 * mm])
    mt.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), base_font), ("FONTNAME", (0, 0), (-1, 0), bold_font),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D9EAF7")), ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
    ]))
    story.append(mt)

    story += [Spacer(1, 5 * mm), Paragraph(text["calibration"], styles["Heading1"])]
    if calibration_info:
        for k, v in calibration_info.items():
            story.append(Paragraph(f"<b>{k}:</b> {v}", styles["BodyText"]))
    else:
        story.append(Paragraph(text["no_cal"], styles["BodyText"]))

    story += [Spacer(1, 5 * mm), Paragraph(text["markers"], styles["Heading1"])]
    if marker_rows:
        marker_table = Table([[text["marker"], "f", "S11", "S21", "Z(S11)", "VSWR"]] + marker_rows,
                             colWidths=[18 * mm, 27 * mm, 34 * mm, 34 * mm, 44 * mm, 23 * mm])
        marker_table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), base_font), ("FONTNAME", (0, 0), (-1, 0), bold_font),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D9EAF7")),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 7.4),
        ]))
        story.append(marker_table)
    story += [Spacer(1, 2 * mm), Paragraph(uncertainty_note, styles["SmallNano"])]

    if description:
        story += [Spacer(1, 5 * mm), Paragraph(text["description"], styles["Heading1"])]
        for key, value in description.items():
            if value:
                story.append(Paragraph(f"<b>{key}:</b> {value}", styles["BodyText"]))

    for plot in plot_paths:
        if Path(plot).exists():
            story += [PageBreak(), Paragraph(Path(plot).stem, styles["Heading1"]), Spacer(1, 2 * mm),
                      Image(str(plot), width=180 * mm, height=105 * mm)]

    valid_images = [Path(p) for p in image_paths if Path(p).exists()]
    if valid_images:
        story += [PageBreak(), Paragraph(text["photos"], styles["Heading1"])]
        for image in valid_images:
            try:
                story += [Paragraph(image.name, styles["SmallNano"]), Image(str(image), width=165 * mm, height=105 * mm), Spacer(1, 4 * mm)]
            except Exception:
                story.append(Paragraph(f"{text['image_error']}: {image}", styles["SmallNano"]))

    saved = [Path(p) for p in saved_files]
    if saved:
        story += [PageBreak(), Paragraph(text["files"], styles["Heading1"])]
        for item in saved:
            story.append(Paragraph(item.name, styles["SmallNano"]))

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output_path
