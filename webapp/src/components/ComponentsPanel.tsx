/**
 * The component library: mine, shared, unsaved (§13).
 *
 * A component says how to model a part the analyzer would otherwise treat as bare copper. It
 * spans every board, which is exactly why saving one needs an account — there is nothing to
 * file it under otherwise.
 *
 * A signed-out visitor can still build and use one. It works immediately and is sent inline
 * with a run; it just lives in `sessionStorage` and is gone when the tab closes. The banner
 * says so before they start typing rather than after they lose it.
 */

import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ActionIcon, Alert, Badge, Button, Card, Group, NumberInput, Select, Stack, Switch, Text,
  TextInput, Title, Tooltip,
} from '@mantine/core'
import type { EmiApi, StoredComponent } from '../lib/emiApi'
import {
  ComponentError, describeProvenance, parseComponent, type SeriesRLC,
} from '../lib/componentDocument'
import { ComponentImpedancePreview } from './ComponentImpedancePreview'
import { addUnsaved, readUnsaved, removeUnsaved } from '../lib/unsavedComponents'
import type { EmiDeployment } from '../routes'

interface Props {
  api: EmiApi
  /** From `me.anonymous`, which the header already reads. */
  signedIn: boolean
  /** Where this copy runs. Locally there is one user and nobody to share a model with. */
  deployment?: EmiDeployment
  /** Solve frequencies, marked on the preview so it is visible where the part still works. */
  frequencies?: number[]
}

const PACKAGES = ['0201', '0402', '0603', '0805', '1206', '1210']

interface FormState {
  name: string
  value: string
  pkg: string
  c_nf: number
  esl_nh: number
  esr_mohm: number
  eslIncludesMount: boolean
  source: string
  mpn: string
}

const EMPTY: FormState = {
  name: '', value: '100n', pkg: '0402',
  c_nf: 100, esl_nh: 0.6, esr_mohm: 20,
  eslIncludesMount: false, source: '', mpn: '',
}

/**
 * Build an emi-component document from the form.
 *
 * Exported and pure because of the units: the form works in nF, nH and mΩ, which is how a
 * datasheet quotes them, while the document is in farads, henries and ohms. A missing factor
 * would move the self-resonance by decades and still validate.
 */
export function buildComponentDocument(f: FormState): Record<string, unknown> {
  return {
    format: 'emi-component',
    version: 1,
    id: `user-${f.value || 'c'}-${f.pkg}`.toLowerCase().replace(/[^\w-]+/g, '-'),
    kind: 'capacitor',
    name: f.name.trim() || `${f.value} ${f.pkg}`,
    provenance: f.source.trim() ? 'vendor' : 'user',
    match: { value: f.value.trim() || null, package: f.pkg, mpn: f.mpn.trim() || null },
    model: {
      type: 'series_rlc',
      c_f: f.c_nf * 1e-9,
      esl_h: f.esl_nh * 1e-9,
      esr_ohm: f.esr_mohm * 1e-3,
    },
    esl_includes_mount: f.eslIncludesMount,
    sources: f.source.trim()
      ? [{ doc: f.source.trim(), rev: '', what: 'ESL and ESR' }]
      : [{ doc: 'entered by hand', rev: '', what: 'ESL and ESR' }],
  }
}

