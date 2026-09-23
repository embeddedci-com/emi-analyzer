/**
 * Drivers: the measured or declared source attached to a net (§9.4).
 *
 * A solve on its own is relative — it says one layout radiates 8 dB less than another, not
 * how many µA/m either of them produces. A driver is what turns that into absolute units,
 * and because openEMS solves a linear structure it is arithmetic on a finished result rather
 * than another run (§8).
 *
 * The form's job is mostly to be honest about provenance. Every number carries where it came
 * from, the weakest one sets the driver's confidence term (§17.2), and the panel shows that
 * on a chip — so a driver built from one guessed number reads differently at a glance from
 * one measured throughout.
 */

import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ActionIcon, Alert, Badge, Button, Card, FileInput, Group, NumberInput, SegmentedControl,
  Select, Stack, Text, TextInput, Title, Tooltip,
} from '@mantine/core'
import type { EmiApi, StoredDriver } from '../lib/emiApi'
import {
  parseDriverDocument, SOURCE_SIGMA_DB, SOURCES, sigmaDb, weakestSource, type Source,
} from '../lib/driverDocument'
import { DriverError, type Trapezoid } from '../lib/driverSpectrum'
import { DriverSpectrumPreview } from './DriverSpectrumPreview'
import { documentFromFile } from '../lib/driverUpload'

interface Props {
  api: EmiApi
  projectId: string
  /** The solve's frequencies, so the preview can mark which ones a driver actually drives. */
  frequencies?: number[]
  nets?: string[]
}

/** A number plus where it came from — the shape every driver field takes. */
export interface Field {
  value: number
  source: Source
}

const SOURCE_OPTIONS = SOURCES.map((s) => ({
  value: s,
  label: `${s} (±${SOURCE_SIGMA_DB[s]} dB)`,
}))

export const DRIVER_FORM_DEFAULTS: Record<string, Field> = {
  amplitude_v: { value: 3.3, source: 'datasheet' },
  period_ns: { value: 40, source: 'assumed' },
  pulse_width_ns: { value: 20, source: 'assumed' },
  rise_ns: { value: 1.2, source: 'datasheet' },
  fall_ns: { value: 1.2, source: 'datasheet' },
  source_impedance_ohm: { value: 40, source: 'assumed' },
}

/**
 * Build an emi-driver document from the form's fields.
 *
 * Exported and pure because of the unit conversion. The form works in nanoseconds, which is
 * what a datasheet quotes, while §9.2's document is in seconds — and a missing or doubled
 * 1e-9 would move every harmonic by nine decades while still producing a document that
 * validates and a spectrum that looks like a spectrum.
 */
export function buildTrapezoidDocument(
  fields: Record<string, Field>,
  name: string,
  net: string | null,
): Record<string, unknown> {
  const f = fields
  return {
    format: 'emi-driver',
    version: 1,
    name: name.trim() || 'Untitled driver',
    ...(net ? { net } : {}),
    kind: 'trapezoid',
    role: 'signal',
    trapezoid: {
      amplitude_v: { value: f.amplitude_v.value, source: f.amplitude_v.source },
      period_s: { value: f.period_ns.value * 1e-9, source: f.period_ns.source },
      pulse_width_s: { value: f.pulse_width_ns.value * 1e-9, source: f.pulse_width_ns.source },
      rise_s: { value: f.rise_ns.value * 1e-9, source: f.rise_ns.source },
      fall_s: { value: f.fall_ns.value * 1e-9, source: f.fall_ns.source },
      source_impedance_ohm: {
        value: f.source_impedance_ohm.value, source: f.source_impedance_ohm.source,
      },
    },
  }
}

function sourceBadgeColor(source: string): string {
  const sigma = SOURCE_SIGMA_DB[source as Source] ?? 6
  if (sigma <= 1) return 'teal'
  if (sigma <= 1.5) return 'green'
  if (sigma <= 3) return 'yellow'
  return 'orange'
}

function SourcedNumber({
  label, unit, field, onChange, step,
}: {
  label: string
  unit: string
  field: Field
  onChange: (f: Field) => void
  step?: number
}) {
  return (
    <Group gap="xs" align="flex-end" wrap="nowrap">
      <NumberInput
        label={label}
        size="xs"
        w={150}
        value={field.value}
        step={step}
        onChange={(v) => onChange({ ...field, value: typeof v === 'number' ? v : 0 })}
        rightSection={<Text size="10px" c="dimmed" pr={6}>{unit}</Text>}
        rightSectionWidth={42}
      />
      <Select
        label="from"
        size="xs"
        w={175}
        data={SOURCE_OPTIONS}
        value={field.source}
        allowDeselect={false}
        onChange={(v) => onChange({ ...field, source: (v as Source) ?? 'assumed' })}
      />
    </Group>
  )
}

