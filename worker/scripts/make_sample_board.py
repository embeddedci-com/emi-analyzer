"""Write the public sample board: tests/fixtures/sample.kicad_pcb.

A small synthetic 4-layer board, drawn so that every quick analysis has something to show:

  * J1, a micro-USB connector at the left edge. /USB_DP and /USB_DN run through the ESD array
    D1 right at the connector and on to the MCU U1: the ESD simulation has two clean lines,
    and the Cables tab suggests a USB cable.
  * J2, a 3-pin header at the right edge. /UART_TX reaches U1 before its clamp D2, and
    /UART_RX has no clamp at all.
  * /SPI_CLK, a clock from U1 to the flash U3, crosses a slot in the In1.Cu ground plane.
  * /SPI_MOSI changes layer twice with no ground via nearby.
  * C1 decouples U1 closely; C2 sits too far from U3.
  * /NRST has only a pull-up, and VBUS enters at J1 with no capacitor beside the connector.
  * /UART_TX and the other I/O lines are long enough to radiate at the default 1 GHz.

Not a real design: the traces are straight lines between pads, and the zone fills are the
zone outlines. KiCad opens it and refills the zones on the first edit. The worker's own tiny
test fixture stays as it is; this board is what the app's "Try the sample board" uploads.

    python scripts/make_sample_board.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "fixtures" / "sample.kicad_pcb"
APP_COPY = ROOT.parent / "webapp" / "src" / "assets" / "sample.kicad_pcb"

W, H = 70.0, 45.0

NETS = [
    "", "GND", "+3V3", "VBUS", "/USB_DP", "/USB_DN", "/UART_TX", "/UART_RX",
    "/SPI_CLK", "/SPI_MOSI", "/NRST", "/XIN", "/XOUT",
]
NET = {n: i for i, n in enumerate(NETS)}

_uuid = 0


def uuid() -> str:
    global _uuid
    _uuid += 1
    return f'(uuid "5a3b0000-0000-4000-8000-{_uuid:012x}")'


def net(name: str) -> str:
    return f'(net {NET[name]} "{name}")'


def footprint(ref: str, value: str, lib: str, x: float, y: float, pads: list[tuple]) -> str:
    """pads: (number, net, dx, dy, w, h) for SMD, or with a drill as a 7th item for through-hole."""
    th = any(len(p) > 6 for p in pads)
    lines = [
        f'  (footprint "{lib}" (layer "F.Cu") {uuid()}',
        f"    (at {x:g} {y:g})",
        f"    (attr {'through_hole' if th else 'smd'})",
        f'    (property "Reference" "{ref}" (at 0 -2.5 0) (layer "F.SilkS") {uuid()}',
        "      (effects (font (size 0.8 0.8) (thickness 0.12))))",
        f'    (property "Value" "{value}" (at 0 2.5 0) (layer "F.Fab") {uuid()}',
        "      (effects (font (size 0.8 0.8) (thickness 0.12))))",
    ]
    for p in pads:
        number, name, dx, dy, w, h = p[:6]
        if len(p) > 6:
            lines.append(
                f'    (pad "{number}" thru_hole circle (at {dx:g} {dy:g}) (size {w:g} {h:g}) '
                f'(drill {p[6]:g}) (layers "*.Cu" "*.Mask") {net(name)} {uuid()})'
            )
        else:
            lines.append(
                f'    (pad "{number}" smd rect (at {dx:g} {dy:g}) (size {w:g} {h:g}) '
                f'(layers "F.Cu" "F.Paste" "F.Mask") {net(name)} {uuid()})'
            )
    lines.append("  )")
    return "\n".join(lines)


def track(name: str, layer: str, *pts: tuple[float, float], width: float = 0.2) -> list[str]:
    return [
        f"  (segment (start {a[0]:g} {a[1]:g}) (end {b[0]:g} {b[1]:g}) (width {width:g}) "
        f'(layer "{layer}") (net {NET[name]}) {uuid()})'
        for a, b in zip(pts, pts[1:])
    ]


def via(name: str, x: float, y: float) -> str:
    return (f'  (via (at {x:g} {y:g}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
            f"(net {NET[name]}) {uuid()})")


def zone(name: str, layer: str, pts: list[tuple[float, float]]) -> str:
    xy = " ".join(f"(xy {x:g} {y:g})" for x, y in pts)
    return "\n".join([
        f'  (zone (net {NET[name]}) (net_name "{name}") (layer "{layer}") {uuid()}',
        "    (hatch edge 0.5)",
        "    (connect_pads (clearance 0.2))",
        "    (min_thickness 0.25) (filled_areas_thickness no)",
        "    (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))",
        f"    (polygon (pts {xy}))",
        f'    (filled_polygon (layer "{layer}") (pts {xy}))',
        "  )",
    ])


def board() -> str:
    parts = [
        # Micro-USB at the left edge: VBUS, D-, D+, GND.
        footprint("J1", "USB_B_Micro", "Connector_USB:USB_Micro-B_Molex-105017-0001", 2.5, 19, [
            ("1", "VBUS", 0, -3, 1.4, 0.45), ("2", "/USB_DN", 0, -1, 1.4, 0.45),
            ("3", "/USB_DP", 0, 1, 1.4, 0.45), ("5", "GND", 0, 3, 1.4, 0.45),
        ]),
        # USB ESD array right behind the connector.
        footprint("D1", "USBLC6-2SC6", "Package_TO_SOT_SMD:SOT-23-6", 8, 19, [
            ("1", "/USB_DN", -1, -1, 0.6, 0.6), ("3", "/USB_DP", -1, 1, 0.6, 0.6),
            ("2", "GND", 1, 0, 0.6, 0.6), ("5", "VBUS", 1, -1.5, 0.6, 0.6),
        ]),
        # 3.3 V regulator fed from VBUS.
        footprint("U4", "AP2112K-3.3", "Package_TO_SOT_SMD:SOT-23-5", 14, 7, [
            ("1", "VBUS", -1, 0, 0.6, 0.8), ("2", "GND", 0, 1.5, 0.6, 0.8),
            ("5", "+3V3", 1, 0, 0.6, 0.8),
        ]),
        footprint("C3", "1uF", "Capacitor_SMD:C_0603_1608Metric", 13.5, 3.5, [
            ("1", "VBUS", -0.8, 0, 0.8, 0.9), ("2", "GND", 0.8, 0, 0.8, 0.9),
        ]),
        footprint("C4", "1uF", "Capacitor_SMD:C_0603_1608Metric", 17, 4.5, [
            ("1", "+3V3", -0.8, 0, 0.8, 0.9), ("2", "GND", 0.8, 0, 0.8, 0.9),
        ]),
        # The MCU.
        footprint("U1", "STM32G031K8", "Package_QFP:LQFP-32_7x7mm_P0.8mm", 35, 20, [
            ("1", "+3V3", -1, -4, 0.5, 1.2), ("2", "GND", 1, -4, 0.5, 1.2),
            ("3", "/USB_DN", -4, -2, 1.2, 0.5), ("4", "/USB_DP", -4, 0, 1.2, 0.5),
            ("5", "/NRST", -4, 2, 1.2, 0.5), ("6", "/XIN", -4, 4, 1.2, 0.5),
            ("7", "/XOUT", -2, 4.5, 0.5, 1.2),
            ("8", "/UART_TX", 4, -2, 1.2, 0.5), ("9", "/UART_RX", 4, 0, 1.2, 0.5),
            ("10", "/SPI_CLK", 4, 2, 1.2, 0.5), ("11", "/SPI_MOSI", 4, 4, 1.2, 0.5),
        ]),
        footprint("C1", "100nF", "Capacitor_SMD:C_0402_1005Metric", 35, 13.5, [
            ("1", "+3V3", -1, 0, 0.6, 0.6), ("2", "GND", 1, 0, 0.6, 0.6),
        ]),
        footprint("R1", "10k", "Resistor_SMD:R_0402_1005Metric", 27, 22, [
            ("1", "+3V3", -1, 0, 0.6, 0.6), ("2", "/NRST", 1, 0, 0.6, 0.6),
        ]),
        footprint("Y1", "16MHz", "Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm", 27, 28, [
            ("1", "/XIN", -1.1, 0, 1.0, 1.0), ("3", "/XOUT", 1.1, 0, 1.0, 1.0),
        ]),
        # 3-pin UART header at the right edge.
        footprint("J2", "Conn_01x03", "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
                  67.5, 15, [
            ("1", "/UART_TX", 0, 0, 1.7, 1.7, 1.0), ("2", "/UART_RX", 0, 2.54, 1.7, 1.7, 1.0),
            ("3", "GND", 0, 5.08, 1.7, 1.7, 1.0),
        ]),
        # A TVS on /UART_TX, but past the MCU.
        footprint("D2", "PESD5V0S1BA", "Diode_SMD:D_SOD-323", 43, 8, [
            ("1", "/UART_TX", -1, 0, 0.6, 0.6), ("2", "GND", 1, 0, 0.6, 0.6),
        ]),
        # SPI flash in the lower right, its capacitor too far away.
        footprint("U3", "W25Q32JVSS", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", 58, 35, [
            ("1", "/SPI_CLK", -2, -1, 1.2, 0.6), ("2", "/SPI_MOSI", -2, 1, 1.2, 0.6),
            ("8", "+3V3", 2, -1, 1.2, 0.6), ("4", "GND", 2, 1, 1.2, 0.6),
        ]),
        footprint("C2", "100nF", "Capacitor_SMD:C_0402_1005Metric", 58, 26, [
            ("1", "+3V3", -1, 0, 0.6, 0.6), ("2", "GND", 1, 0, 0.6, 0.6),
        ]),
    ]

    copper: list[str] = []
    # USB: connector -> clamp -> MCU.
    copper += track("/USB_DN", "F.Cu", (2.5, 18), (7, 18), (31, 18))
    copper += track("/USB_DP", "F.Cu", (2.5, 20), (7, 20), (31, 20))
    copper += track("VBUS", "F.Cu", (2.5, 16), (5, 16), (9, 17.5))
    copper += track("VBUS", "F.Cu", (5, 16), (5, 7), (13, 7), width=0.5)
    copper += track("VBUS", "F.Cu", (12.7, 3.5), (12.7, 7), width=0.4)
    # UART: straight to the MCU; the clamp on TX hangs off past it.
    copper += track("/UART_TX", "F.Cu", (67.5, 15), (42, 15), (39, 18))
    copper += track("/UART_TX", "F.Cu", (42, 15), (42, 8))
    copper += track("/UART_RX", "F.Cu", (67.5, 17.54), (42, 17.54), (41, 20), (39, 20))
    # Clock to the flash, over the slot in the ground plane.
    copper += track("/SPI_CLK", "F.Cu", (39, 22), (44, 27), (53, 27), (56, 30), (56, 34))
    # Data changes layer twice, with no ground via near either change.
    copper += track("/SPI_MOSI", "F.Cu", (39, 24), (42, 27.5), (42, 31))
    copper += track("/SPI_MOSI", "B.Cu", (42, 31), (46, 38), (53, 38))
    copper += track("/SPI_MOSI", "F.Cu", (53, 38), (56, 36))
    # Reset pull-up and the crystal.
    copper += track("/NRST", "F.Cu", (28, 22), (31, 22))
    copper += track("/XIN", "F.Cu", (25.9, 28), (25.9, 26), (29, 24), (31, 24))
    copper += track("/XOUT", "F.Cu", (28.1, 28), (33, 28), (33, 24.5))
    # Supply pins to the planes.
    copper += track("+3V3", "F.Cu", (15, 7), (17, 7), width=0.4)
    copper += track("+3V3", "F.Cu", (16.2, 4.5), (16.2, 7), width=0.4)
    copper += track("+3V3", "F.Cu", (34, 16), (34, 13.5))
    copper += track("+3V3", "F.Cu", (26, 22), (26, 20.5))
    copper += track("+3V3", "F.Cu", (60, 34), (61.5, 34))
    copper += track("+3V3", "F.Cu", (57, 26), (57, 24.5))

    vias = [
        via("/SPI_MOSI", 42, 31), via("/SPI_MOSI", 53, 38),
        via("+3V3", 17, 7), via("+3V3", 33, 13.5), via("+3V3", 26, 20.5),
        via("+3V3", 61.5, 34), via("+3V3", 57, 24.5),
        via("GND", 2.5, 23.5), via("GND", 10, 19), via("GND", 14, 9.8), via("GND", 15.1, 3.5), via("GND", 18.6, 4.5),
        via("GND", 37, 13.5), via("GND", 37.5, 16), via("GND", 45, 8), via("GND", 61.5, 36.5),
        via("GND", 60, 26),
    ]
    copper += [via("GND", 1.0 + 4.0 * i, 1.5) for i in range(17)]

    zones = [
        # Ground on In1.Cu, with a slot cut up from the bottom edge under the clock.
        zone("GND", "In1.Cu", [(0.5, 0.5), (69.5, 0.5), (69.5, 44.5), (51, 44.5), (51, 24),
                                (49, 24), (49, 44.5), (0.5, 44.5)]),
        zone("+3V3", "In2.Cu", [(0.5, 0.5), (69.5, 0.5), (69.5, 44.5), (0.5, 44.5)]),
    ]

    head = f'''(kicad_pcb
  (version 20241229)
  (generator "emi-analyzer-sample")
  (generator_version "9.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (4 "In1.Cu" power)
    (6 "In2.Cu" power)
    (2 "B.Cu" signal)
    (13 "F.Paste" user)
    (15 "B.Paste" user)
    (5 "F.SilkS" user "F.Silkscreen")
    (7 "B.SilkS" user "B.Silkscreen")
    (1 "F.Mask" user)
    (3 "B.Mask" user)
    (25 "Edge.Cuts" user)
    (31 "F.CrtYd" user "F.Courtyard")
    (35 "F.Fab" user)
  )
  (setup
    (stackup
      (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "prepreg") (thickness 0.2104) (material "FR4") (epsilon_r 4.4) (loss_tangent 0.02))
      (layer "In1.Cu" (type "copper") (thickness 0.0152))
      (layer "dielectric 2" (type "core") (thickness 1.065) (material "FR4") (epsilon_r 4.6) (loss_tangent 0.02))
      (layer "In2.Cu" (type "copper") (thickness 0.0152))
      (layer "dielectric 3" (type "prepreg") (thickness 0.2104) (material "FR4") (epsilon_r 4.4) (loss_tangent 0.02))
      (layer "B.Cu" (type "copper") (thickness 0.035))
      (layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01))
      (copper_finish "None")
      (dielectric_constraints no)
    )
    (pad_to_mask_clearance 0)
  )
'''
    nets = "\n".join(f'  (net {i} "{n}")' for i, n in enumerate(NETS))
    outline = f'  (gr_rect (start 0 0) (end {W:g} {H:g}) (stroke (width 0.1) (type default)) (fill no) (layer "Edge.Cuts") {uuid()})'
    return "\n".join([head + nets, outline, *parts, *copper, *vias, *zones, ")", ""])


def main() -> None:
    text = board()
    OUT.write_text(text)
    APP_COPY.write_text(text)
    print(f"wrote {OUT} and {APP_COPY}")


if __name__ == "__main__":
    main()
