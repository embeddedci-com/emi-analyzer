"""Uploaded vendor models: vet them, check they behave like a clamp, and wire them to the board.

A file becomes a model the simulation uses only when all of these pass, and every result is
reported with the simulation so a rejected upload says why:

  1. structure -- spice_model.validate: an allowlist of statements, nothing that runs commands
     or reads files;
  2. the chosen subcircuit exists, and every one of its pins is mapped to a pad of that part on
     this board;
  3. it clamps: a level-1 discharge into a line pin, both polarities, converges and the pin
     stays below a sanity bound -- a "model" that lets the pulse through is not a clamp;
  4. when the part is in the datasheet table, its clamping voltage at the datasheet's rated
     current is within tolerance of the datasheet's.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import ngspice, parts, spice_model, waveforms
from .parts import ClampSpec

#: A clamp that lets a 7.5 A discharge reach this voltage is not clamping.
SANITY_LIMIT_V = 200.0
#: How far a vendor model's clamping voltage may sit from the datasheet's.
DATASHEET_TOLERANCE = 0.25
MAX_MODELS = 20


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class VendorModel:
    part: str
    filename: str
    subckt: str = ""
    #: pad number -> subcircuit pin
    pad_to_pin: dict[str, str] = field(default_factory=dict)
    text: str = ""
    pins: list[str] = field(default_factory=list)
    accepted: bool = False
    reasons: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "part": self.part, "file": self.filename, "subckt": self.subckt,
            "status": "accepted" if self.accepted else "rejected",
            "reasons": self.reasons, "checks": [c.as_dict() for c in self.checks],
        }


def roles_for(pins: list[str], pad_to_pin: dict[str, str], pads: list, line_pad, through_pad=None,
              kind=None) -> list[str]:
    """Role of each subcircuit pin for one placed part on the board."""
    pin_to_pad = {pin.lower(): number for number, pin in pad_to_pin.items()}
    by_number = {p.number: p for p in pads}
    roles = []
    for pin in pins:
        pad = by_number.get(pin_to_pad.get(pin.lower(), ""))
        if pad is None:
            roles.append("nc")
        elif line_pad is not None and pad.number == line_pad.number:
            roles.append("io")
        elif through_pad is not None and pad.number == through_pad.number:
            roles.append("through")
        elif pad.net and kind and kind(pad.net) == "ground":
            roles.append("gnd")
        elif pad.net and kind and kind(pad.net) == "power":
            roles.append("rail")
        else:
            roles.append("nc")
    return roles


def _sanity_netlist(text: str, subckt: str, roles: list[str]) -> str:
    samples = waveforms.esd_samples(waveforms.CONTACT_LEVELS[1].kv, end_s=20e-9)
    lines = ["* vendor model sanity check", text]
    saves = []
    for tag, polarity in (("p", 1), ("n", -1)):
        nodes = []
        for i, role in enumerate(roles):
            if role == "io":
                nodes.append(f"{tag}io")
            elif role == "gnd":
                nodes.append("0")
            elif role == "rail":
                nodes.append(f"{tag}rail")
            else:
                nodes.append(f"{tag}nc{i}")
                lines.append(f"R{tag}nc{i} {tag}nc{i} 0 1e6")
        lines.append(f"I{tag} 0 {tag}io PWL(" + " ".join(f"{t:.6g} {polarity * a:.6g}" for t, a in samples) + ")")
        lines.append(f"R{tag}io {tag}io 0 1e9")
        lines.append(f"X{tag} {' '.join(nodes)} {subckt}")
        if "rail" in roles:
            lines += [f"V{tag}r {tag}rv 0 3.3", f"L{tag}r {tag}rv {tag}rail 5n",
                      f"C{tag}r {tag}rail 0 100n", f"R{tag}rr {tag}rail 0 1e9"]
        saves.append(f"v({tag}io)")
    lines += [".options interp", ".tran 10p 20n 0 5p", ".save " + " ".join(saves), ".end"]
    return "\n".join(lines) + "\n"


def _quasi_static_netlist(text: str, subckt: str, roles: list[str], amps: float) -> str:
    nodes = []
    lines = ["* clamping voltage at the datasheet's rated current", text]
    for i, role in enumerate(roles):
        if role == "io":
            nodes.append("io")
        elif role == "gnd":
            nodes.append("0")
        elif role == "rail":
            nodes.append("rail")
        else:
            nodes.append(f"nc{i}")
            lines.append(f"Rnc{i} nc{i} 0 1e6")
    # A slow ramp to the rated current: at 8 µs nothing capacitive is left, so the pin voltage at
    # the peak is the static clamping voltage the datasheet's table reports.
    lines += [f"I1 0 io PWL(0 0 8u {amps:.6g} 28u 0)", "Rio io 0 1e9",
              f"X1 {' '.join(nodes)} {subckt}"]
    if "rail" in roles:
        lines += ["Vr rv 0 3.3", "Lr rv rail 5n", "Cr rail 0 100n", "Rrr rail 0 1e9"]
    lines += [".options interp", ".tran 20n 10u 0 20n", ".save v(io)", ".end"]
    return "\n".join(lines) + "\n"


def load(entries: list[dict], files: dict[str, bytes], board_parts: dict[str, list[tuple[str, list]]],
         kind) -> dict[str, VendorModel]:
    """Vet every uploaded model. Returns {normalised part value: model}, accepted or not.

    ``board_parts`` maps a normalised part value to [(ref, pads), ...] on this board.
    """
    out: dict[str, VendorModel] = {}
    for entry in entries[:MAX_MODELS]:
        part = str(entry.get("part", "")).strip()
        vm = VendorModel(part=part, filename=str(entry.get("filename", "") or entry.get("key", "")))
        key = parts._norm(part)
        out[key] = vm

        data = files.get(str(entry.get("key", "")))
        if data is None:
            vm.reasons.append("the uploaded file could not be downloaded")
            continue
        try:
            model = spice_model.validate(data, vm.filename)
        except spice_model.ModelRejected as exc:
            vm.reasons.extend(exc.reasons)
            vm.checks.append(Check("is a SPICE model", False, exc.reasons[0]))
            continue
        vm.checks.append(Check(
            "is a SPICE model", True,
            f"{len(model.subckts)} subcircuit{'s' if len(model.subckts) != 1 else ''}, "
            f"{len(model.models)} device model{'s' if len(model.models) != 1 else ''}; nothing that runs commands or reads files",
        ))

        wanted = str(entry.get("subckt", "")).strip()
        found = model.subckt(wanted) if wanted else (next(iter(model.subckts.items())) if len(model.subckts) == 1 else None)
        if found is None:
            reason = (f"the file has no subcircuit named {wanted}" if wanted else
                      f"the file defines {len(model.subckts)} subcircuits; choose which one is {part}")
            vm.reasons.append(reason)
            vm.checks.append(Check("subcircuit", False, reason))
            continue
        vm.subckt, vm.pins = found
        vm.text = model.text

        placed = board_parts.get(key, [])
        if not placed:
            reason = f"no part with value {part} is on this board"
            vm.reasons.append(reason)
            vm.checks.append(Check("pins", False, reason))
            continue
        mapping = {str(k): str(v) for k, v in (entry.get("pins") or {}).items()}
        if not mapping and len(vm.pins) == len(placed[0][1]):
            numbers = sorted({p.number for p in placed[0][1]}, key=lambda n: (len(n), n))
            mapping = dict(zip(numbers, vm.pins))
        assigned = [pin.lower() for pin in mapping.values()]
        missing = [pin for pin in vm.pins if pin.lower() not in assigned]
        doubled = sorted({pin for pin in assigned if assigned.count(pin) > 1})
        unknown_pads = sorted(n for n in mapping if n not in {p.number for p in placed[0][1]})
        unknown_pins = sorted(pin for pin in mapping.values() if pin.lower() not in [p.lower() for p in vm.pins])
        problems = []
        if missing:
            problems.append(f"subcircuit pins not mapped to a pad: {', '.join(missing)}")
        if doubled:
            problems.append(f"mapped to more than one pad: {', '.join(doubled)}")
        if unknown_pads:
            problems.append(f"pads {', '.join(unknown_pads)} are not on {placed[0][0]}")
        if unknown_pins:
            problems.append(f"{', '.join(unknown_pins)} are not pins of {vm.subckt}")
        if problems:
            vm.reasons.extend(problems)
            vm.checks.append(Check("pins", False, "; ".join(problems)))
            continue
        vm.pad_to_pin = mapping
        vm.checks.append(Check("pins", True, f"{len(vm.pins)} pins mapped to pads of {placed[0][0]}"))

        ref, pads = placed[0]
        line_pad = next((p for p in pads if p.net and kind(p.net) == "signal"), None)
        roles = roles_for(vm.pins, mapping, pads, line_pad, kind=kind)
        if "io" not in roles or "gnd" not in roles:
            reason = (f"on {ref} no pin of {vm.subckt} is wired to a signal line and one to ground, "
                      f"so it cannot be tested as a clamp")
            vm.reasons.append(reason)
            vm.checks.append(Check("clamps a discharge", False, reason))
            continue

        try:
            result = ngspice.run(_sanity_netlist(vm.text, vm.subckt, roles), timeout_s=60, expect_end_s=20e-9)
            peaks = {tag: float(max(abs(result[f"v({tag}io)"]))) for tag in ("p", "n")}
        except ngspice.SimulationError as exc:
            reason = f"ngspice could not simulate it: {exc}"
            vm.reasons.append(reason)
            vm.checks.append(Check("clamps a discharge", False, reason))
            continue
        worst = max(peaks.values())
        ok = worst < SANITY_LIMIT_V
        vm.checks.append(Check(
            "clamps a discharge", ok,
            f"a 2 kV contact discharge (7.5 A) peaks at {peaks['p']:.1f} V positive and "
            f"{peaks['n']:.1f} V negative on {ref} pin {line_pad.number}"
            + ("" if ok else f" — above {SANITY_LIMIT_V:g} V, so it is not clamping"),
        ))
        if not ok:
            vm.reasons.append(f"it does not clamp: the line pin reaches {worst:.0f} V")
            continue

        spec: ClampSpec | None = parts.lookup(part)
        if spec and spec.v_c_points:
            amps, volts, conditions = max(spec.v_c_points)
            try:
                qs = ngspice.run(_quasi_static_netlist(vm.text, vm.subckt, roles, amps), timeout_s=60, expect_end_s=10e-6)
                got = float(max(abs(qs["v(io)"])))
                off = abs(got - volts) / volts
                ok = off <= DATASHEET_TOLERANCE
                vm.checks.append(Check(
                    "matches the datasheet", ok,
                    f"{got:.1f} V at {amps:g} A against the datasheet's {volts:g} V ({conditions or '8/20 µs'})"
                    + ("" if ok else f", {off:.0%} off"),
                ))
                if not ok:
                    vm.reasons.append(
                        f"its clamping voltage at {amps:g} A is {got:.1f} V, but the datasheet says {volts:g} V"
                    )
                    continue
            except ngspice.SimulationError as exc:
                vm.reasons.append(f"ngspice could not check it against the datasheet: {exc}")
                vm.checks.append(Check("matches the datasheet", False, str(exc)))
                continue
        vm.accepted = True
    return out
