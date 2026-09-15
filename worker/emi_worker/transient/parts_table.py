"""Datasheet parameters for protection parts, keyed by part number.

Every value here is read from the manufacturer's datasheet, and every entry names the datasheet
and its revision. Nothing is filled in from memory or typical values: a field the datasheet
does not state is absent, and the model built from the entry says what it assumed instead.

The set is the parts found on the boards this analyzer was calibrated against. Read on
2026-09-11; where the manufacturer's site refused the download, the manufacturer's own PDF was
read from a distributor copy, and ``notes`` says so -- a newer revision may exist.

Entry fields (all optional except topology and source):

  manufacturer, datasheet (URL), revision
  topology      "tvs-uni" | "tvs-bi" | "steering-array" | "rail-clamp"
  pins          package pin number -> role ("io:1", "gnd", "rail", ...)
  feedthrough   pin -> pin, for arrays whose line passes through the package (the IC sits on
                the other pin's net)
  v_br_v        breakdown voltage the model uses: the typical when stated, otherwise the midpoint
                of min and max, otherwise the min -- ``v_br_basis`` says which
  i_t_ma        breakdown test current
  v_c_points    [[amps, volts, "conditions"], ...] maximum clamping voltage
  c_j_pf        line capacitance (typical when stated, otherwise the maximum)
  v_f_points    [[amps, volts], ...] diode forward voltage (maximum)
  notes         what affects modelling
"""

from __future__ import annotations

