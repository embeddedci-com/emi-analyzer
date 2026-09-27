# The component library

September 2026. The library says how to model a capacitor that would otherwise be bare
copper (in a solve) or an assumed part (in the decoupling view). It lives in
`worker/emi_worker/components/library/` and has two halves:

- **`mlcc.json`, generic.** Typical figures per package and value class, plus one family
  entry that answers for any value in a known package. Shown as "generic 0402" (with the
  dielectric when the Value field names one). Never attributed to a manufacturer.
- **`vendor.json`, named parts.** Each entry is one part number, with its numbers from the
  manufacturer's own model and a citation (URL, document, revision). Shown as
  "datasheet (Samsung CL05B104KO5NNNC)". Chosen only when the board names the part.

## What is in it

The MLCCs on JLCPCB's Basic Parts list, which many hobby and small-company boards are built
from. The list came from JLCPCB's parts search (jlcpcb.com/parts, basic library, category
"Multilayer Ceramic Capacitors MLCC - SMD/SMT") on 2026-09-27: 88 parts, 58 Samsung, 26 FH
(Fenghua), 3 Yageo, 1 Murata.

59 of them are in the library:

| | 0402 | 0603 | 0805 | 1206 |
|---|---|---|---|---|
| Samsung (CL05, CL10, CL21, CL31) | 9 | 24 | 18 | 7 |
| Murata (GRM21BR61H106KE43L) | | | 1 | |

16 C0G, 23 X7R and 20 X5R. Each entry carries the MPN, the base part number without the
packaging code, and the LCSC number.

**Left out, because the manufacturer publishes nothing to model them from:**

- **FH (Fenghua), 26 parts** (C1523, C1530, C1532, C1538, C1546, C1547, C1548, C1549, C1554,
  C1555, C1562, C1567, C1594, C1604, C1620, C1631, C1658, C1739, C1743, C1744, C1846, C9196,
  C29823, C53987, C57112, C1322360). Fenghua's catalogs (fhcomp.com, General Series A0006)
  give no ESR, ESL, impedance curve or simulation model.
- **Yageo, 3 parts** (C14663 CC0603KRX7R9BB104, C49678 CC0805KRX7R9BB104, C107145
  CC0805KRX7R9BB221). Yageo's simulator (Y-SIM, which runs on KEMET's K-SIM) does not carry
  CC parts for simulation or model export, and the spec sheets only have raster plots cut off
  below the resonance.

These parts get the generic model for their package, as before.

## Where the numbers come from

| Manufacturer | Source | Revision |
|---|---|---|
| Samsung | Precise SPICE model per part, at 0 V, 3.3 V and 5 V, 25 °C, from the Component Library (weblib.samsungsem.com/mlcc/mlcc-ec.do?partNumber=...) | SPICE model version 5.0, generated 2026-09-27 |
| Murata | Static SPICE netlist per part, GRM library (murata.com/en-global/tool/data/spicedata/netlist-mlcc) | v78, data generated 2026-06-19 |

Both are ladder networks, not a single R-L-C. Each entry's series R-L-C is fitted to the
model's |Z|:

1. The notch is where the model's reactance crosses zero. The fit holds it: ESL = 1 / ((2π
   f_notch)² C).
2. ESR and C are chosen by least squares on |Z| in dB from half to twice the notch.
3. The model's |Z| at 0.5, 0.71, 1, 1.41 and 2 times the notch is stored with the entry
   (`reference`), and `tests/test_component_vendor_library.py` checks the R-L-C against it.

Result: the notch matches for every part. |Z| is within 2 dB from half to twice the notch for
all 59 and within 1 dB for the small ones the test names (median worst case 0.7 dB). Bulk X5R
parts are the limit: their notch is a broad valley that one R-L-C cannot follow, so they sit
1.2 to 1.7 dB off beside it and up to 9 dB off a decade away.

Samsung also publishes a "Simple" R-L-C, cited on each entry as a cross-check. It is not used:
it puts the self-resonance of bulk X5R parts up to 1.8 times above the notch of Samsung's own
Precise model, and the decoupling view's point is where each part stops working.

**DC bias** is recorded, not applied. Samsung entries carry C from the same fit at 3.3 V and
5 V (for example the 10 µF 0402 CL05A106MQ5NUNC keeps 46 % at 3.3 V and 29 % at 5 V). The
Murata netlist is 0 V only. The model uses the 0 V figure, which is optimistic for a bulk X5R
part on a 3.3 V or 5 V rail.

## How a part is matched

In order, first match wins:

1. **Part number.** Ingest keeps the footprint fields that name a part: `MPN`,
   `Manufacturer Part Number`, `Part Number`, `LCSC`, `LCSC Part`, `LCSC Part #`,
   `JLCPCB Part #` and similar, matched case-insensitively. A field whose value is the MPN,
   an alias or the LCSC number of a component picks it: the user's own, then shared, then
   unsaved, then built in. This needs neither a readable Value nor a standard footprint.
   When the board's Value or footprint disagrees with the part number, the part number wins
   and the part says so.
2. **Value and package**, in the same order, then the generic family by package.

A named part in the built-in library never answers for value and package alone: two of
JLCPCB's Basic parts are both 100 nF 0402. A user's own component with an MPN typed in still
matches on value and package, as it always has.

On four private boards, 357 of 499 two-pad capacitors carry a part-number field (LCSC on
354, MPN on 149) and 332 now match a named part. The other 25 name FH or non-Basic parts.

## Adding a part

1. Get the manufacturer's own model: a SPICE netlist, an S-parameter file or an impedance
   curve. Not a distributor's figure and not a blog.
2. Fit it as above, or take the manufacturer's R-L-C if it states one and check it against
   their impedance curve. Say which in the source's `what`.
3. Add an entry to `vendor.json` with `provenance: "vendor"`, `manufacturer`, `match` (`mpn`,
   `mpn_aliases`, `lcsc`, `value`, `package`), `model`, `valid_hz`, `reference` and `sources`
   (`doc`, `rev`, `url`, `what`). Add `dc_bias` only with a source for it.
4. Run the worker tests. `parse()` refuses an entry without a source; the library tests check
   the URL, the revision, unique part numbers, plausible ESL and self-resonance for the
   package, and the fit against `reference`.

Do not commit the manufacturer's files. Commit only the numbers and where they came from.
