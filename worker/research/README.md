# Research scripts

One-off experiments from the model's design. Each answered one question before the
production code was written, and the answer is in [docs/](../../docs/) and in the comments
of the code that came out of it. They are kept so the numbers can be checked, not as tools:
they are not maintained, not tested, and may no longer run against the current worker.

| Script | Question |
|---|---|
| `spike_m0_nec.py` | Can we write, run and parse a nec2c deck? |
| `spike_m0_wire_fdtd.py` | Do openEMS and nec2c agree on the same wire? |
| `spike_m0_wire_nec.py` | Which nec2c wire matches the FDTD one? |
| `spike_m0_coupling.py` | Is the Thevenin voltage at the connector independent of the cable? |
| `spike_m0_linearity.py` | Is a solve's transfer function independent of the excitation? |
| `spike_m0_record.py` | Was each recorded run long enough to transform? |
| `spike_m0_nf2ff.py` | Does nf2ff give a dipole's far field, and does Mirror do the ground? |
| `spike_m0_nf2ff_size.py` | How big is an NF2FF recording at 60 frequencies? |
| `spike_m3_cable_test4.py` | Tier B against Tier C on real boards |
| `ff_verify_dipole.py` | Does the board far field match nec2c on dipoles over ground? |
| `ff_verify_slope.py` | Does a loop and a short dipole rise per volt as theory says? |
| `ff_fixture_explore.py`, `ff_fixture_decompose.py` | Why did the fixture board's far field rise 20 dB/decade? |
| `ff_real_board.py` | What does the far field cost, and does it run, on a real board? |
| `ff_explore.py` | Which knob moves a short dipole's far field? |
| `verify_cable_tier_a.py` | Does nec2c's product deck agree with openEMS on the same wire? |

The `ff_` scripts share `ff_harness.py` and are written up in
[docs/verification/far-field.md](../../docs/verification/far-field.md). Unlike the spikes they
run against the current worker.

## Running one

Most need openEMS or nec2c, so they run inside the worker image with this folder's parent
mounted. The docstring at the top of each script has its exact command. The pattern:

```sh
docker run --rm -v "$PWD/worker:/spike" -v "$PWD/spike_out:/spike/spike_out" -w /spike \
    -e PYTHONPATH=/spike --entrypoint python3 ghcr.io/embeddedci-com/emi-worker:dev \
    research/spike_m0_coupling.py
```

## Your own boards

The scripts that read real boards were run on private designs, which are not in this
repository. Point them at yours the same way as the test suite (`EMI_TEST_BOARDS`): a folder
with one subfolder per board, each holding a `.kicad_pcb`. Mount it at `/boards`:

```sh
docker run --rm -v "$PWD/worker:/spike" -v "$EMI_TEST_BOARDS:/boards:ro" \
    -v "$PWD/spike_out:/spike/spike_out" -w /spike -e PYTHONPATH=/spike \
    -e SETUPS=my-board:USB1:usb2-shielded \
    --entrypoint python3 ghcr.io/embeddedci-com/emi-worker:dev research/spike_m3_cable_test4.py
```

- `spike_m0_nf2ff_size.py` reads one board from `BOARD` (a path inside the container).
- `spike_m3_cable_test4.py` reads `SETUPS`: `folder:connector-ref:cable`, comma separated,
  where the cable is an id from the worker's cable library.

Tools that are maintained, such as the fixture generators, live in [scripts/](../scripts/).