export function DriversPanel({ api, projectId, frequencies = [], nets = [] }: Props) {
  const qc = useQueryClient()
  const [adding, setAdding] = useState(false)
  const [kind, setKind] = useState<'trapezoid' | 'waveform' | 'spectrum'>('trapezoid')
  const [uploaded, setUploaded] = useState<Record<string, unknown> | null>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [net, setNet] = useState<string | null>(null)
  const [fields, setFields] = useState<Record<string, Field>>(DRIVER_FORM_DEFAULTS)

  const drivers = useQuery({
    queryKey: ['emi', 'drivers', projectId],
    queryFn: () => api.listDrivers(projectId),
  })

  const document = useMemo(
    () => (kind === 'trapezoid' ? buildTrapezoidDocument(fields, name, net) : uploaded ?? {}),
    [kind, fields, name, net, uploaded],
  )

  // Validate with the same code the worker runs, so the form cannot accept something the
  // server or the solve would later refuse.
  const parsed = useMemo(() => {
    try {
      return { driver: parseDriverDocument(document), error: null as string | null }
    } catch (e) {
      return {
        driver: null,
        error: e instanceof DriverError ? e.message : String((e as Error).message),
      }
    }
  }, [document])

  const trapezoid: Trapezoid = useMemo(() => ({
    amplitude_v: fields.amplitude_v.value,
    period_s: fields.period_ns.value * 1e-9,
    pulse_width_s: fields.pulse_width_ns.value * 1e-9,
    rise_s: fields.rise_ns.value * 1e-9,
    fall_s: fields.fall_ns.value * 1e-9,
  }), [fields])

  const create = useMutation({
    mutationFn: () => api.createDriver(projectId, document),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['emi', 'drivers', projectId] })
      setAdding(false)
      setName('')
      setFields(DRIVER_FORM_DEFAULTS)
      setUploaded(null)
      setUploadError(null)
      setKind('trapezoid')
    },
  })

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteDriver(projectId, id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['emi', 'drivers', projectId] }),
  })

  const exportJson = () => {
    const blob = new Blob([JSON.stringify(document, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = window.document.createElement('a')
    a.href = url
    a.download = `${(name.trim() || 'driver').replace(/[^\w.-]+/g, '-')}.emi-driver.json`
    a.click()
    URL.revokeObjectURL(url)
  }

  const setField = (key: string) => (f: Field) => setFields((prev) => ({ ...prev, [key]: f }))

  return (
    <Stack gap="sm">
      <Group justify="space-between" align="center">
        <Stack gap={0}>
          <Title order={5}>Drivers</Title>
          <Text size="xs" c="dimmed">
            What a net actually carries. Attaching one turns a relative result into dBµA/m
            without re-solving.
          </Text>
        </Stack>
        {!adding && (
          <Button size="xs" variant="light" onClick={() => setAdding(true)}>
            New driver
          </Button>
        )}
      </Group>

      {drivers.isLoading && <Text size="sm" c="dimmed">Loading…</Text>}
      {drivers.error && (
        <Alert color="red" variant="light">{(drivers.error as Error).message}</Alert>
      )}
      {remove.isError && (
        <Alert color="red" variant="light" withCloseButton onClose={() => remove.reset()}>
          <Text size="xs">{(remove.error as Error).message}</Text>
        </Alert>
      )}

      {drivers.data?.length === 0 && !adding && (
        <Text size="sm" c="dimmed">
          No drivers yet. Results stay relative until one is attached.
        </Text>
      )}

      {drivers.data?.map((d: StoredDriver) => {
        let weakest = 'unknown'
        let sigma: number | null = null
        try {
          const parsedDoc = parseDriverDocument(d.document)
          weakest = weakestSource(parsedDoc)
          sigma = sigmaDb(parsedDoc)
        } catch {
          /* a stored document this build cannot read still lists, with its row fields */
        }
        return (
          <Card key={d.id} withBorder padding="xs">
            <Group justify="space-between" wrap="nowrap">
              <Stack gap={2}>
                <Group gap="xs">
                  <Text size="sm" fw={600}>{d.name}</Text>
                  <Badge size="xs" variant="light">{d.kind}</Badge>
                  {d.role !== 'signal' && (
                    <Badge size="xs" variant="light" color="grape">{d.role}</Badge>
                  )}
                </Group>
                <Group gap="xs">
                  {(d.document as { net?: string })?.net && (
                    <Text size="xs" c="dimmed" ff="monospace">
                      {(d.document as { net?: string }).net}
                    </Text>
                  )}
                  <Tooltip
                    label="The least certain number in this driver sets its confidence term"
                    withArrow
                  >
                    <Badge size="xs" variant="dot" color={sourceBadgeColor(weakest)}>
                      {weakest}{sigma !== null ? ` ±${sigma} dB` : ''}
                    </Badge>
                  </Tooltip>
                </Group>
              </Stack>
              <ActionIcon
                variant="subtle"
                color="red"
                size="sm"
                aria-label={`Delete ${d.name}`}
                loading={remove.isPending && remove.variables === d.id}
                onClick={() => remove.mutate(d.id)}
              >
                ×
              </ActionIcon>
            </Group>
          </Card>
        )
      })}

      {adding && (
        <Card withBorder padding="sm">
          <Stack gap="sm">
            <Group gap="xs" align="flex-end">
              <TextInput
                label="Name"
                size="xs"
                w={220}
                placeholder="U3 SPI clock"
                value={name}
                onChange={(e) => setName(e.currentTarget.value)}
              />
              {nets.length > 0 && (
                <Select
                  label="Net"
                  size="xs"
                  w={220}
                  searchable
                  clearable
                  data={nets}
                  value={net}
                  onChange={setNet}
                />
              )}
            </Group>

            <SegmentedControl
              size="xs"
              value={kind}
              onChange={(v) => {
                setKind(v as typeof kind)
                setUploaded(null)
                setUploadError(null)
              }}
              data={[
                { value: 'trapezoid', label: 'Trapezoid' },
                { value: 'waveform', label: 'Upload waveform' },
                { value: 'spectrum', label: 'Upload spectrum' },
              ]}
            />

            {kind !== 'trapezoid' && (
              <Stack gap={4}>
                <FileInput
                  size="xs"
                  w={360}
                  label={kind === 'waveform'
                    ? 'CSV of time and voltage, or an emi-driver JSON'
                    : 'CSV of frequency and level, or an emi-driver JSON'}
                  placeholder="Choose a file"
                  accept=".csv,.tsv,.txt,.json,application/json,text/csv"
                  onChange={async (file) => {
                    setUploadError(null)
                    setUploaded(null)
                    if (!file) return
                    try {
                      const text = await file.text()
                      setUploaded(documentFromFile(file.name, text, kind, {
                        name: name.trim() || file.name,
                        source: fields.amplitude_v.source,
                        sourceImpedanceOhm: fields.source_impedance_ohm.value,
                        ...(net ? { net } : {}),
                      }))
                    } catch (e) {
                      setUploadError((e as Error).message)
                    }
                  }}
                />
                <Text size="xs" c="dimmed">
                  Parsed here in the browser. A capture has to cover at least one whole period
                  and be evenly sampled — the document carries a single sample interval, so
                  uneven spacing cannot be represented and is refused rather than averaged.
                </Text>
                {uploadError && (
                  <Alert color="orange" variant="light" title="Could not read that file">
                    <Text size="xs">{uploadError}</Text>
                  </Alert>
                )}
              </Stack>
            )}

            {kind === 'trapezoid' && (
            <Group gap="lg" align="flex-start" wrap="wrap">
              <Stack gap="xs">
                <SourcedNumber label="Amplitude" unit="V" step={0.1}
                               field={fields.amplitude_v} onChange={setField('amplitude_v')} />
                <SourcedNumber label="Period" unit="ns" step={1}
                               field={fields.period_ns} onChange={setField('period_ns')} />
                <SourcedNumber label="Pulse width (50 %)" unit="ns" step={1}
                               field={fields.pulse_width_ns}
                               onChange={setField('pulse_width_ns')} />
              </Stack>
              <Stack gap="xs">
                <SourcedNumber label="Rise (0–100 %)" unit="ns" step={0.1}
                               field={fields.rise_ns} onChange={setField('rise_ns')} />
                <SourcedNumber label="Fall" unit="ns" step={0.1}
                               field={fields.fall_ns} onChange={setField('fall_ns')} />
                <SourcedNumber label="Source impedance" unit="Ω" step={5}
                               field={fields.source_impedance_ohm}
                               onChange={setField('source_impedance_ohm')} />
              </Stack>
            </Group>
            )}

            {kind !== 'trapezoid' && !uploaded ? null : parsed.error ? (
              <Alert color="orange" variant="light" title="Not a possible waveform">
                <Text size="xs">{parsed.error}</Text>
              </Alert>
            ) : (
              <>
                {kind === 'trapezoid' ? (
                  <DriverSpectrumPreview trapezoid={trapezoid} marks={frequencies} />
                ) : parsed.driver ? (
                  <Text size="xs" c="dimmed">
                    Read {parsed.driver.kind === 'waveform'
                      ? `${(parsed.driver.payload.samples_v as number[]).length.toLocaleString()} samples`
                      : `${(parsed.driver.payload.points as unknown[]).length.toLocaleString()} points`}
                    {' '}from the file.
                  </Text>
                ) : null}
                {parsed.driver && (
                  <Text size="xs" c="dimmed">
                    Weakest source: <b>{weakestSource(parsed.driver)}</b> — this driver
                    contributes ±{sigmaDb(parsed.driver)} dB to the confidence budget.
                  </Text>
                )}
              </>
            )}

            {create.error && (
              <Alert color="red" variant="light">{(create.error as Error).message}</Alert>
            )}

            <Group gap="xs">
              <Button size="xs" disabled={!parsed.driver} loading={create.isPending}
                      onClick={() => create.mutate()}>
                Save driver
              </Button>
              <Button size="xs" variant="default" onClick={exportJson} disabled={!parsed.driver}>
                Export JSON
              </Button>
              <Button size="xs" variant="subtle" onClick={() => setAdding(false)}>
                Cancel
              </Button>
            </Group>
          </Stack>
        </Card>
      )}
    </Stack>
  )
}
