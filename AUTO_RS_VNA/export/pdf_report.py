from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from xml.sax.saxutils import escape


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
    labels: dict[str, str] | None = None,
) -> Path:
    labels = labels or {}
    t = lambda key, fallback: labels.get(key, fallback)
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak
    except ImportError as exc:
        raise RuntimeError(t("reportlab_missing", "Install the reportlab package to generate PDF reports.")) from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    base_font, bold_font = "Helvetica", "Helvetica-Bold"
    candidates = [
        (Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/arialbd.ttf")),
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
    ]
    for regular, bold in candidates:
        if regular.exists() and bold.exists():
            pdfmetrics.registerFont(TTFont("AutoRSRegular", str(regular)))
            pdfmetrics.registerFont(TTFont("AutoRSBold", str(bold)))
            base_font, bold_font = "AutoRSRegular", "AutoRSBold"
            break
    for style in styles.byName.values():
        style.fontName = base_font
    styles["Title"].fontName = bold_font
    styles["Heading1"].fontName = bold_font
    styles.add(ParagraphStyle(name="SmallAutoRS", parent=styles["BodyText"], fontSize=8.2, leading=10.2))

    doc = SimpleDocTemplate(
        str(output_path), pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
        title=title, author="AutoRS VNA",
    )

    def footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont(base_font, 8)
        canvas.drawString(15 * mm, 8 * mm, "AutoRS VNA")
        canvas.drawRightString(195 * mm, 8 * mm, t("report_page", "Page {page}").format(page=doc_obj.page))
        canvas.restoreState()

    story: list[Any] = [
        Paragraph(escape(title), styles["Title"]),
        Paragraph(f"{t('report_date', 'Report date')}: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", styles["BodyText"]),
        Spacer(1, 5 * mm),
        Paragraph(t("report_device", "Device"), styles["Heading1"]),
    ]
    device_rows = [[t("report_field", "Field"), t("report_value", "Value")]] + [[str(k), str(v or t("report_unavailable", "unavailable"))] for k, v in device_info.items()]
    table = Table(device_rows, colWidths=[55 * mm, 125 * mm])
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), base_font),
        ("FONTNAME", (0, 0), (-1, 0), bold_font),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D9EAF7")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story += [table, Spacer(1, 5 * mm), Paragraph(t("report_measurement_parameters", "Measurement parameters"), styles["Heading1"])]
    meas_rows = [[t("report_field", "Field"), t("report_value", "Value")]] + [[str(k), str(v)] for k, v in measurement_info.items()]
    mt = Table(meas_rows, colWidths=[70 * mm, 110 * mm])
    mt.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), base_font),
        ("FONTNAME", (0, 0), (-1, 0), bold_font),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D9EAF7")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
    ]))
    story.append(mt)

    story += [Spacer(1, 5 * mm), Paragraph(t("report_calibration", "Calibration"), styles["Heading1"])]
    if calibration_info:
        for k, v in calibration_info.items():
            story.append(Paragraph(f"<b>{escape(str(k))}:</b> {escape(str(v))}", styles["BodyText"]))
    else:
        story.append(Paragraph(t("report_correction_off", "Analyzer system correction is disabled or its state could not be confirmed."), styles["BodyText"]))

    story += [Spacer(1, 5 * mm), Paragraph(t("report_markers_uncertainty", "Markers and uncertainty"), styles["Heading1"])]
    if marker_rows:
        marker_table = Table([[t("report_marker", "Marker"), "f", "S11", "S21", "S12", "S22", "Z(S11)", "VSWR"]] + marker_rows,
                             colWidths=[13 * mm, 22 * mm, 24 * mm, 24 * mm, 24 * mm, 24 * mm, 28 * mm, 15 * mm])
        marker_table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), base_font),
            ("FONTNAME", (0, 0), (-1, 0), bold_font),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D9EAF7")),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.grey),
            ("FONTSIZE", (0, 0), (-1, -1), 7.4),
        ]))
        story.append(marker_table)
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(escape(uncertainty_note), styles["SmallAutoRS"]))

    if description:
        story += [Spacer(1, 5 * mm), Paragraph(t("report_conditions", "Test conditions and description"), styles["Heading1"])]
        for key, value in description.items():
            if value:
                story.append(Paragraph(f"<b>{escape(str(key))}:</b> {escape(str(value))}", styles["BodyText"]))

    for plot in plot_paths:
        if Path(plot).exists():
            plot_name = Path(plot).stem
            plot_title = t(f"report_plot_{plot_name}", plot_name.replace("_", " "))
            story += [PageBreak(), Paragraph(escape(plot_title), styles["Heading1"]), Spacer(1, 2 * mm),
                      Image(str(plot), width=180 * mm, height=105 * mm)]

    valid_images = [Path(p) for p in image_paths if Path(p).exists()]
    if valid_images:
        story += [PageBreak(), Paragraph(t("report_photos", "Test-setup images"), styles["Heading1"])]
        for image in valid_images:
            try:
                story += [Paragraph(escape(image.name), styles["SmallAutoRS"]), Image(str(image), width=165 * mm, height=105 * mm), Spacer(1, 4 * mm)]
            except Exception:
                story.append(Paragraph(t("report_image_error", "Could not embed image: {image}").format(image=image), styles["SmallAutoRS"]))

    saved = [Path(p) for p in saved_files]
    if saved:
        story += [PageBreak(), Paragraph(t("report_series_files", "Series files"), styles["Heading1"])]
        for p in saved:
            story.append(Paragraph(escape(p.name), styles["SmallAutoRS"]))

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output_path
