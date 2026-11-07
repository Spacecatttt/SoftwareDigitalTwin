"""Generates diagram_graph.html: SVG of the twin graph of one printer (Eclipse Ditto, lab 4), sized for an A4 page.
Run:  python report/lab4/gen_graph_svg.py report/lab4/diagram_graph.html"""
import sys

OUT = sys.argv[1]
W, H = 1000, 640
BW, BH = 222, 92
COLS = [28, 268, 508, 748]
ROW1, ROW2 = 300, 470
NODES = {  # suffix: (column, row y, kind, feature lines)
    "controller": (0, ROW1, "Controller", ["connectivity: status,", "lastEventAt, messageRateHz"]),
    "hotend": (1, ROW1, "Hotend", ["heater: temperature, thermalState,", "дія emergencyCooldown"]),
    "extruder": (2, ROW1, "Extruder", ["drive: feedRate,", "extrudedLengthMm, filamentUsedG"]),
    "fan": (3, ROW1, "Fan", ["fan: speedPercent"]),
    "psu": (0, ROW2, "PowerSupply", ["без features; атрибути:", "ratedPowerW, outputVoltageV"]),
    "bed": (1, ROW2, "HeatedBed", ["temperature: temperature"]),
    "axis-x": (2, ROW2, "StepperAxis", ["motor: current, currentRms5s,", "loadState"]),
    "enclosure": (3, ROW2, "Enclosure", ["temperature: temperature"]),
}
C = {"gray": "#52514e", "blue": "#2a78d6", "orange": "#eb6834", "aqua": "#1baf7a"}
out = []


def x(n):
    return COLS[NODES[n][0]]


def cx(n):
    return x(n) + BW / 2


def y(n):
    return NODES[n][1]


def box(x0, y0, w, h, title, kind, lines, fill="#f0efec", stroke="#c3c2b7"):
    out.append(f'<rect x="{x0}" y="{y0}" width="{w}" height="{h}" rx="7" fill="{fill}" stroke="{stroke}"/>')
    out.append(f'<text class="t" x="{x0 + 10}" y="{y0 + 21}">{title}</text>')
    out.append(f'<text class="k" x="{x0 + 10}" y="{y0 + 39}">{kind}</text>')
    for i, l in enumerate(lines):
        out.append(f'<text class="s" x="{x0 + 10}" y="{y0 + 59 + i * 17}">{l}</text>')


def line(pts, color, dash=False, arrow=True, width=1.8):
    d = "M " + " L ".join(f"{a:.1f} {b:.1f}" for a, b in pts)
    out.append(f'<path d="{d}" fill="none" stroke="{C[color]}" stroke-width="{width}"'
               + (' stroke-dasharray="6 4"' if dash else "") + (f' marker-end="url(#{color})"' if arrow else "") + "/>")


def label(x0, y0, text, color="gray", anchor="start"):
    out.append(f'<text class="lbl" x="{x0}" y="{y0}" fill="{C[color]}" text-anchor="{anchor}">{text}</text>')


# farm and printers
box(320, 16, 360, 74, "kpi.dt:farm-01", "PrintFarm", ["summary: printersTotal, printersOnline, printersInAlarm"],
    "#fdefe8", "#eb6834")
box(28, 128, 462, 92, "kpi.dt:printer_01", "Printer",
    ["status: operatingMode, healthIndex, dataQualityPct, …", "alarms: active, level, message, source, count"],
    "#eaf2fc", "#2a78d6")
box(520, 128, 220, 74, "kpi.dt:printer_02", "Printer", ["+ 8 двійників підсистем"])
box(760, 128, 212, 74, "kpi.dt:printer_03", "Printer", ["+ 8 двійників підсистем"])
line([(259, 128), (259, 108), (440, 108), (440, 92)], "gray")
line([(630, 128), (630, 108), (520, 108), (520, 92)], "gray")
line([(866, 128), (866, 108), (600, 108), (600, 92)], "gray")
label(270, 103, "isPartOf")

