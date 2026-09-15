"""Vendor SPICE models: decide whether an uploaded file is something we will hand to ngspice.

A SPICE "model file" is a netlist fragment, and a netlist is closer to a script than to data.
ngspice will happily run a ``.control`` block -- which can call ``shell`` -- read other files
through ``.include`` and ``.lib``, load code models, and change options for the whole
simulation. An upload is somebody else's file running on our worker, so nothing from it reaches
ngspice unless it passes this module.

The approach is an allowlist, not a blocklist. A model is subcircuits and device models, so a
file is accepted when every statement in it is one of those things:

  * dot-statements: ``.subckt``, ``.ends``, ``.model``, ``.param``, ``.func`` (``.end`` is
    dropped); anything else is refused with the reason it is dangerous or out of place;
  * element lines, only inside a subcircuit, and only for passive, semiconductor, controlled
    source, transmission-line and switch devices -- no XSPICE ``A`` devices (code models) and
    no ``N`` devices (OSDI/Verilog-A);
  * no ``file=`` arguments anywhere;
  * every subcircuit instance and every diode model it references must be defined in the file.

Passing this says the file is *structurally* a model. Whether it behaves like a clamp is a
separate, electrical check the stage runs next (see ``models.validate_electrically``); both
results are reported with the simulation, so a rejected upload says why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_MODEL_BYTES = 2 * 1024 * 1024

ALLOWED_DIRECTIVES = {".subckt", ".ends", ".model", ".param", ".func"}
DROPPED_DIRECTIVES = {".end"}

#: Why each refused directive is refused, so the user reads a reason rather than a rule.
_REFUSED = {
    ".control": "runs ngspice commands, including shell commands",
    ".endc": "ends a .control block, which is not allowed",
    ".include": "reads another file from disk",
    ".inc": "reads another file from disk",
    ".lib": "reads another file (or a section of one) from disk",
    ".endl": "belongs to a .lib section, which is not allowed",
    ".options": "changes simulator options for the whole simulation",
    ".option": "changes simulator options for the whole simulation",
    ".opt": "changes simulator options for the whole simulation",
    ".global": "changes the meaning of node names outside the model",
    ".osdi": "loads compiled device code",
    ".temp": "changes the simulation temperature",
}
_ANALYSIS = {
    ".tran", ".ac", ".dc", ".op", ".noise", ".tf", ".pz", ".sens", ".disto", ".four",
    ".meas", ".measure", ".save", ".print", ".plot", ".probe", ".width", ".nodeset", ".ic",
    ".csparam", ".if", ".else", ".elseif", ".endif", ".step", ".data", ".enddata",
}

#: Element letters a device model can legitimately contain.
ALLOWED_ELEMENTS = set("RCLKDQMJVIEFGHBXTSW")
_ELEMENT_NAMES = {
    "A": "an XSPICE code-model device", "N": "an OSDI/Verilog-A device",
    "P": "a coupled multiconductor line", "Y": "a lossy transmission line",
    "U": "a uniform RC line", "Z": "a MESFET", "O": "a lossy transmission line",
}

_FILE_ARG = re.compile(r"\b(file|filename|infile|outfile)\s*=", re.I)
#: Names the simulation netlist uses for its own models and subcircuits.
_RESERVED = re.compile(r"^emi_", re.I)


class ModelRejected(Exception):
    """The upload is not a model we will run. ``reasons`` are written for the user."""

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass
class SpiceModel:
    filename: str
    #: The accepted statements only, one logical line each, safe to place in a netlist.
    text: str
    #: Subcircuit name (as written) -> its pins, in order.
    subckts: dict[str, list[str]] = field(default_factory=dict)
    models: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    def subckt(self, name: str) -> tuple[str, list[str]] | None:
        for k, pins in self.subckts.items():
            if k.lower() == name.lower():
                return k, pins
        return None


def _decode(data: bytes) -> str:
    if len(data) > MAX_MODEL_BYTES:
        raise ModelRejected([
            f"the file is {len(data) / 1e6:.1f} MB; a device model is kilobytes, and the limit "
            f"is {MAX_MODEL_BYTES / 1e6:.0f} MB"
        ])
    if b"\x00" in data:
        raise ModelRejected(["the file is binary, not a SPICE text netlist"])
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    control = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\r\n\f")
    if text and control / len(text) > 0.01:
        raise ModelRejected(["the file is mostly control characters, not a SPICE text netlist"])
    return text


def _strip_comment(line: str) -> str:
    # ';' (PSpice) and '$' (HSPICE/ngspice) start a comment; '$' only after whitespace, since
    # it can legitimately appear inside a name in some libraries.
    line = line.split(";", 1)[0]
    m = re.search(r"\s\$", line)
    if m:
        line = line[: m.start()]
    return line.rstrip()


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Physical lines joined across '+' continuations, comments removed, with line numbers."""
    out: list[tuple[int, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("*"):
            continue
        body = _strip_comment(stripped)
        if not body:
            continue
        if body.startswith("+"):
            if not out:
                raise ModelRejected([f"line {number}: continuation with nothing to continue"])
            n, prev = out[-1]
            out[-1] = (n, prev + " " + body[1:].strip())
        else:
            out.append((number, body))
    return out


def _subckt_pins(tokens: list[str]) -> list[str]:
    pins = []
    for tok in tokens:
        if tok.lower() in ("params:", "param:") or "=" in tok:
            break
        pins.append(tok)
    return pins


def _instance_target(tokens: list[str]) -> str:
    """The subcircuit an X line instantiates: the last token before any parameters."""
    body = []
    for tok in tokens[1:]:
        if tok.lower() in ("params:", "param:") or "=" in tok:
            break
        body.append(tok)
    return body[-1] if body else ""


def validate(data: bytes, filename: str) -> SpiceModel:
    """Parse and vet an uploaded model. Raises ModelRejected with every problem found."""
    text = _decode(data)
    lines = _logical_lines(text)
    reasons: list[str] = []
    kept: list[str] = []
    model = SpiceModel(filename=filename, text="")
    stack: list[str] = []
    instances: list[tuple[int, str]] = []
    diode_models: list[tuple[int, str]] = []

    for number, line in lines:
        tokens = line.split()
        head = tokens[0]
        if _FILE_ARG.search(line):
            reasons.append(f"line {number}: file arguments are not allowed in a model")
            continue

        if head.startswith("."):
            directive = head.lower()
            if directive in DROPPED_DIRECTIVES:
                continue
            if directive in _REFUSED:
                reasons.append(f"line {number}: {head} {_REFUSED[directive]}")
                continue
            if directive in _ANALYSIS:
                reasons.append(f"line {number}: {head} is an analysis or output command, not part of a model")
                continue
            if directive not in ALLOWED_DIRECTIVES:
                reasons.append(f"line {number}: {head} is not a statement a device model needs")
                continue

            if directive == ".subckt":
                if len(tokens) < 3:
                    reasons.append(f"line {number}: .subckt needs a name and at least one pin")
                    continue
                name = tokens[1]
                if _RESERVED.match(name):
                    reasons.append(f"line {number}: subcircuit names starting with EMI_ are reserved")
                    continue
                if model.subckt(name):
                    reasons.append(f"line {number}: subcircuit {name} is defined twice")
                    continue
                model.subckts[name] = _subckt_pins(tokens[2:])
                if not model.subckts[name]:
                    reasons.append(f"line {number}: subcircuit {name} has no pins")
                stack.append(name)
            elif directive == ".ends":
                if not stack:
                    reasons.append(f"line {number}: .ends without a matching .subckt")
                    continue
                if len(tokens) > 1 and tokens[1].lower() != stack[-1].lower():
                    reasons.append(f"line {number}: .ends {tokens[1]} closes {stack[-1]}")
                stack.pop()
            elif directive == ".model":
                if len(tokens) < 3:
                    reasons.append(f"line {number}: .model needs a name and a type")
                    continue
                if _RESERVED.match(tokens[1]):
                    reasons.append(f"line {number}: model names starting with EMI_ are reserved")
                    continue
                model.models.add(tokens[1].lower())
            kept.append(line)
            continue

        letter = head[0].upper()
        if letter not in ALLOWED_ELEMENTS:
            what = _ELEMENT_NAMES.get(letter, f"an element of type {letter}")
            reasons.append(f"line {number}: {head} is {what}, which a device model does not need")
            continue
        if not stack:
            reasons.append(
                f"line {number}: {head} is outside any .subckt, so it would be added to the "
                f"simulated circuit itself"
            )
            continue
        if letter == "X":
            instances.append((number, _instance_target(tokens)))
        elif letter == "D" and len(tokens) >= 4:
            diode_models.append((number, tokens[3]))
        kept.append(line)

    if stack:
        reasons.append(f"subcircuit {stack[-1]} is never closed with .ends")
    if not model.subckts:
        reasons.append("the file defines no .subckt, so there is nothing to connect to the board")
    for number, target in instances:
        if not model.subckt(target):
            reasons.append(f"line {number}: instantiates {target}, which the file does not define")
    for number, name in diode_models:
        if name.lower() not in model.models:
            reasons.append(f"line {number}: uses diode model {name}, which the file does not define")

    if reasons:
        raise ModelRejected(reasons)
    model.text = "\n".join(kept)
    return model