export function ComponentsPanel({ api, signedIn, deployment = 'hosted', frequencies = [] }: Props) {
  const local = deployment === 'local'
  const qc = useQueryClient()
  const [adding, setAdding] = useState(false)
  const [form, setForm] = useState<FormState>(EMPTY)
  const [unsaved, setUnsaved] = useState<Record<string, unknown>[]>([])

  useEffect(() => setUnsaved(readUnsaved()), [])

  const saved = useQuery({
    queryKey: ['emi', 'components'],
    queryFn: () => api.listComponents(),
    enabled: signedIn,
  })

  const document = useMemo(() => buildComponentDocument(form), [form])

  const parsed = useMemo(() => {
    try {
      return { component: parseComponent(document), error: null as string | null }
    } catch (e) {
      return {
        component: null,
        error: e instanceof ComponentError ? e.message : String((e as Error).message),
      }
    }
  }, [document])

  const rlc: SeriesRLC = {
    c_f: form.c_nf * 1e-9,
    esl_h: form.esl_nh * 1e-9,
    esr_ohm: form.esr_mohm * 1e-3,
    esl_includes_mount: form.eslIncludesMount,
  }

  const create = useMutation({
    mutationFn: () => api.createComponent({
      kind: 'capacitor',
      name: (document.name as string),
      match: document.match,
      model: document.model,
      sources: document.sources,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['emi', 'components'] })
      setAdding(false)
      setForm(EMPTY)
    },
  })

  const share = useMutation({
    mutationFn: (v: { id: string; shared: boolean }) => api.shareComponent(v.id, v.shared),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['emi', 'components'] }),
  })

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteComponent(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['emi', 'components'] }),
  })

  const keepUnsaved = () => {
    setUnsaved(addUnsaved({ ...document, id: `${document.id}-${Date.now()}` }))
    setAdding(false)
    setForm(EMPTY)
  }

  const set = <K extends keyof FormState>(k: K) => (v: FormState[K]) =>
    setForm((p) => ({ ...p, [k]: v }))

  return (
    <Stack gap="sm">
      <Group justify="space-between" align="center">
        <Stack gap={0}>
          <Title order={5}>Components</Title>
          <Text size="xs" c="dimmed">
            Models for parts that would otherwise be bare copper. A component spans every
            board, not just this one.
          </Text>
        </Stack>
        {!adding && (
          <Button size="xs" variant="light" onClick={() => setAdding(true)}>
            New component
          </Button>
        )}
      </Group>

      {!signedIn && (
        <Alert color="yellow" variant="light" title="These components are not saved">
          <Text size="xs">
            You can build a component and use it right away — it is sent with the run like any
            other. It will not be kept: a component is a library entry that outlives a session,
            and there is no account here to file it under. Sign in to keep them.
          </Text>
        </Alert>
      )}

      {signedIn && saved.isLoading && <Text size="sm" c="dimmed">Loading…</Text>}
      {signedIn && saved.error && (
        <Alert color="red" variant="light">{(saved.error as Error).message}</Alert>
      )}

      {signedIn && saved.data?.map((c: StoredComponent) => (
        <Card key={c.id} withBorder padding="xs">
          <Group justify="space-between" wrap="nowrap">
            <Stack gap={2}>
              <Group gap="xs">
                <Text size="sm" fw={600}>{c.name}</Text>
                <Badge size="xs" variant="light">{c.kind}</Badge>
                {c.shared && <Badge size="xs" variant="light" color="blue">shared</Badge>}
              </Group>
              <Text size="10px" c="dimmed" ff="monospace">v{c.version}</Text>
            </Stack>
            <Group gap="xs" wrap="nowrap">
              <Tooltip
                label={local
                  ? 'Marks the model as shared. On this computer you are the only user, so it changes nothing.'
                  : 'Members of your organisation can use it, read-only'}
                withArrow
              >
                <Switch
                  size="xs"
                  label="Share"
                  checked={c.shared}
                  onChange={(e) => share.mutate({ id: c.id, shared: e.currentTarget.checked })}
                />
              </Tooltip>
              <ActionIcon
                variant="subtle" color="red" size="sm"
                aria-label={`Delete ${c.name}`}
                loading={remove.isPending && remove.variables === c.id}
                onClick={() => remove.mutate(c.id)}
              >
                ×
              </ActionIcon>
            </Group>
          </Group>
        </Card>
      ))}

      {unsaved.map((c) => (
        <Card key={String(c.id)} withBorder padding="xs" bg="var(--mantine-color-yellow-0)">
          <Group justify="space-between" wrap="nowrap">
            <Group gap="xs">
              <Text size="sm" fw={600}>{String(c.name)}</Text>
              <Badge size="xs" variant="light" color="yellow">this tab only</Badge>
            </Group>
            <ActionIcon
              variant="subtle" color="red" size="sm"
              aria-label={`Discard ${String(c.name)}`}
              onClick={() => setUnsaved(removeUnsaved(String(c.id)))}
            >
              ×
            </ActionIcon>
          </Group>
        </Card>
      ))}

      {signedIn && saved.data?.length === 0 && unsaved.length === 0 && !adding && (
        <Text size="sm" c="dimmed">
          No components yet. The built-in library still applies — these are for parts it does
          not cover, or where you know better than a class average.
        </Text>
      )}

      {adding && (
        <Card withBorder padding="sm">
          <Stack gap="sm">
            <Group gap="xs" align="flex-end">
              <TextInput label="Name" size="xs" w={200} placeholder="100 nF 0402 X7R"
                         value={form.name}
                         onChange={(e) => set('name')(e.currentTarget.value)} />
              <TextInput label="Value" size="xs" w={90} value={form.value}
                         onChange={(e) => set('value')(e.currentTarget.value)} />
              <Select label="Package" size="xs" w={100} data={PACKAGES} value={form.pkg}
                      allowDeselect={false}
                      onChange={(v) => set('pkg')(v ?? '0402')} />
              <TextInput label="MPN (optional)" size="xs" w={160} value={form.mpn}
                         onChange={(e) => set('mpn')(e.currentTarget.value)} />
            </Group>

            <Group gap="xs" align="flex-end">
              <NumberInput label="C" size="xs" w={110} step={10} value={form.c_nf}
                           onChange={(v) => set('c_nf')(typeof v === 'number' ? v : 0)}
                           rightSection={<Text size="10px" c="dimmed" pr={6}>nF</Text>}
                           rightSectionWidth={32} />
              <NumberInput label="ESL" size="xs" w={110} step={0.1} decimalScale={3}
                           value={form.esl_nh}
                           onChange={(v) => set('esl_nh')(typeof v === 'number' ? v : 0)}
                           rightSection={<Text size="10px" c="dimmed" pr={6}>nH</Text>}
                           rightSectionWidth={32} />
              <NumberInput label="ESR" size="xs" w={110} step={5} value={form.esr_mohm}
                           onChange={(v) => set('esr_mohm')(typeof v === 'number' ? v : 0)}
                           rightSection={<Text size="10px" c="dimmed" pr={6}>mΩ</Text>}
                           rightSectionWidth={32} />
              <TextInput label="Source" size="xs" w={220}
                         placeholder="part datasheet rev C"
                         value={form.source}
                         onChange={(e) => set('source')(e.currentTarget.value)} />
            </Group>

            <Switch
              size="xs"
              label="This ESL already includes the mounting loop"
              checked={form.eslIncludesMount}
              onChange={(e) => set('eslIncludesMount')(e.currentTarget.checked)}
              description="Leave off for a datasheet figure. The solve models pads, vias and planes itself, and counting the loop twice puts every resonance low."
            />

            {parsed.error ? (
              <Alert color="orange" variant="light" title="Not a usable component">
                <Text size="xs">{parsed.error}</Text>
              </Alert>
            ) : (
              <>
                <ComponentImpedancePreview rlc={rlc} marks={frequencies} />
                {parsed.component && (
                  <Text size="xs" c="dimmed">{describeProvenance(parsed.component)}</Text>
                )}
              </>
            )}

            {create.error && (
              <Alert color="red" variant="light">{(create.error as Error).message}</Alert>
            )}

            <Group gap="xs">
              {signedIn ? (
                <Button size="xs" disabled={!parsed.component} loading={create.isPending}
                        onClick={() => create.mutate()}>
                  {local ? 'Save to the library' : 'Save to my library'}
                </Button>
              ) : (
                <Button size="xs" disabled={!parsed.component} onClick={keepUnsaved}>
                  Use in this tab
                </Button>
              )}
              <Button size="xs" variant="subtle" onClick={() => setAdding(false)}>Cancel</Button>
            </Group>
          </Stack>
        </Card>
      )}
    </Stack>
  )
}