# subsystems of printer_01 inside a group: each of them isPartOf printer_01
out.append('<rect x="16" y="252" width="968" height="322" rx="10" fill="none" stroke="#898781" stroke-dasharray="3 4"/>')
label(28, 270, "kpi.dt:printer_01.<підсистема>", "gray")
line([(259, 252), (259, 222)], "gray")
label(268, 242, "isPartOf (кожна з 8 підсистем)")
for n, (col, row, kind, lines) in NODES.items():
    fill, stroke = ("#e6f6f0", "#1baf7a") if kind in ("Controller", "PowerSupply") else ("#fcfcfb", "#c3c2b7")
    box(COLS[col], row, BW, BH, n, kind, lines, fill, stroke)

# channel between the rows: controlledBy (blue) at y=bus_c, poweredBy (orange) at y=bus_p
bus_c, bus_p = ROW1 + BH + 22, ROW1 + BH + 50
actuators_top = ["hotend", "extruder", "fan"]
actuators_bottom = ["bed", "axis-x"]
line([(cx("fan") - 30, bus_c), (cx("controller") - 30, bus_c), (cx("controller") - 30, ROW1 + BH + 2)], "blue", True)
for n in actuators_top:
    line([(cx(n) - 30, ROW1 + BH), (cx(n) - 30, bus_c)], "blue", True, False)
for n in actuators_bottom:
    line([(cx(n) - 30, ROW2), (cx(n) - 30, bus_c)], "blue", True, False)
label(cx("controller") + 38, bus_c - 6, "controlledBy", "blue")
line([(cx("fan") + 30, bus_p), (cx("psu") + 30, bus_p), (cx("psu") + 30, ROW2 - 2)], "orange", True)
for n in ["controller"] + actuators_top:
    line([(cx(n) + 30, ROW1 + BH), (cx(n) + 30, bus_p)], "orange", True, False)
for n in actuators_bottom:
    line([(cx(n) + 30, ROW2), (cx(n) + 30, bus_p)], "orange", True, False)
label(cx("psu") + 38, bus_p + 15, "poweredBy", "orange")

# hotend -> extruder (fedBy), hotend -> fan (cooledBy): above the first row
arc_y = ROW1 - 16
line([(cx("hotend") + 50, ROW1), (cx("hotend") + 50, arc_y), (cx("extruder") - 50, arc_y), (cx("extruder") - 50, ROW1 - 2)], "aqua")
label(cx("hotend") + 6, arc_y - 6, "fedBy", "aqua", "end")
line([(cx("hotend") + 80, ROW1), (cx("hotend") + 80, arc_y - 22), (cx("fan") - 40, arc_y - 22), (cx("fan") - 40, ROW1 - 2)], "aqua")
label(cx("extruder") + 60, arc_y - 6, "cooledBy", "aqua")

# legend
ly = H - 30
x0 = 24
for color, dash, text in [("gray", False, "isPartOf — входить до складу"), ("blue", True, "controlledBy — керується"),
                          ("orange", True, "poweredBy — живиться від"), ("aqua", False, "cooledBy / fedBy")]:
    line([(x0, ly), (x0 + 36, ly)], color, dash)
    label(x0 + 44, ly + 4, text, color)
    x0 += 44 + len(text) * 7.4 + 34

markers = "".join(f'<marker id="{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
                  f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{v}"/></marker>' for k, v in C.items())
html = f'''<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><title>Twin Graph</title>
<style>
  body {{ margin: 0; background: #fcfcfb; font-family: "Segoe UI", system-ui, sans-serif; }}
  svg {{ display: block; }}
  .t {{ font-size: 15px; font-weight: 650; fill: #0b0b0b; }}
  .k {{ font-size: 13px; font-weight: 600; fill: #898781; }}
  .s {{ font-size: 13px; fill: #52514e; }}
  .lbl {{ font-size: 13px; font-weight: 600; }}
</style></head><body>
<!-- generated by gen_graph_svg.py: twin graph of printer_01 in Eclipse Ditto -->
<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">
<defs>{markers}</defs>
<rect width="{W}" height="{H}" fill="#fcfcfb"/>
{chr(10).join(out)}
</svg></body></html>
'''
open(OUT, "w", encoding="utf-8").write(html)
