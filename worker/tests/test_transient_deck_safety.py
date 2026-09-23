"""Board text must never become a netlist line.

The deck's title carries a net name and a reference designator straight from the board, and
the s-expression reader keeps a newline inside a quoted string. A net named
``"X\\n.control\\nshell ..."`` once put a ``.control`` block -- which ngspice runs, shell
commands included -- into the deck. These need no ngspice: they check the text that would be
handed to it.
"""

from __future__ import annotations

import pytest

from emi_worker.kicad import sexpr
from emi_worker.transient import netlist, ngspice

HOSTILE = '(kicad_pcb (net 1 "USB_D\n.control\nshell id > /tmp/pwned\n.endc\n*"))'


def _hostile_net_name() -> str:
    return sexpr.parse(HOSTILE)[1][2]


def test_a_net_name_with_newlines_stays_on_the_title_line():
    name = _hostile_net_name()
    assert "\n" in name  # the reader keeps it, which is why the deck must not
    deck = netlist.build([], [], [(0.0, 0.0), (1e-9, 1.0)], 1e-9, 1e-11, 1e-12,
                         title=f"{name} at J1")
    first, *rest = deck.split("\n")
    assert first.startswith("* USB_D?.control?shell")
    assert not any(line.lstrip().lower().startswith((".control", "shell", ".endc")) for line in rest)
    ngspice.check_deck(deck)  # and the last check lets our own deck through


def test_the_title_is_printable_ascii_and_bounded():
    title = netlist.safe_title("NET\r\x00\x1b[31m" + "é" * 500)
    assert all(0x20 <= ord(ch) <= 0x7e for ch in title)
    assert len(title) <= netlist.MAX_TITLE


@pytest.mark.parametrize("directive", [".control", ".CONTROL", "  .include /etc/passwd", ".lib x.lib", ".endc"])
def test_a_deck_with_a_forbidden_directive_is_refused_before_ngspice(directive):
    deck = f"* title\nR1 a 0 1k\n{directive}\n.end\n"
    with pytest.raises(ngspice.SimulationError, match="refused"):
        ngspice.run(deck)


def test_a_forbidden_word_on_the_title_line_is_only_a_title():
    ngspice.check_deck("* .control is just text here\nR1 a 0 1k\n.end\n")
