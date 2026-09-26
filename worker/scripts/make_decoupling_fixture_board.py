"""Write tests/fixtures/decoupling_demo.kicad_pcb: a small public board for the decoupling view.

Four layers with a ground plane on In1.Cu and a +3V3 plane on In2.Cu. U1, a microcontroller,
has four supply pins with a 100 nF at each, a 10 nF, a 16 MHz crystal and a 10 uF bulk
capacitor at the regulator U3. U2 sits on +1V8, which has no plane, with its only capacitor
7 mm away and no ground via at it: the rail the view should flag.

    python scripts/make_decoupling_fixture_board.py
"""

from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "decoupling_demo.kicad_pcb"

NETS = ["", "GND", "+3V3", "+1V8", "XIN", "XOUT", "VIN"]
N = {name: i for i, name in enumerate(NETS)}


def pad(num, x, y, net, w=0.6, h=0.6, layer="F.Cu"):
    return (f'    (pad "{num}" smd rect (at {x} {y}) (size {w} {h}) (layers "{layer}" "F.Mask")'
            f' (net {N[net]} "{net}"))')


def footprint(lib, ref, value, x, y, pads):
    return "\n".join([
        f'  (footprint "{lib}" (layer "F.Cu") (at {x} {y} 0)',
        f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))',
        f'    (property "Value" "{value}" (at 0 0 0) (layer "F.Fab"))',
        *pads,
        "  )",
    ])


def cap(ref, value, x, y, supply, pkg="0402"):
    metric = {"0402": "1005", "0805": "2012"}[pkg]
    half = {"0402": 0.5, "0805": 1.0}[pkg]
    return footprint(f"Capacitor_SMD:C_{pkg}_{metric}Metric", ref, value, x, y,
                     [pad("1", -half, 0, supply), pad("2", half, 0, "GND")])


def via(x, y, net):
    return f'  (via (at {x} {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net {N[net]}))'


def zone(net, layer, x0, y0, x1, y1):
    pts = f"(xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1})"
    return (f'  (zone (net {N[net]}) (net_name "{net}") (layer "{layer}") (hatch edge 0.5)'
            f' (min_thickness 0.25)\n    (polygon (pts {pts}))\n'
            f'    (filled_polygon (layer "{layer}") (pts {pts}))\n  )')


def build() -> str:
    parts = []
    # U1: supply pins on each side of a 10 mm body centred at (20, 20); ground beside each.
    u1 = []
    for i, (px, py) in enumerate([(-5, -2), (5, 2), (-2, 5), (2, -5)]):
        u1.append(pad(str(2 * i + 1), px, py, "+3V3"))
        # Ground one pin along the same side.
        gx, gy = (px, py + 1) if abs(px) == 5 else (px + 1, py)
        u1.append(pad(str(2 * i + 2), gx, gy, "GND"))
    u1 += [pad("9", -5, 4, "XIN"), pad("10", -5, 3, "XOUT")]
    parts.append(footprint("Package_QFP:LQFP-48", "U1", "MCU", 20, 20, u1))
    parts.append(footprint("Crystal:Crystal_SMD_3225", "Y1", "16MHz", 11, 25,
                           [pad("1", -1, 0, "XIN", 1.0, 1.0), pad("2", 1, 0, "XOUT", 1.0, 1.0)]))
    # The four 100 nF, each 1.5 mm off its pin, each with a supply and a ground via.
    for i, (cx, cy) in enumerate([(13.5, 18), (26.5, 22), (18, 26.5), (22, 13.5)], start=1):
        parts.append(cap(f"C{i}", "100nF", cx, cy, "+3V3"))
        parts.append(via(cx - 0.5, cy + 0.9, "+3V3"))
        parts.append(via(cx + 0.5, cy + 0.9, "GND"))
    parts.append(cap("C5", "10nF", 27, 17, "+3V3"))
    parts += [via(26.5, 17.9, "+3V3"), via(27.5, 17.9, "GND")]
    # The regulator and its bulk capacitor, 25 mm away.
    parts.append(footprint("Package_TO_SOT_SMD:SOT-223", "U3", "LDO 3V3", 45, 20,
                           [pad("1", -2, 0, "VIN"), pad("2", 0, 0, "+3V3"), pad("3", 2, 0, "GND")]))
    parts.append(cap("C6", "10uF", 45, 24, "+3V3", "0805"))
    parts += [via(44, 25.2, "+3V3"), via(46, 25.2, "GND")]
    # U2 on +1V8: one capacitor 7 mm away, and no ground via anywhere near it.
    parts.append(footprint("Package_SON:WSON-8", "U2", "Sensor", 20, 38,
                           [pad("1", -1, 0, "+1V8"), pad("2", 1, 0, "GND")]))
    parts.append(cap("C10", "100nF", 27.5, 38, "+1V8"))
    parts.append(f'  (segment (start 19 38) (end 27 38) (width 0.3) (layer "F.Cu") (net {N["+1V8"]}))')

    nets = "\n".join(f'  (net {i} "{n}")' for i, n in enumerate(NETS))
    return f"""(kicad_pcb
  (version 20241229)
  (generator "emi-analyzer-test-fixture")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" power)
    (2 "In2.Cu" power)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (stackup
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "prepreg") (thickness 0.2) (material "FR4")
             (epsilon_r 4.2) (loss_tangent 0.02))
      (layer "In1.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 2" (type "core") (thickness 1.065) (material "FR4")
             (epsilon_r 4.5) (loss_tangent 0.02))
      (layer "In2.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 3" (type "prepreg") (thickness 0.2) (material "FR4")
             (epsilon_r 4.2) (loss_tangent 0.02))
      (layer "B.Cu" (type "copper") (thickness 0.035))
    )
  )
{nets}

  (gr_rect (start 0 0) (end 60 45) (layer "Edge.Cuts") (width 0.1))

{chr(10).join(parts)}

{zone("GND", "In1.Cu", 0.5, 0.5, 59.5, 44.5)}
{zone("+3V3", "In2.Cu", 0.5, 0.5, 59.5, 32)}
)
"""


if __name__ == "__main__":
    OUT.write_text(build(), encoding="utf-8")
    print(f"wrote {OUT}")