TABLE: dict[str, dict] = {
    "USBLC6-2SC6": {
        "manufacturer": "STMicroelectronics",
        "datasheet": "https://www.st.com/resource/en/datasheet/usblc6-2.pdf",
        "revision": "Doc ID 11265 Rev 5, October 2011",
        "topology": "steering-array",
        "pins": {"1": "io:1", "2": "gnd", "3": "io:2", "4": "io:2", "5": "rail", "6": "io:1"},
        "feedthrough": {"1": "6", "6": "1", "3": "4", "4": "3"},
        "v_br_v": 6.0, "v_br_basis": "min, VBUS to GND (the rail TVS)", "i_t_ma": 1.0,
        "v_c_points": [[1.0, 12.0, "8/20 µs, I/O to GND"], [5.0, 17.0, "8/20 µs, I/O to GND"]],
        "c_j_pf": 2.5,
        "v_f_points": [[0.01, 1.1]],
        "notes": "st.com timed out; ST's Rev 5 PDF read from a distributor copy. Pins 1/6 and 3/4 "
                 "are the same internal line (flow-through layout).",
    },
    "USBLC6-4SC6": {
        "manufacturer": "STMicroelectronics",
        "datasheet": "https://www.st.com/resource/en/datasheet/usblc6-4.pdf",
        "revision": "Doc ID 11068 Rev 4, September 2011",
        "topology": "steering-array",
        "pins": {"1": "io:1", "2": "gnd", "3": "io:2", "4": "io:3", "5": "rail", "6": "io:4"},
        "v_br_v": 6.0, "v_br_basis": "min, VBUS to GND (the rail TVS)", "i_t_ma": 1.0,
        "v_c_points": [[1.0, 12.0, "8/20 µs, I/O to GND"], [5.0, 17.0, "8/20 µs, I/O to GND"]],
        "c_j_pf": 3.0,
        "v_f_points": [[0.01, 0.86]],
        "notes": "st.com timed out; ST's Rev 4 PDF read from a distributor copy.",
    },
    "PESD5V0S1BA": {
        "manufacturer": "Nexperia",
        "datasheet": "https://assets.nexperia.com/documents/data-sheet/PESD5V0S1BA.pdf",
        "revision": "v.6, 26 April 2024",
        "topology": "tvs-bi",
        "pins": {"1": "io:1", "2": "gnd"},
        "v_br_v": 7.5, "v_br_basis": "midpoint of 5.5 V min and 9.5 V max", "i_t_ma": 1.0,
        "v_c_points": [[1.0, 10.0, "8/20 µs, pin 1 to pin 2"], [12.0, 14.0, "8/20 µs, pin 1 to pin 2"]],
        "c_j_pf": 35.0,
        "notes": "Symmetric bidirectional device; the 50 Ω differential resistance at 1 mA is a "
                 "low-current slope, not an ESD dynamic resistance, and is not used.",
    },
    "PESD3V3L4UG": {
        "manufacturer": "Nexperia",
        "datasheet": "https://assets.nexperia.com/documents/data-sheet/PESDXL4UF_G_W.pdf",
        "revision": "PESDxL4UF/UG/UW Rev. 04, 28 February 2008",
        "topology": "tvs-uni",
        "pins": {"1": "io:1", "2": "gnd", "3": "io:2", "4": "io:3", "5": "io:4"},
        "v_br_v": 5.6, "v_br_basis": "typical", "i_t_ma": 1.0,
        "v_c_points": [[1.0, 8.0, "8/20 µs, I/O to pin 2"], [3.0, 12.0, "8/20 µs, I/O to pin 2"]],
        "c_j_pf": 22.0,
        "notes": "Four unidirectional diodes, common anode on pin 2.",
    },
    "SRV05-4": {
        "manufacturer": "Semtech",
        "datasheet": "https://www.semtech.com/products/circuit-protection/tvs-diodes/srv05-4",
        "revision": "Revision 01/18/2008",
        "topology": "steering-array",
        "pins": {"1": "io:1", "2": "gnd", "3": "io:2", "4": "io:3", "5": "rail", "6": "io:4"},
        "v_br_v": 6.0, "v_br_basis": "min, pin 5 to pin 2 (the rail TVS)", "i_t_ma": 1.0,
        "v_c_points": [[1.0, 12.5, "8/20 µs, I/O to ground"], [5.0, 17.5, "8/20 µs, I/O to ground"]],
        "c_j_pf": 3.0,
        "v_f_points": [[0.015, 1.2]],
        "notes": "Semtech's 2008 PDF read from a distributor copy; the product page lists a "
                 "2018-12-15 revision that could not be retrieved. Rated to 12 A, tabulated to 5 A.",
    },
    "SMAJ5.0A": {
        "manufacturer": "Littelfuse",
        "datasheet": "https://www.littelfuse.com/products/tvs-diodes/surface-mount/smaj",
        "revision": "SMAJ Series, revised 02/22/22",
        "topology": "tvs-uni",
        "v_br_v": 6.7, "v_br_basis": "midpoint of 6.4 V min and 7.0 V max", "i_t_ma": 10.0,
        "v_c_points": [[43.5, 9.2, "10/1000 µs rating basis"]],
        "v_f_points": [[25.0, 3.5]],
        "notes": "littelfuse.com refused the download; Littelfuse's PDF read from a distributor copy. "
                 "Clamping is on the 10/1000 µs basis, not 8/20 µs. Capacitance is graph-only.",
    },
    "SMAJ22A": {
        "manufacturer": "Littelfuse",
        "datasheet": "https://www.littelfuse.com/products/tvs-diodes/surface-mount/smaj",
        "revision": "SMAJ Series, revised 02/22/22",
        "topology": "tvs-uni",
        "v_br_v": 25.65, "v_br_basis": "midpoint of 24.4 V min and 26.9 V max", "i_t_ma": 1.0,
        "v_c_points": [[11.3, 35.5, "10/1000 µs rating basis"]],
        "v_f_points": [[25.0, 3.5]],
        "notes": "Same datasheet and caveats as SMAJ5.0A.",
    },
    "SMAJ36CA": {
        "manufacturer": "Littelfuse",
        "datasheet": "https://www.littelfuse.com/products/tvs-diodes/surface-mount/smaj",
        "revision": "SMAJ Series, revised 02/22/22",
        "topology": "tvs-bi",
        "pins": {"1": "io:1", "2": "gnd"},
        "v_br_v": 42.1, "v_br_basis": "midpoint of 40 V min and 44.2 V max", "i_t_ma": 1.0,
        "v_c_points": [[6.9, 58.1, "10/1000 µs rating basis, both polarities"]],
        "notes": "Same datasheet and caveats as SMAJ5.0A; uni and bidirectional parts share the row.",
    },
    "SMBJ5.0A": {
        "manufacturer": "Littelfuse",
        "datasheet": "https://www.littelfuse.com/products/tvs-diodes/surface-mount/smbj",
        "revision": "SMBJ Series, revised 06/03/20",
        "topology": "tvs-uni",
        "v_br_v": 6.7, "v_br_basis": "midpoint of 6.4 V min and 7.0 V max", "i_t_ma": 10.0,
        "v_c_points": [[65.3, 9.2, "10/1000 µs rating basis"]],
        "v_f_points": [[50.0, 3.5]],
        "notes": "littelfuse.com refused the download; Littelfuse's PDF read from a re-hosted copy. "
                 "Clamping is on the 10/1000 µs basis. Capacitance is graph-only.",
    },
    "SMF13CA": {
        "manufacturer": "Diotec Semiconductor",
        "datasheet": "https://diotec.com/request/datasheet/smf50a.pdf",
        "revision": "SMF5.0 ... SMF220CA, version 2026-07-02",
        "topology": "tvs-bi",
        "pins": {"1": "io:1", "2": "gnd"},
        "v_br_v": 15.15, "v_br_basis": "midpoint of 14.4 V min and 15.9 V max", "i_t_ma": 1.0,
        "v_c_points": [[9.3, 21.5, "10/1000 µs, both directions"]],
        "notes": "A Littelfuse datasheet listing the CA part could not be retrieved; Diotec's is used, "
                 "and its SMF13A row matches Littelfuse's SMF13A exactly.",
    },
    "BAT54S": {
        "manufacturer": "Nexperia",
        "datasheet": "https://assets.nexperia.com/documents/data-sheet/BAT54S.pdf",
        "revision": "v.6, 1 July 2022",
        "topology": "rail-clamp",
        "pins": {"1": "anode-d1", "2": "cathode-d2", "3": "common"},
        "c_j_pf": 10.0,
        "v_f_points": [[0.0001, 0.24], [0.001, 0.32], [0.01, 0.40], [0.03, 0.50], [0.1, 0.80]],
        "notes": "A Schottky pair, not a TVS: no breakdown, clamping or ESD rating. Forward voltage "
                 "above 100 mA is graph-only, so discharge-level currents are extrapolated. The "
                 "usual rail-clamp wiring (pin 1 ground, pin 2 supply, pin 3 line) is assumed.",
    },
}
