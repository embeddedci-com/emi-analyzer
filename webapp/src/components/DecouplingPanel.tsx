/**
 * The decoupling view: per supply rail and IC, what the capacitors filter and what they don't.
 *
 * The worker builds a lumped model of each IC's supply from the layout (rules.json
 * `decoupling`, worker/emi_worker/rules/decoupling_view.py). This lists the rails and ICs,
 * and for the one selected draws |Z| against the target, lists its capacitors and ranks what
 * would close the worst gap. The target is editable here, and everything follows it.
 */

import { useEffect, useMemo, useState } from 'react'
import {
  Badge, Box, Button, Group, Modal, NumberInput, Stack, Table, Text, UnstyledButton,
} from '@mantine/core'
import {
  branchesOf, curve, fmtFarads, fmtHz, fmtRange, frequencies, gapsOf, noiseMarks,
  applyChange, partModelLabel, rankRecommendations, targetOhm,
  type DecIC, type DecouplingDoc, type DecRail,
} from '../lib/decoupling'
import { DecouplingChart } from './DecouplingChart'
import { fmtOhm } from './ComponentImpedancePreview'
import { hexToRgb, type BoardMarker } from '../lib/markers'

export interface DecouplingSelection {
  rail: DecRail
  ic: DecIC
  /** A capacitor picked in the table, or null. */
  cap: string | null
}

export interface DecouplingPanelProps {
  doc: DecouplingDoc | null | undefined
  /** Called when the selection changes, so the page can mark the parts on the board. */
  onSelect?: (s: DecouplingSelection | null) => void
  /** Zoom the board to a part. */
  onFocus?: (x: number, y: number) => void
}

/** The IC and its capacitors, for the board. The picked capacitor stands out. */
export function decouplingMarkers(s: DecouplingSelection): BoardMarker[] {
  const out: BoardMarker[] = [{ x: s.ic.x, y: s.ic.y, label: s.ic.ref, color: hexToRgb('#fd7e14') }]
  for (const c of s.ic.caps) {
    const p = s.rail.parts[c.ref]
    if (!p) continue
    out.push({ x: p.x, y: p.y, label: c.ref, color: hexToRgb(c.ref === s.cap ? '#be4bdb' : '#ffffff') })
  }
  return out
}

const key = (rail: string, ic: string) => `${rail}\u0000${ic}`

function statusText(ic: DecIC): string {
  if (!ic.caps.length) return 'no capacitors'
  if (ic.status === 'ok') return 'ok'
  const first = ic.gaps[0]
  return `gaps at ${fmtRange(first)}${ic.gaps.length > 1 ? ` +${ic.gaps.length - 1}` : ''}`
}

