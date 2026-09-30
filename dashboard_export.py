"""
Management dashboard workbook
=============================
Rebuilds the "Unilever Violations - Dashboard" workbook from any frozen portal report.

The hand-made version of that workbook was a one-off: someone exported a Wialon notification list for
16-17 September, pasted it into a sheet, parsed a Region out of each event text, marked every row
Genuine or False by hand, and wrote COUNTIF tables to drive four charts. Everything in it except the
Genuine/False judgement is already in the portal, so this module reproduces the same three sheets for
whatever period the portal has loaded:

  Dashboard        headline numbers and the four charts
  Violation Data   one row per violation, same fourteen columns as the original, plus coordinates
                   and the fatigue timings the original had no way to carry
  Summary          the COUNTIF tables the charts read

Remarks (Genuine / False) is a reviewer's decision, not something Wialon reports, and nothing in the
data predicts it - in the original workbook the same vehicle and event type was marked both ways, and a
109 km/h overspeed was dismissed while an 86 km/h one was upheld. It is filled from the reviews made in
the portal and left empty (with a dropdown) where none was made. Change it and every number and chart
follows, because the workbook keeps the formulas live rather than pasting values.
"""

import io
import re
from collections import Counter

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from regions import ALIASES, CITIES, city_in, group_region  # noqa: F401  (shared with the portal)
from dashboard_data import speed_of   # the same speed the portal dashboard uses

# Wialon writes the region into the notification text as "Region: <name>" before the verb.
# Palette: Space Cadet (#25344F), Slate Gray (#617891), Tan (#D5B893), Coffee (#6F4D38), Caput Mortuum (#632024)
SPACE_CADET = "25344F"
LIGHT_TAN = "F5EFE6"
SLATE_GRAY = "617891"
GREY = "617891"
THIN = Side(style="thin", color="C3D0DE")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
REMARKS = {"GENUINE": "Genuine", "FALSE": "False"}   # portal review verdict -> Remarks column
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def region_of(violation):
    """(region as written, grouped city, where it came from) for one violation. The portal attaches the
    vehicle's own region (from its Wialon record); without that, the Region tag in the alert text."""
    if violation.get("vehicle_region"):
        return (violation.get("vehicle_region_raw") or violation["vehicle_region"],
                violation["vehicle_region"], violation.get("vehicle_region_source") or "Vehicle record")
    match = REGION_IN_TEXT.search(violation.get("details") or "")
    if match and match.group(1):
        raw = match.group(1).strip()
        return raw, group_region(raw) or raw, "Event text"
    city = city_in(violation.get("location") or violation.get("address") or "")
    if city:                                 # no tag in the text: read the city off the address
        return "(not in event text) " + city, city, "Derived from Location"
    return "(unknown)", "Unknown", "Not stated"


def date_parts(stamp):
    """'2026-09-16 16:07:35' -> ('16-Sep-2026', '16-Sep-2026 16:07:35', 16)."""
    text = str(stamp or "")
    try:
        month, day, hour = int(text[5:7]), int(text[8:10]), int(text[11:13])
        date = "%02d-%s-%s" % (day, MONTHS[month - 1], text[0:4])
        return date, date + " " + text[11:19], hour
    except (ValueError, IndexError):
        return text, text, None


def sheet_head(ws, titles, widths):
    for index, (title, width) in enumerate(zip(titles, widths), 1):
        cell = ws.cell(row=1, column=index, value=title)
        cell.font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=SPACE_CADET)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.row_dimensions[1].height = 30


def table_head(ws, row, col, titles, widths):
    for offset, (title, width) in enumerate(zip(titles, widths)):
        cell = ws.cell(row=row, column=col + offset, value=title)
        cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=SPACE_CADET)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER
        ws.column_dimensions[get_column_letter(col + offset)].width = width


DATA_COLUMNS = [
    ("S.No", 7, "center", False, "0"),
    ("Vehicle", 15, "center", False, None),
    ("Driver", 24, "left", False, None),
    ("Event time", 22, "center", False, None),
    ("Date", 14, "center", False, None),
    ("Hour", 7, "center", False, "0"),
    ("Event name", 30, "left", False, None),
    ("Remarks", 13, "center", False, None),
    ("Region", 28, "left", False, None),
    ("Region (Grouped)", 18, "left", False, None),
    ("Region Source", 18, "center", False, None),
    ("Speed (km/h)", 12, "center", False, "0"),
    ("Location", 46, "left", True, None),
    ("Event text", 80, "left", True, None),
    ("Latitude", 12, "center", False, "0.000000"),
    ("Longitude", 12, "center", False, "0.000000"),
    ("Continuous Drive (min)", 15, "center", False, "0.0"),
    ("Day / Night", 11, "center", False, None),
    ("Rest Taken (min)", 13, "center", False, "0.0"),
]


