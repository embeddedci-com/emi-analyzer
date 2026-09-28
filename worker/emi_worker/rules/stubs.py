"""Test points at the end of a stub on a fast net.

A test point placed on the trace costs nothing. One reached by a branch off the route is an
open stub: the edge runs down it, reflects off the open end and comes back to the line late,
so a fast signal rings, and the stub is a small antenna driven by every edge. On a supply or
a slow control line it does not matter, so the check only looks at nets whose name says they
are fast (clocks, SPI, SDIO, USB, Ethernet and the like) and at differential pairs.

The stub is measured along the copper: for a test point T on a net whose other pins include
A and B, the branch off the A-B route is (d(A,T) + d(B,T) - d(A,B)) / 2, which is exact for
a tree and is taken over every pair of the other pins.
"""

from __future__ import annotations

import re
from itertools import combinations
from typing import Iterator

from .model import Finding, RuleContext, classify_net
from .planes import severity

RULE = "test-point-stub"

TP_REF = re.compile(r"^TP\d", re.I)
TP_HINT = re.compile(r"test_?point|testpad|test_?pad", re.I)
#: Names that say a net carries fast edges. Word-ish matching: "SCK" in "/SPI1_SCK", not in
#: "/BACKLIGHT".
FAST_NET = re.compile(
    r"(^|[/_.-])(clk\w*|\w*clk|sck|sclk|mclk|bclk|lrclk|mosi|miso|copi|cipo|sdo|sdi|spi\w*|"
    r"qspi\w*|ospi\w*|sdio\w*|sd_\w*|mmc\w*|emmc\w*|cmd|dat[0-7]|usb\w*|d[+-]|dp|dm|"
    r"eth\w*|rgmii\w*|rmii\w*|mdi\w*|tx_en|rx_dv|tx_clk|rx_clk|ref_clk|hdmi\w*|"
    r"lvds\w*|mipi\w*|csi\w*|dsi\w*|pcie\w*|sata\w*|ddr\w*|dq\w*|dqs\w*)($|[/_.+-])",
    re.I,
)


def _is_test_point(pads) -> bool:
    p = pads[0]
    return bool(TP_REF.match(p.ref) or TP_HINT.search(f"{p.footprint} {p.value}"))


def check_test_point_stubs(ctx: RuleContext) -> Iterator[Finding]:
    if not ctx.enabled(RULE) or not ctx.topology:
        return
    default_limit = ctx.wavelength_mm / 20.0
    pair_nets = {n for p in ctx.pairs for n in (p.positive, p.negative)}

    by_ref: dict[str, list] = {}
    for p in ctx.model.pads:
        if p.ref:
            by_ref.setdefault(p.ref, []).append(p)
    tps = {ref for ref, pads in by_ref.items() if _is_test_point(pads)}
    if not tps:
        return

    seen: set[tuple[str, str]] = set()
    for ref in sorted(tps):
        for tp in by_ref[ref]:
            net = tp.net
            if not net or (ref, net) in seen or classify_net(net) != "signal":
                continue
            seen.add((ref, net))
            # Per net, so a net group can mark a net fast (or slow) and give it its own budget.
            if (ctx.setting(RULE, "fast_nets_only", net=net) and net not in pair_nets
                    and not FAST_NET.search(net)):
                continue
            limit = float(ctx.setting(RULE, "max_stub_mm", net=net) or 0.0) or default_limit
            topo = ctx.topology.get(net)
            if topo is None:
                continue
            key = f"{tp.ref}.{tp.number}"
            if key not in topo.pads:
                continue
            others = [k for k in topo.pads if k.split(".", 1)[0] not in tps]
            if len(others) < 2:
                continue  # a pin and its test point: nothing for the probe to hang off
            stub = None
            for a, b in combinations(others, 2):
                pa, pb, ab = topo.path(a, key), topo.path(b, key), topo.path(a, b)
                if pa is None or pb is None or ab is None:
                    continue
                s = max(0.0, (pa.length_mm + pb.length_mm - ab.length_mm) / 2)
                stub = s if stub is None else min(stub, s)
            if stub is None or stub <= limit:
                continue
            vf = ctx.electrics.mean_velocity_factor if ctx.electrics else ctx.velocity_factor
            f_res = 299_792_458.0 * vf / (4 * stub / 1000.0)
            x, y = ctx.pt(tp.x, tp.y)
            yield Finding(
                rule=RULE,
                severity=severity(ctx, RULE, "warning"),
                title=f"Test point {ref} hangs {stub:.1f} mm off {net}",
                detail=(
                    f"{ref} is reached by a {stub:.1f} mm branch off the route of {net} (budget "
                    f"{limit:.1f} mm). The branch is an open stub: each edge reflects off its end "
                    f"and returns late, so the signal rings, and the stub radiates. It is a quarter "
                    f"wavelength at about {f_res / 1e6:.0f} MHz. Put the test point on the trace "
                    f"itself, or on a via along the route."
                ),
                action="Put the test point on the trace, not at the end of a branch.",
                net=net, layer=(tp.layers or [""])[0], x=x, y=y,
            )
