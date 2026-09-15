# openEMS CSX XML — verified schema notes

We drive openEMS through its XML interface (`openEMS <file.xml>`) rather than the Python
bindings, because Debian's `openems` package ships the binaries but **not** the Python
module, and trixie dropped the package entirely. The XML interface is stable, version-
robust, and gives us exact control over the geometry.

Everything below was verified empirically against **openEMS v0.0.35 / CSXCAD v0.6.2** by
feeding candidate files to the binary and reading back `--debug-CSX`, which echoes the
canonical form of whatever it parsed. That echo is the ground truth; do not trust this file
over it.

Reproduce with:

```bash
openEMS candidate.xml --no-simulation --debug-CSX   # writes debugCSX.xml
```

A property whose element name or required attributes are wrong comes back as
`<Unknown … Property="unknown">` and openEMS prints `Warning: Unknown Property found!!!`.
That is the signal to look at.

## Skeleton

```xml
<?xml version="1.0" encoding="UTF-8"?>
<openEMS>
  <FDTD NumberOfTimesteps="20000" endCriteria="1e-4" f_max="3e9">
    <Excitation Type="0" f0="1.5e9" fc="1.5e9"/>
    <BoundaryCond xmin="PML_8" xmax="PML_8" ymin="PML_8"
                  ymax="PML_8" zmin="PML_8" zmax="PML_8"/>
  </FDTD>
  <ContinuousStructure CoordSystem="0">
    <Properties> … </Properties>
    <RectilinearGrid DeltaUnit="0.001" CoordSystem="0">
      <XLines>-12,-11,…</XLines>
      <YLines>…</YLines>
      <ZLines>…</ZLines>
    </RectilinearGrid>
  </ContinuousStructure>
</openEMS>
```

- `DeltaUnit="0.001"` means every coordinate in the file is in **millimetres**, which is
  what board.json already uses. No unit conversion anywhere.
- `<FDTD><Excitation>` defines the *signal*. It is unrelated to the `<Excitation>`
  *property* below, which defines *where* that signal is applied. Same word, two jobs.
- PML_8 needs at least 8 grid lines on that side or openEMS silently falls back to PEC and
  says so: `Operator_Ext_UPML::Create_UPML: Warning: Not enough lines in direction: N`.
  A PEC wall reflects everything, so a run that ignores this warning is measuring an echo
  chamber.

## Properties

Element names dispatch exactly; anything else becomes `Unknown`.

### Material — dielectric

```xml
<Material Name="FR4" Isotropy="1">
  <Property Epsilon="4.4" Mue="1" Kappa="0.002"/>
  <Primitives><Box Priority="0"><P1 X="…" Y="…" Z="…"/><P2 …/></Box></Primitives>
</Material>
```

Material puts its values in a **child `<Property>` element**. Scalars are accepted on
input and echoed back as 3-vectors.

### Metal — perfect conductor

```xml
<Metal Name="F.Cu">
  <Primitives> … </Primitives>
</Metal>
```

### Excitation — where the source is applied

```xml
<Excitation Name="port_exc" Number="0" Type="0" Excite="0,0,-1">
  <Primitives><Box Priority="5">…</Box></Primitives>
</Excitation>
```

**`Excite` is an attribute on the element itself, not a child `<Property>`.** This is the
one place the pattern differs from `Material`, and getting it wrong is silent: the property
parses as `Unknown` and openEMS then reports `CalcFieldExcitation: Warning, no excitation
properties found` — a *warning*, not an error, so the simulation runs happily and produces
a field of zeros.

`Type="0"` is a soft (E-field) excitation. `Excite` is the field vector; `0,0,-1` drives
−z, i.e. from the trace down to the plane.

### LumpedElement — the port's terminating resistance

```xml
<LumpedElement Name="port_res" Direction="2" Caps="1" R="50">
  <Primitives><Box Priority="5">…</Box></Primitives>
</LumpedElement>
```

`Direction` is the axis index (0=x, 1=y, 2=z). `Caps="1"` adds PEC caps so the element
actually connects to the metal either side of it.

### ProbeBox — voltage and current probes for S-parameters

```xml
<ProbeBox Name="port_ut1" Type="0" Weight="-1" NormDir="2"> … </ProbeBox>
<ProbeBox Name="port_it1" Type="1" Weight="1"  NormDir="0"> … </ProbeBox>
```