def build_workbook(payload, type_labels):
    """payload is the portal's report_payload(); type_labels is key -> label. Returns workbook bytes."""
    violations = sorted((v for item in payload["vehicles"] for v in item["violations"]),
                        key=lambda v: v["time_unix"])
    labels = list(type_labels.values())
    wb = Workbook()

    # ------------------------------------------------------------------ Violation Data
    data = wb.create_sheet("Violation Data")
    sheet_head(data, [c[0] for c in DATA_COLUMNS], [c[1] for c in DATA_COLUMNS])
    band = PatternFill("solid", fgColor=LIGHT_TAN)
    for number, vio in enumerate(violations, start=2):
        telemetry = vio.get("telemetry") or {}
        date, stamp, hour = date_parts(vio.get("time"))
        raw_region, grouped, source = region_of(vio)
        location, details = vio.get("location", ""), vio.get("details", "")
        values = [number - 1, vio.get("vehicle", "Unknown"), vio.get("driver", "Unassigned"),
                  stamp, date, hour, vio.get("type_label", vio.get("type")), REMARKS.get(vio.get("verdict")),
                  raw_region, grouped, source, speed_of(vio),
                  location, details, vio.get("lat"), vio.get("lon"),
                  vio.get("continuous_drive_minutes"), vio.get("period"), vio.get("rest_minutes")]
        for index, (value, column) in enumerate(zip(values, DATA_COLUMNS), 1):
            _title, _width, align, wrap, fmt = column
            cell = data.cell(row=number, column=index, value=value)
            cell.font = Font(name="Arial", size=10)
            cell.alignment = Alignment(horizontal=align, vertical="center", wrap_text=wrap)
            cell.border = BORDER
            if fmt and isinstance(value, (int, float)):
                cell.number_format = fmt
            if number % 2 == 0:
                cell.fill = band
        lines = max(len(str(details)) / 78.0, len(str(location)) / 44.0, 1)
        data.row_dimensions[number].height = 16 * min(int(lines) + (1 if lines % 1 else 0), 4)

    last = max(len(violations) + 1, 2)
    review = DataValidation(type="list", formula1='"Genuine,False"', allow_blank=True,
                            promptTitle="Review",
                            prompt="Mark the event Genuine or False. Every number and chart on the "
                                   "Dashboard follows this column.")
    data.add_data_validation(review)
    review.add("H2:H" + str(last))
    data.auto_filter.ref = "A1:" + get_column_letter(len(DATA_COLUMNS)) + str(last)
    data.freeze_panes = "A2"
    data.print_title_rows = "1:1"
    data.page_setup.orientation = "landscape"
    data.page_setup.fitToWidth = 1
    data.page_setup.fitToHeight = 0
    data.sheet_properties.pageSetUpPr.fitToPage = True
    data.sheet_view.showGridLines = False

    # ------------------------------------------------------------------ Summary
    def rng(col):
        return "'Violation Data'!$%s$2:$%s$%d" % (col, col, last)

    def judged(col, cell, verdict):
        return ('=SUMPRODUCT(--EXACT(%s,%s),--EXACT(UPPER(%s&""),"%s"))'
                % (rng(col), cell, rng("H"), verdict))

    summary = wb.create_sheet("Summary")
    regions = [r for r, _ in Counter(region_of(v)[1] for v in violations).most_common()] or ["Unknown"]
    plates = [v for v, _ in Counter(v.get("vehicle", "Unknown") for v in violations).most_common(10)] or ["-"]

    for title, col in [("Violations by Region", 1), ("Violations by Event Type", 6),
                       ("Top 10 Vehicles by Violations", 11), ("Hourly Violation Pattern", 14),
                       ("Region x Event Type", 17)]:
        head = summary.cell(row=1, column=col, value=title)
        head.font = Font(name="Arial", size=12, bold=True, color=SPACE_CADET)

    table_head(summary, 2, 1, ["Region", "Genuine", "False", "Pending", "Total"], [22, 11, 11, 11, 11])
    for i, region in enumerate(regions, start=3):
        summary.cell(row=i, column=1, value=region)
        summary.cell(row=i, column=2, value=judged("J", "$A%d" % i, "GENUINE"))
        summary.cell(row=i, column=3, value=judged("J", "$A%d" % i, "FALSE"))
        summary.cell(row=i, column=4, value="=$E%d-$B%d-$C%d" % (i, i, i))
        summary.cell(row=i, column=5, value="=COUNTIF(%s,$A%d)" % (rng("J"), i))

    table_head(summary, 2, 6, ["Event Type", "Genuine", "False", "Pending", "Total"], [30, 11, 11, 11, 11])
    for i, label in enumerate(labels, start=3):
        summary.cell(row=i, column=6, value=label)
        summary.cell(row=i, column=7, value=judged("G", "$F%d" % i, "GENUINE"))
        summary.cell(row=i, column=8, value=judged("G", "$F%d" % i, "FALSE"))
        summary.cell(row=i, column=9, value="=$J%d-$G%d-$H%d" % (i, i, i))
        summary.cell(row=i, column=10, value="=COUNTIF(%s,$F%d)" % (rng("G"), i))

    table_head(summary, 2, 11, ["Vehicle", "Violations"], [16, 12])
    for i, plate in enumerate(plates, start=3):
        summary.cell(row=i, column=11, value=plate)
        summary.cell(row=i, column=12, value="=COUNTIF(%s,$K%d)" % (rng("B"), i))

    table_head(summary, 2, 14, ["Hour", "Violations"], [10, 12])
    for hour in range(24):
        summary.cell(row=3 + hour, column=14, value="%02d:00" % hour)
        summary.cell(row=3 + hour, column=15, value="=COUNTIF(%s,%d)" % (rng("F"), hour))

    table_head(summary, 2, 17, ["Region"] + labels, [22] + [16] * len(labels))
    for i, region in enumerate(regions, start=3):
        summary.cell(row=i, column=17, value=region)
        for j in range(len(labels)):
            letter = get_column_letter(18 + j)
            summary.cell(row=i, column=18 + j,
                         value="=COUNTIFS(%s,$Q%d,%s,%s$2)" % (rng("J"), i, rng("G"), letter))

    for row in summary.iter_rows(min_row=3):
        for cell in row:
            if cell.value is not None:
                cell.font = Font(name="Arial", size=10)
                cell.alignment = Alignment(vertical="center",
                                           horizontal="left" if cell.column in (1, 6, 11, 17) else "center")
                cell.border = BORDER
    summary.sheet_view.showGridLines = False

    # ------------------------------------------------------------------ Dashboard
    dash = wb.active
    dash.title = "Dashboard"
    region_end, type_end = 2 + len(regions), 2 + len(labels)

    title = dash.cell(row=1, column=1, value="UNILEVER FLEET - DRIVER VIOLATION DASHBOARD")
    title.font = Font(name="Arial", size=20, bold=True, color=SPACE_CADET)
    dash.merge_cells("A1:R1")
    subtitle = dash.cell(row=3, column=1, value=(
        "Reporting period: %s - %s PKT  (%s)   |   Source: Wialon Remote API, read live by the "
        "violations portal   |   Generated %s PKT"
        % (payload["from"], payload["to"], payload["label"], payload["generated_at"])))
    subtitle.font = Font(name="Arial", size=10, italic=True, color=GREY)
    dash.merge_cells("A3:R3")

    cards = [
        ("TOTAL EVENTS", "=COUNTA(%s)" % rng("A"), "0"),
        ("GENUINE", '=SUMPRODUCT(--EXACT(UPPER(%s&""),"GENUINE"))' % rng("H"), "0"),
        ("FALSE", '=SUMPRODUCT(--EXACT(UPPER(%s&""),"FALSE"))' % rng("H"), "0"),
        ("PENDING REVIEW", "=$A$6-$C$6-$E$6", "0"),
        ("GENUINE %", '=IF($C$6+$E$6=0,"not reviewed",$C$6/($C$6+$E$6))', "0%"),
        ("VEHICLES INVOLVED", '=SUMPRODUCT((%s<>"")/COUNTIF(%s,%s&""))' % (rng("B"), rng("B"), rng("B")), "0"),
        ("REGIONS", '=SUMPRODUCT((%s<>"")/COUNTIF(%s,%s&""))' % (rng("J"), rng("J"), rng("J")), "0"),
        ("MAX SPEED (km/h)", "=IFERROR(MAX(%s),0)" % rng("L"), "0"),
        ("AVG O/S SPEED (km/h)", '=IFERROR(ROUND(AVERAGE(%s),1),"-")' % rng("L"), "0.0"),
    ]
    for index, (label, formula, fmt) in enumerate(cards):
        col = 1 + index * 2
        letter, letter2 = get_column_letter(col), get_column_letter(col + 1)
        dash.merge_cells("%s5:%s5" % (letter, letter2))
        dash.merge_cells("%s6:%s6" % (letter, letter2))
        head = dash.cell(row=5, column=col, value=label)
        head.font = Font(name="Arial", size=9, bold=True, color="FFFFFF")
        head.fill = PatternFill("solid", fgColor=SPACE_CADET)
        head.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        value = dash.cell(row=6, column=col, value=formula)
        value.font = Font(name="Arial", size=18, bold=True, color=SPACE_CADET)
        value.fill = PatternFill("solid", fgColor=LIGHT_TAN)
        value.alignment = Alignment(horizontal="center", vertical="center")
        value.number_format = fmt
        for row in (5, 6):
            for offset in (0, 1):
                dash.cell(row=row, column=col + offset).border = BORDER
    dash.row_dimensions[5].height = 26
    dash.row_dimensions[6].height = 34
    for col in range(1, 19):
        dash.column_dimensions[get_column_letter(col)].width = 9.5

    def sized(chart, chart_title):
        chart.title = chart_title
        chart.height, chart.width = 9.5, 17.5
        chart.style = 2
        chart.y_axis.title = "Violations"
        return chart

    by_region = sized(BarChart(), "Total Violations by Region")
    by_region.type = "col"
    by_region.add_data(Reference(summary, min_col=5, min_row=2, max_row=region_end), titles_from_data=True)
    by_region.set_categories(Reference(summary, min_col=1, min_row=3, max_row=region_end))
    dash.add_chart(by_region, "A9")

    by_type = sized(BarChart(), "Event Type - Genuine vs False vs Pending")
    by_type.type, by_type.grouping, by_type.overlap = "col", "stacked", 100
    by_type.add_data(Reference(summary, min_col=7, max_col=9, min_row=2, max_row=type_end), titles_from_data=True)
    by_type.set_categories(Reference(summary, min_col=6, min_row=3, max_row=type_end))
    dash.add_chart(by_type, "J9")

    hourly = sized(LineChart(), "Violations by Hour of Day")
    hourly.add_data(Reference(summary, min_col=15, min_row=2, max_row=26), titles_from_data=True)
    hourly.set_categories(Reference(summary, min_col=14, min_row=3, max_row=26))
    dash.add_chart(hourly, "A29")

    mix = sized(BarChart(), "Region-wise Violation Mix by Event Type")
    mix.type, mix.grouping, mix.overlap = "col", "stacked", 100
    mix.add_data(Reference(summary, min_col=18, max_col=17 + len(labels), min_row=2, max_row=region_end),
                 titles_from_data=True)
    mix.set_categories(Reference(summary, min_col=17, min_row=3, max_row=region_end))
    dash.add_chart(mix, "J29")

    derived = sum(1 for v in violations if region_of(v)[2] != "Event text")
    note = dash.cell(row=50, column=1, value=(
        'Note: "Region" is read from the Region tag inside each Wialon event text. %d of %d events '
        'carried no tag; those are marked "Derived from Location" on the data sheet and grouped by the '
        'city in their address. "Remarks" is a reviewer judgement, not something Wialon reports: fill '
        'the Genuine / False dropdown on the data sheet and every figure and chart above updates.'
        % (derived, len(violations))))
    note.font = Font(name="Arial", size=9, italic=True, color=GREY)
    note.alignment = Alignment(vertical="top", wrap_text=True)
    dash.merge_cells("A50:R51")

    dash.sheet_view.showGridLines = False
    dash.print_area = "A1:R52"
    dash.page_setup.orientation = "landscape"
    dash.page_setup.fitToWidth = 1
    dash.page_setup.fitToHeight = 0
    dash.sheet_properties.pageSetUpPr.fitToPage = True

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    return stream.getvalue()