export function DecouplingPanel({ doc, onSelect, onFocus }: DecouplingPanelProps) {
  const [selected, setSelected] = useState<string | null>(null)
  const [cap, setCap] = useState<string | null>(null)
  const [whatIf, setWhatIf] = useState<number | null>(null)

  const entries = useMemo(
    () => (doc?.rails ?? []).flatMap((rail) => rail.ics.map((ic) => ({ rail, ic }))),
    [doc],
  )
  // Open on the worst IC: that is the one worth reading first.
  const initial = useMemo(() => {
    const worst = [...entries].sort((a, b) =>
      (b.ic.worst?.excess_db ?? -Infinity) - (a.ic.worst?.excess_db ?? -Infinity))[0]
    return worst ? key(worst.rail.net, worst.ic.ref) : null
  }, [entries])
  const current = entries.find((e) => key(e.rail.net, e.ic.ref) === (selected ?? initial)) ?? null

  useEffect(() => {
    onSelect?.(current ? { rail: current.rail, ic: current.ic, cap } : null)
    // The selection is the dependency, not the callback's identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current?.rail.net, current?.ic.ref, cap])

  if (!doc) {
    return <Text size="sm" c="dimmed">Analyze the board again to see its decoupling.</Text>
  }
  if (!entries.length) {
    return (
      <Text size="sm" c="dimmed">
        No IC supply pins found. ICs are parts named U or IC, on nets named like a supply
        (3V3, VDD, VCC).
      </Text>
    )
  }

  return (
    <Stack gap="sm">
      <Text size="xs" c="dimmed">{doc.note}{doc.stackup_assumed ? ' Stackup assumed.' : ''}</Text>
      <Stack gap={6}>
        {doc.rails.map((rail) => (
          <Box key={rail.net}>
            <Text size="xs" fw={600}>
              {rail.net} <Text span size="xs" c="dimmed" fw={400}>
                {rail.v} V{rail.v_assumed ? ' (assumed)' : ''}{rail.plane ? `, plane on ${rail.plane.layer}` : ''}
              </Text>
            </Text>
            {rail.ics.map((ic) => {
              const k = key(rail.net, ic.ref)
              const active = current && key(current.rail.net, current.ic.ref) === k
              return (
                <UnstyledButton key={k} w="100%" px={6} py={2}
                                onClick={() => { setSelected(k); setCap(null); setWhatIf(null); onFocus?.(ic.x, ic.y) }}
                                style={{
                                  borderRadius: 4,
                                  background: active ? 'var(--mantine-color-default-hover)' : undefined,
                                }}>
                  <Group justify="space-between" gap={6} wrap="nowrap">
                    <Text size="xs" ff="monospace">{ic.ref}</Text>
                    <Badge size="xs" variant="light" color={ic.status === 'ok' ? 'green' : 'red'}
                           style={{ textTransform: 'none' }}>
                      {statusText(ic)}
                    </Badge>
                  </Group>
                </UnstyledButton>
              )
            })}
          </Box>
        ))}
      </Stack>
      {current && (
        <IcDetail key={key(current.rail.net, current.ic.ref)} doc={doc} rail={current.rail}
                  ic={current.ic} cap={cap} whatIf={whatIf}
                  onCap={(ref) => {
                    setCap(ref)
                    const p = ref ? current.rail.parts[ref] : null
                    if (p) onFocus?.(p.x, p.y)
                  }}
                  onWhatIf={setWhatIf} />
      )}
    </Stack>
  )
}

function IcDetail({ doc, rail, ic, cap, whatIf, onCap, onWhatIf }: {
  doc: DecouplingDoc
  rail: DecRail
  ic: DecIC
  cap: string | null
  whatIf: number | null
  onCap: (ref: string | null) => void
  onWhatIf: (i: number | null) => void
}) {
  const [volts, setVolts] = useState<number>(rail.v)
  const [ripple, setRipple] = useState<number>(ic.ripple_pct)
  const [step, setStep] = useState<number>(ic.step_current_a)
  const [enlarged, setEnlarged] = useState(false)
  const target = targetOhm(volts, ripple, step)

  const freqs = useMemo(() => frequencies(doc), [doc])
  const branches = useMemo(() => branchesOf(ic, rail.parts), [ic, rail.parts])
  const total = useMemo(() => curve(branches, ic.series_l_h, freqs), [branches, ic.series_l_h, freqs])
  const ranked = useMemo(
    () => (Number.isFinite(target) ? rankRecommendations(ic, rail.parts, freqs, target) : []),
    [ic, rail.parts, freqs, target],
  )
  const gaps = Number.isFinite(target) ? gapsOf(freqs, total, target, ic.band_hz) : []
  const noise = noiseMarks(ic.noise, branches, ic.series_l_h, target, ic.band_hz, doc.f_max_hz)
  const unfiltered = noise.filter((n) => n.notFiltered)
  const inBand = ic.anti_resonances.filter((a) => a.hz <= ic.band_hz)
  const whatIfCurve = whatIf !== null && ranked[whatIf]
    ? curve(applyChange(branches, ranked[whatIf].change), ic.series_l_h, freqs)
    : null

  return (
    <Stack gap="xs" pt={4} style={{ borderTop: '1px solid var(--mantine-color-default-border)' }}>
      <Group justify="space-between" align="baseline">
        <Text size="sm" fw={600}>{ic.ref} on {rail.net}</Text>
        <Text size="xs" c="dimmed">pins {ic.pins.join(', ')}</Text>
      </Group>

      <Group gap="xs" grow>
        <NumberInput size="xs" label="Rail V" value={volts}
                     min={0.1} step={0.1} decimalScale={2} onChange={(v) => setVolts(Number(v) || 0)} />
        <NumberInput size="xs" label="Ripple %" value={ripple} min={0.1} step={1} decimalScale={1}
                     onChange={(v) => setRipple(Number(v) || 0)} />
        <NumberInput size="xs" label="Step A" value={step} min={0.01} step={0.1}
                     decimalScale={2} onChange={(v) => setStep(Number(v) || 0)} />
      </Group>
      <Text size="xs">
        Target <Text span ff="monospace" size="xs" fw={600}>{Number.isFinite(target) ? fmtOhm(target) : '-'}</Text>
        {' '}up to {fmtHz(ic.band_hz)}
        {rail.v_assumed ? ', rail voltage and step assumed' : ', step assumed'}.{' '}
        {gaps.length
          ? <Text span size="xs" c="red.7" fw={600}>Above it at {gaps.map(fmtRange).join(', ')}.</Text>
          : <Text span size="xs" c="green.8" fw={600}>Below it everywhere.</Text>}
      </Text>

      <Group justify="flex-end" mb={-8}>
        <Button size="compact-xs" variant="subtle" onClick={() => setEnlarged(true)}>Enlarge</Button>
      </Group>
      <DecouplingChart freqs={freqs} total={total} branches={branches} target={target}
                       bandHz={ic.band_hz} antiResonances={ic.anti_resonances} noise={noise}
                       selected={cap} whatIf={whatIfCurve} width={380} height={240} fontSize={11} />
      <Modal opened={enlarged} onClose={() => setEnlarged(false)} size="90%" centered
             title={`${ic.ref} on ${rail.net}`}>
        <DecouplingChart freqs={freqs} total={total} branches={branches} target={target}
                         bandHz={ic.band_hz} antiResonances={ic.anti_resonances} noise={noise}
                         selected={cap} whatIf={whatIfCurve} width={1000} height={440} fontSize={12} />
      </Modal>

      {ic.noise.length > 0 && (
        <Text size="xs">
          Noise: {ic.noise.map((n) => `${fmtHz(n.hz)} (${n.source})`).join(', ')}.{' '}
          {unfiltered.length
            ? <Text span size="xs" c="red.7">Not filtered: {unfiltered.map((n) => fmtHz(n.hz)).join(', ')}.</Text>
            : 'Filtered up to the board range.'}
        </Text>
      )}
      {/* Listed within the board range only; the chart marks them all. */}
      {inBand.length > 0 && (
        <Text size="xs">
          Anti-resonance: {inBand.map((a) => `${fmtOhm(a.ohm)} at ${fmtHz(a.hz)}`).join(', ')}.
        </Text>
      )}

      {ranked.length > 0 && (
        <Stack gap={4}>
          <Text size="xs" fw={600} tt="uppercase" c="dimmed">What would help</Text>
          {ranked.map((r, i) => (
            <UnstyledButton key={r.text} px={6} py={3}
                            onClick={() => onWhatIf(whatIf === i ? null : i)}
                            style={{
                              borderRadius: 4,
                              background: whatIf === i ? 'var(--mantine-color-default-hover)' : undefined,
                            }}>
              <Text size="xs">{r.text}</Text>
              <Text size="xs" c="dimmed">
                {[
                  r.improvementDb > 0.1 ? `${r.improvementDb.toFixed(1)} dB lower at ${fmtHz(r.atHz)}` : '',
                  r.fixesNow.length ? `fixes ${r.fixesNow.map(fmtRange).join(', ')}` : '',
                ].filter(Boolean).join(', ')}
              </Text>
            </UnstyledButton>
          ))}
        </Stack>
      )}

      <Table.ScrollContainer minWidth={380}>
        <Table fz="xs" verticalSpacing={2} highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Part</Table.Th>
              <Table.Th>Value</Table.Th>
              <Table.Th ta="right">To pin</Table.Th>
              <Table.Th ta="right">Loop</Table.Th>
              <Table.Th ta="right">Useful to</Table.Th>
              <Table.Th>Model</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {ic.caps.map((c) => {
              const p = rail.parts[c.ref]
              const label = partModelLabel(p)
              return (
                <Table.Tr key={c.ref} onClick={() => onCap(cap === c.ref ? null : c.ref)}
                          style={{ cursor: 'pointer' }}
                          bg={cap === c.ref ? 'var(--mantine-color-default-hover)' : undefined}>
                  <Table.Td ff="monospace">{c.ref}</Table.Td>
                  <Table.Td>{p ? `${fmtFarads(p.c_f)}${p.package ? ` ${p.package}` : ''}` : ''}</Table.Td>
                  <Table.Td ta="right">{c.distance_mm.toFixed(1)} mm</Table.Td>
                  <Table.Td ta="right"
                            title={`${c.connection_nh.toFixed(2)} nH connection${c.spreading_nh ? ` + ${c.spreading_nh.toFixed(2)} nH through the planes` : ''}, ${c.ground_vias} ground via${c.ground_vias === 1 ? '' : 's'}`}>
                    {(c.mount_l_h * 1e9).toFixed(1)} nH
                  </Table.Td>
                  <Table.Td ta="right">{fmtHz(c.useful_up_to_hz)}</Table.Td>
                  <Table.Td c={p?.source === 'assumed' ? 'orange.8' : undefined}
                            title={label.title}>
                    {label.text}
                  </Table.Td>
                </Table.Tr>
              )
            })}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
      {ic.assumed.length > 0 && (
        <Text size="xs" c="dimmed">Assumed from the layout: {ic.assumed.join(', ')}.</Text>
      )}
      {rail.plane && (
        <Text size="xs" c="dimmed">
          Plane pair {rail.plane.layer} / {rail.plane.ground_layer}, {rail.plane.cavity_mm} mm apart:
          {' '}{fmtFarads(rail.plane.c_f)}. First resonance near {fmtHz(rail.plane.resonance_hz)}.
        </Text>
      )}
    </Stack>
  )
}
