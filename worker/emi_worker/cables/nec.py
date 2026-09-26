"""The antenna solver: writing NEC decks and reading what comes back (§6).

nec2c is run as a subprocess, never linked, so its GPL stays its own — the same arrangement
openEMS and ngspice already have. That is also why PyNEC is not used despite being the obvious
binding: it links in-process.

The deck follows §6.2's model. The cable is a wire from the connector along its exit normal,
the board is the other arm, the feed is a voltage source on the junction segment, and the
observation points sit on the receiving antenna's scan rather than in the far field — at 30 MHz
and 3 m a receiving antenna is nowhere near the far field, so a gain pattern would be answering
a different question.

**NEC computes nothing until it reaches an execution card, and says nothing when it does not.**
M0 lost a run to that: a deck with FR and no XQ produced a structure echo, no results block, and
no error of any kind. The writer always emits XQ and the reader refuses an output with no
results rather than returning an empty list.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

NEC2C = "nec2c"

#: Segments per wavelength. NEC's own guidance is that a segment should be well under λ/10;
#: M0 measured impedance stable to about 3 % between 11 and 81 segments on a half-wave dipole,
#: so this is comfortable rather than marginal.
SEGMENTS_PER_WAVELENGTH = 20
MIN_SEGMENTS = 9
#: Enough for the longest cable the library accepts at the top of the grid: 10 m at 1.2 GHz is
#: 40 wavelengths, 800 segments at λ/20. The old cap of 201 cut a 3 m cable to λ/8 at 1.2 GHz
#: and a 30 m one to λ/1.7, where NEC's thin-wire currents mean nothing.
MAX_SEGMENTS = 801
#: The longest segment on any wire, whatever the frequency: λ/20 at 1.2 GHz.
#:
#: λ/20 alone let a segment grow as the frequency fell. At 30 MHz a 1 m cable was nine 111 mm
#: segments, fed on the first one and joined to a board arm of 11 mm segments, a 10:1 step
#: right at the source. Against openEMS on the same wire (docs/verification/
#: cables-and-drivers.md) that deck read e_per_amp up to 1.7 dB off below the first
#: resonance; with every segment at 12.5 mm it agreed to 0.7 dB, and a 6.25 mm deck moved
#: by less than 0.5 dB more, so this is converged. It costs nothing that matters: a 1 m cable
#: is 80 segments at every frequency.
MAX_SEGMENT_M = 0.0125

#: Height of the table top above the ground plane, in metres. The tabletop setups of ANSI C63.4
#: and CISPR 16-2-3 put the product and its cables 0.8 m up, and the full-wave far field uses
#: the same height (openems.nf2ff.TABLE_HEIGHT_M). This was 1.0 m, so board and cable paths
#: were modelled over two different grounds.
TABLE_HEIGHT_M = 0.8

SPEED_OF_LIGHT = 299_792_458.0


class NecError(RuntimeError):
    """nec2c could not be run, or produced an output with nothing in it."""


def available() -> bool:
    return shutil.which(NEC2C) is not None


def transmission_line_resonance_hz(length_m: float, height_m: float, far_end: str) -> float:
    """Where a wire over ground resonates, from transmission-line theory (cable test 2).

    Fed against the plane at one end, a wire is a transmission line: open at the far end it is
    series-resonant at a quarter wave, shorted at a half wave — the factor of two §6.2 refers
    to.

    The length that resonates is the **conductor path**, not the horizontal run. The feed drop
    adds the height once and a shorted far end adds it again, and at the heights a real bench
    uses that is several percent. Predicting from the horizontal run alone would call a correct
    solver wrong by more than the tolerance.
    """
    if length_m <= 0 or height_m < 0:
        raise ValueError("length must be positive and height non-negative")
    path = length_m + height_m + (height_m if far_end in ("ground", "equipment") else 0.0)
    divisor = 2.0 if far_end in ("ground", "equipment") else 4.0
    return SPEED_OF_LIGHT / (divisor * path)


def segments_for(length_m: float, frequency_hz: float) -> int:
    """An odd segment count, so a centre feed lands on a segment rather than between two."""
    lam = SPEED_OF_LIGHT / frequency_hz
    step = min(lam / SEGMENTS_PER_WAVELENGTH, MAX_SEGMENT_M)
    n = int(math.ceil(length_m / step - 1e-9))
    n = max(MIN_SEGMENTS, min(MAX_SEGMENTS, n))
    return n if n % 2 else n + 1


@dataclass
class ObservationRing:
    """Where the receiving antenna looks from (§6.2).

    The measurement distance is from the antenna to the **boundary of the product
    arrangement**, the smallest circle around it (CISPR 16-2-3; ANSI C63.4 measures from the
    periphery the same way). The turntable spins that circle, so the antenna sweeps a ring
    centred on it, ``distance_m`` outside it, at the heights the mast covers. The maximum over
    the ring is what a measurement reports.

    The ring was once centred on the board/cable junction with a 3 m radius, whatever the
    cable's length. A 2 m cable along +x then ended 1 m from the antenna, and a 3 m one ran
    straight through it: the "field at 3 m" was the field on the wire.
    """

    distance_m: float = 3.0
    heights_m: tuple[float, ...] = (1.0, 2.0, 3.0, 4.0)
    azimuth_steps: int = 12

    def points(self, x_min: float = 0.0, x_max: float = 0.0) -> list[tuple[float, float, float]]:
        """The scan around an arrangement lying along the x axis from x_min to x_max."""
        centre = (x_min + x_max) / 2.0
        radius = self.distance_m + (x_max - x_min) / 2.0
        out = []
        for h in self.heights_m:
            for k in range(self.azimuth_steps):
                a = 2 * math.pi * k / self.azimuth_steps
                out.append((centre + radius * math.cos(a), radius * math.sin(a), h))
        return out


@dataclass
class Deck:
    """One cable, as NEC sees it."""

    length_m: float
    frequency_hz: float
    #: Height of the cable above the ground plane, in metres.
    height_m: float = TABLE_HEIGHT_M
    #: The board, as the other arm of the antenna: its length along the cable's exit.
    board_span_m: float = 0.1
    #: The board's width across the exit. None models the board as one thin wire
    #: ``board_span_m`` long (Tier A); a width models it as a plate, a wire grid of the board's
    #: bounding box (Tier B).
    #:
    #: The thin wire has far too little capacitance for a board. Against a plate of the same
    #: outline it reads the antenna's impedance 5-12 dB high below the first resonance, so
    #: Tier B's current came out that much low; that was most of what cable test 4 measured
    #: (docs/verification/cables-and-drivers.md §4).
    board_width_m: float | None = None
    #: Where the cable leaves, measured across the board from one edge. None is the middle.
    board_offset_m: float | None = None
    radius_m: float = 0.0005
    far_end: str = "open"
    #: What the source pushes against.
    #:
    #:   "board"   the product geometry (§6.2): fed between the cable and the board, which is
    #:             the other arm of the antenna. This is a dipole, and its resonance depends
    #:             on the board as well as the cable.
    #:   "ground"  the validation fixture for cable test 2: fed between the cable and the
    #:             ground plane, which makes the structure a transmission line with a closed
    #:             form to check against — c/(4L) open, c/(2L) shorted.
    #:
    #: They are different structures and resonate at different frequencies. Conflating them
    #: is how a solver gets validated against a prediction for something else.
    feed: str = "board"
    #: Common-mode choke impedance at this frequency, R + jX ohms, as a series load on the
    #: first segment of the cable. ``library.Choke.z_at`` gives it from a datasheet curve.
    choke_z: complex | None = None
    #: A bond: the board strapped to the ground plane at the connector, through this
    #: inductance. ``None`` leaves the board floating; 0 is a strap with no added inductance.
    #:
    #: It is a wire, not a load. It was once a load on segment 1 of the board arm, which is the
    #: arm's open far end: a series element in a wire carrying no current, which moved nothing
    #: and so passed any test of which way a bond moves the resonance.
    bond_nh: float | None = None
    #: Whether a perfect ground plane sits at z = 0.
    #:
    #: True for every product and compliance use: a radiated scan is defined over a ground
    #: plane, and the image it puts under the cable changes both the resonance and the field.
    #: False exists for cable test 4, where the same structure has to be built in an FDTD
    #: domain that has absorbing boundaries on all six sides and therefore no image. Comparing
    #: a NEC cable over ground against an FDTD cable in free space measures the difference
    #: between two antennas, not the composition the test is for.
    ground: bool = True
    ring: ObservationRing = field(default_factory=ObservationRing)

    def to_text(self) -> str:
        segments = segments_for(self.length_m, self.frequency_hz)
        # The board arm is segmented like the cable. It was a third as fine, 33 mm segments on a
        # 0.1 m board, which is coarser than λ/10 above 900 MHz right at the feed.
        board_segments = segments_for(self.board_span_m, self.frequency_hz)
        lines = [
            f"CM cable {self.length_m:g} m at {self.frequency_hz / 1e6:g} MHz",
            f"CM far end {self.far_end}",
            "CE",
            # Tag 1: the cable, running along +x from the board edge at the far end.
            f"GW 1 {segments} 0 0 {self.height_m:.6f} "
            f"{self.length_m:.6f} 0 {self.height_m:.6f} {self.radius_m:.6f}",
        ]
        if self.feed == "board" and self.board_width_m:
            # Tag 2 and up: the board as a plate, ending at the feed.
            lines += _plate_cards(self.board_span_m, self.board_width_m,
                                  self.board_offset_m, self.height_m, first_tag=10)
        elif self.feed == "board":
            # Tag 2: the other arm, the board as one wire.
            lines.append(
                f"GW 2 {board_segments} {-self.board_span_m:.6f} 0 {self.height_m:.6f} "
                f"0 0 {self.height_m:.6f} {self.radius_m:.6f}")
        else:
            # Tag 2: a drop to the ground plane, for the transmission-line fixture.
            lines.append(
                f"GW 2 {max(3, segments_for(self.height_m, self.frequency_hz) | 1)} "
                f"0 0 {self.height_m:.6f} 0 0 0 {self.radius_m:.6f}")

        # The far end, which §6.2 says decides the resonance — and it only does if the model
        # actually connects it. A small series load on the end segment does nothing at all: the
        # current there is already near zero, so a 1 mOhm resistor in series with nothing is
        # still nothing. Grounding means the wire reaching the ground plane, which is a
        # geometry change, not a load.
        #
        #   open       the wire ends in air; a dipole against the board arm
        #   ground     a drop wire to z = 0, which GE 1 treats as bonded to the plane
        #   equipment  the same drop, carrying CISPR 16-1-2's 150 ohm common-mode impedance
        drop_segments = max(3, segments_for(self.height_m, self.frequency_hz) | 1)
        if self.far_end in ("ground", "equipment") and self.ground:
            lines.append(
                f"GW 3 {drop_segments} {self.length_m:.6f} 0 {self.height_m:.6f} "
                f"{self.length_m:.6f} 0 0 {self.radius_m:.6f}")

        # The bond: a strap from the junction, the board side of the feed, down to the plane.
        # The feed is segment 1 of the cable, so the source then drives the cable against a
        # grounded board. Its own length is the table height, and at 0.8 m that wire is about
        # a microhenry by itself: a bond inductance well under that changes little, which is
        # what a bond to a floor plane can do and no more.
        if self.bond_nh is not None and self.ground and self.feed == "board":
            lines.append(
                f"GW 4 {drop_segments} 0 0 {self.height_m:.6f} 0 0 0 {self.radius_m:.6f}")

        # GE 1: a ground plane is present, which every measurement standard specifies. It has
        # to come after every GW, which is why the far end is decided above rather than below.
        # GE 0 is free space, and then there is no GN card to write and nothing for a drop
        # wire or a 150 ohm termination to reach.
        lines.append("GE 1" if self.ground else "GE 0")
        if self.ground:
            lines.append("GN 1")

        if self.far_end == "equipment" and self.ground:
            # The 150 ohm sits in the drop, between the cable and the plane.
            lines.append("LD 4 3 1 1 150.0 0.0")

        if self.choke_z:
            # A choke is a series impedance at the board end. With a non-negative real part it
            # can only ever reduce the current, which is what cable test 3 checks.
            z = complex(self.choke_z)
            lines.append(f"LD 4 1 1 1 {z.real:.6f} {z.imag:.6f}")
        if self.bond_nh and self.ground and self.feed == "board":
            # In the strap's top segment, at the board: where a bond's inductance is.
            reactance = 2 * math.pi * self.frequency_hz * self.bond_nh * 1e-9
            lines.append(f"LD 4 4 1 1 0.0 {reactance:.6f}")

        # The feed sits on the junction between the two arms: segment 1 of the cable.
        lines.append("EX 0 1 1 0 1.0 0.0")
        lines.append(f"FR 0 1 0 0 {self.frequency_hz / 1e6:.6f} 0")

        # Near field, not a pattern. NE 0 selects E-field in rectangular coordinates; each
        # card names one point, which keeps the reader simple and the deck explicit.
        x_min = -self.board_span_m if self.feed == "board" else 0.0
        for x, y, z in self.ring.points(x_min, self.length_m):
            lines.append(f"NE 0 1 1 1 {x:.6f} {y:.6f} {z:.6f} 0 0 0")

        # Without this NEC echoes the structure and computes nothing, with no error at all.
        lines.append("XQ 0")
        lines.append("EN")
        return "\n".join(lines) + "\n"


#: About how many wires a board plate uses. nec2c's cost grows with the cube of the segment
#: count; 700 is about 0.15 s a frequency. Past about 250 x 250 mm the pitch cap wins and it
#: grows.
PLATE_MAX_WIRES = 700
#: The finest plate pitch. Against a 2.5 mm grid, a 5 mm grid reads |Z| 0.3 dB high and a
#: 12.5 mm one about 1 dB high, the same at every frequency.
PLATE_MIN_PITCH_M = 0.0025


def plate_pitch(span_m: float, width_m: float) -> float:
    """The grid pitch for a board plate: as fine as PLATE_MAX_WIRES allows, never above λ/20
    at 1.2 GHz (MAX_SEGMENT_M), which is well under the λ/10 a wire grid needs."""
    s = math.sqrt(2.0 * span_m * width_m / PLATE_MAX_WIRES)
    return min(MAX_SEGMENT_M, max(PLATE_MIN_PITCH_M, s))


def _lattice(lo: float, hi: float, pitch: float) -> list[float]:
    """Evenly spaced points from lo to hi, with 0 among them whenever it lies inside, so the
    cable can join the grid there. NEC connects wires only where their ends coincide."""
    if lo < 0.0 < hi:
        return _lattice(lo, 0.0, pitch)[:-1] + _lattice(0.0, hi, pitch)
    n = max(1, math.ceil((hi - lo) / pitch - 1e-9))
    return [lo + (hi - lo) * i / n for i in range(n + 1)]


def _plate_cards(span_m: float, width_m: float, offset_m: float | None, z_m: float,
                 first_tag: int) -> list[str]:
    """A board as a wire grid (§6.2): its bounding box at the table height, x from -span to
    the edge the cable leaves from, the cable joining at x = 0, y = 0.

    Standard NEC practice for a conducting surface: a pitch well under λ/10, and the
    equal-area radius, pitch / 2π, so each direction's wires have the surface of the plate.
    One segment per wire.
    """
    pitch = plate_pitch(span_m, width_m)
    radius = pitch / (2 * math.pi)
    offset = width_m / 2.0 if offset_m is None else min(max(offset_m, 0.0), width_m)
    xs = _lattice(-span_m, 0.0, pitch)
    ys = _lattice(-offset, width_m - offset, pitch)
    cards = []
    tag = first_tag
    for y in ys:
        for x0, x1 in zip(xs, xs[1:]):
            cards.append(f"GW {tag} 1 {x0:.6f} {y:.6f} {z_m:.6f} {x1:.6f} {y:.6f} {z_m:.6f} "
                         f"{radius:.6f}")
            tag += 1
    for x in xs:
        for y0, y1 in zip(ys, ys[1:]):
            cards.append(f"GW {tag} 1 {x:.6f} {y0:.6f} {z_m:.6f} {x:.6f} {y1:.6f} {z_m:.6f} "
                         f"{radius:.6f}")
            tag += 1
    return cards


#: A number as nec2c writes them: plain decimals in the location columns, exponential in the
#: field ones, and negative phases. Written once rather than inline, because getting it subtly
#: wrong is how a parser silently matches nothing.
_NUM = r"[-+]?\d+\.?\d*(?:[Ee][-+]?\d+)?"

#: The input-parameters row is TAG, SEG, then V real/imag, I real/imag, Z real/imag, and more.
#: The impedance is the *seventh and eighth* numbers. An earlier version of this counted to the
#: fifth and sixth, read the current as an impedance, and produced a plausible-looking zero.
_IMPEDANCE = re.compile(
    rf"^\s*\d+\s+\d+\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+({_NUM})\s+({_NUM})", re.M)

#: A near-field row: X Y Z, then magnitude and phase for each of EX, EY, EZ. Magnitudes and
#: phases, not real and imaginary parts.
_E_FIELD = re.compile(
    rf"^\s*({_NUM})\s+({_NUM})\s+({_NUM})\s+"
    rf"({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})\s*$", re.M)

NEAR_FIELD_HEADER = "NEAR ELECTRIC FIELDS"


@dataclass
class NecResult:
    frequency_hz: float
    #: Input impedance at the feed, ohms.
    z_in: complex
    #: The strongest reading a receiving antenna gets at each observation point, V/m, for
    #: the 1 V source the deck excites with. "Strongest" means the better of vertical and
    #: horizontal polarisation, which is what a scan reports (§6.2) — not the total field,
    #: which no single antenna can read at once.
    e_v_per_m: list[float]

    def peak_e(self) -> float:
        return max(self.e_v_per_m) if self.e_v_per_m else 0.0

    def e_per_amp(self) -> float:
        """|E| per amp of feed current — the quantity a budget actually needs.

        The deck is excited with 1 V, so the current it draws depends on the impedance.
        Dividing by that current makes the answer independent of the excitation, which is
        what lets a measured or predicted common-mode current be applied to it later.
        """
        i = 1.0 / self.z_in if self.z_in != 0 else 0.0
        return self.peak_e() / abs(i) if i != 0 else 0.0


def parse_output(text: str, frequency_hz: float) -> NecResult:
    """Read impedance and near field out of a nec2c report."""
    if "ANTENNA INPUT PARAMETERS" not in text:
        raise NecError(
            "nec2c produced no results block. NEC computes only when it reaches an execution "
            "card, and reports nothing at all when it does not — check the deck has an XQ"
        )
    z = _IMPEDANCE.search(text.split("ANTENNA INPUT PARAMETERS", 1)[1])
    if not z:
        raise NecError("nec2c reported no input impedance")
    z_in = complex(float(z.group(1)), float(z.group(2)))

    # Each NE card produces its own block, so every block has to be read rather than only the
    # first. A parser that split once and stopped would report one point out of forty-eight
    # and call it the peak.
    fields: list[float] = []
    for block in text.split(NEAR_FIELD_HEADER)[1:]:
        m = _E_FIELD.search(block)
        if not m:
            continue
        ex, ey, ez = float(m.group(4)), float(m.group(6)), float(m.group(8))
        # Vertical or horizontal, whichever reads higher. A measurement sweeps both and keeps
        # the larger; the vector total would be a number no single antenna can see.
        fields.append(max(abs(ez), math.hypot(ex, ey)))

    if not fields:
        raise NecError(
            "nec2c reported no near-field points. The deck asked for them, so this means the "
            "NE cards were not reached — check the execution card"
        )
    return NecResult(frequency_hz=frequency_hz, z_in=z_in, e_v_per_m=fields)


#: How many nec2c processes run at once. Each deck is one small solve and most of its cost is
#: starting the process, so a few in parallel hide that; the 12.5 mm segment cap made the
#: resonance rule's 80 runs take 2 s on a CI runner, which every upload pays.
PARALLEL_RUNS = min(4, os.cpu_count() or 1)


def run_many(decks: list[Deck], *, timeout_s: float = 60.0) -> list[NecResult]:
    """Run several decks, a few at a time. Results come back in the order of ``decks``."""
    if len(decks) <= 1 or PARALLEL_RUNS <= 1:
        return [run(d, timeout_s=timeout_s) for d in decks]
    from concurrent.futures import ThreadPoolExecutor

    # Threads are enough: each one only waits on its own nec2c process.
    with ThreadPoolExecutor(max_workers=PARALLEL_RUNS) as pool:
        return list(pool.map(lambda d: run(d, timeout_s=timeout_s), decks))


def run(deck: Deck, *, timeout_s: float = 60.0) -> NecResult:
    """Run one deck through nec2c."""
    if not available():
        raise NecError(
            f"{NEC2C} is not installed on this worker, so the antenna solver cannot run. "
            f"The worker advertises the cable run kind only when it resolves"
        )
    with tempfile.TemporaryDirectory(prefix="emi-nec-") as tmp:
        in_path = Path(tmp) / "deck.nec"
        out_path = Path(tmp) / "deck.out"
        in_path.write_text(deck.to_text())
        proc = subprocess.run(
            [NEC2C, f"-i{in_path}", f"-o{out_path}"],
            capture_output=True, text=True, timeout=timeout_s,
        )
        if proc.returncode != 0:
            raise NecError(
                f"{NEC2C} exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:400]}")
        if not out_path.exists():
            raise NecError(f"{NEC2C} wrote no output file")
        return parse_output(out_path.read_text(), deck.frequency_hz)