`Type="0"` integrates E along the box (a voltage); `Type="1"` integrates H around it
(a current). The current probe's box must **enclose** the conductor, so it is drawn half a
cell larger than the trace in the transverse directions.

Output is written as plain text files named after the probe (`port_ut1`, `port_it1`), not
HDF5.

### DumpBox — field output

```xml
<DumpBox Name="Jf" DumpType="12" DumpMode="2" FileType="1">
  <FD_Samples>1e9,2e9</FD_Samples>
  <Primitives><Box Priority="0">…</Box></Primitives>
</DumpBox>
```

`DumpType`: 0 = E time, 1 = H time, 2 = J time, 3 = total current time;
**add 10 for the frequency domain** — so 10 = E, 11 = H, **12 = J**, 13 = total current.

`DumpType="12"` with `FD_Samples` is what produces our copper hotspot map: surface current
density at named frequencies, which shows *which trace* is acting as the antenna.

`FileType="1"` selects HDF5.

**`DumpMode` matters more than it looks.** 0 is raw grid values, 1 is node interpolation,
2 is cell interpolation. Cell interpolation reports at cell *centres*, so a dump plane
placed on a copper layer is sampled half a cell away from the metal — and since surface
current exists only on the conductor, the entire map comes back zero. Measured on a real
board: with `DumpMode="2"` a layer at z = 1.5825 mm was sampled at 1.5336 mm and every cell
read 0; with `DumpMode="1"` it was sampled at 1.5825 mm exactly and every cell was
non-zero. **Use node interpolation for anything sampled on copper.**

**Frequency-domain dumps only.** A time-domain dump writes every timestep and reaches
hundreds of gigabytes; `FD_Samples` writes one complex field per named frequency and lands
in megabytes.

`<FD_Samples>` is a child element with comma-separated values, and must come *after*
`<Primitives>` in the echo — though order on input did not matter.

## Primitives

```xml
<Box Priority="10"><P1 X="" Y="" Z=""/><P2 X="" Y="" Z=""/></Box>

<Polygon Priority="10" NormDir="2" Elevation="1.6">
  <Vertex X1="-8" X2="-0.15"/>
  <Vertex X1="8"  X2="-0.15"/>
  …
</Polygon>

<LinPoly Priority="10" NormDir="2" Elevation="1.6" Length="0.035"> … </LinPoly>
```

`NormDir` is the normal axis (2 = z, i.e. a polygon lying in the xy plane, which is every
copper layer). `Elevation` is its position along that axis. Vertices use `X1`/`X2` for the
two in-plane coordinates, **not** X/Y.

`Polygon` is infinitely thin; `LinPoly` extrudes by `Length`. Copper is modelled as a
zero-thickness `Polygon` — 35 µm of copper thickness would force the vertical mesh finer
than the copper, multiplying the timestep cost for a detail that barely moves the answer.

`Priority` resolves overlaps: higher wins. Metal must outrank the dielectric it sits on.

## Progress output

openEMS writes a line every few seconds:

```
[@       17s] Timestep:  3708 || Speed: 4.7 MC/s (4.834e-03 s/TS) || Energy: ~2.27e-15 (- 0.00dB)
```

Timestep, throughput and energy-decay-in-dB are all there, which is exactly what the run
progress UI needs. Parse this rather than estimating from wall clock.

The run stops at `endCriteria` (energy decayed by that factor) or `NumberOfTimesteps`,
whichever comes first. Hitting the timestep cap first prints
`Warning: Max. number of timesteps was reached before the end-criteria` — the result is
usable but under-resolved, and the UI should say so rather than presenting it as converged.

## Performance note: architecture matters

Two measurements in the same 4-CPU arm64 container on Apple silicon:

| grid | throughput |
|---|---|
| 23 k cells | 4.8 MCells/s |
| 504 k cells | 210 MCells/s |

The small grid is not a benchmark — at that size the run is dominated by per-timestep
overhead rather than by memory traffic. At half a million cells the figure lands at
210 MCells/s, squarely inside the 150–250 range the cost model assumes.

Benchmark on a realistic grid or not at all. A quick timing on a toy structure will report
something like forty times too slow and send you looking for a problem that is not there.
