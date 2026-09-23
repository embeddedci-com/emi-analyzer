import { describe, expect, it } from 'vitest'
import { renderToString } from 'react-dom/server'
import { MemoryRouter, Route, Routes } from 'react-router'
import { DEFAULT_HOST_COPY, EmiBase, resolveHostCopy, useEmiBase } from './host'

function ShowBase() {
  return <>{`[${useEmiBase()}]`}</>
}

/** The fragment's shape: a pathless EmiBase layout with the pages below it. */
const fragment = () => (
  <Route element={<EmiBase />}>
    <Route index element={<ShowBase />} />
    <Route path="limitations" element={<ShowBase />} />
    <Route path=":projectId" element={<ShowBase />} />
  </Route>
)

function baseAt(url: string, mount: 'nested' | 'splat', prefix = '/tools/emi') {
  const tree = mount === 'nested'
    ? <Routes><Route path={prefix}>{fragment()}</Route></Routes>
    // A host that mounts a splat route and renders its own <Routes> inside it.
    : <Routes><Route path={`${prefix}/*`} element={<Routes>{fragment()}</Routes>} /></Routes>
  return renderToString(<MemoryRouter initialEntries={[url]}>{tree}</MemoryRouter>)
}

describe('useEmiBase', () => {
  it('is the mount point on every page, however deep', () => {
    for (const mount of ['nested', 'splat'] as const) {
      expect(baseAt('/tools/emi', mount)).toContain('[/tools/emi]')
      expect(baseAt('/tools/emi/limitations', mount)).toContain('[/tools/emi]')
      expect(baseAt('/tools/emi/abc123', mount)).toContain('[/tools/emi]')
    }
  })

  it('follows a host that mounts somewhere else', () => {
    expect(baseAt('/emi/abc123', 'splat', '/emi')).toContain('[/emi]')
  })
})

describe('resolveHostCopy', () => {
  it('names no operator by default', () => {
    const copy = resolveHostCopy()
    expect(copy).toMatchObject(DEFAULT_HOST_COPY)
    expect(copy.pluginDocsUrl).toBeUndefined()
    expect(JSON.stringify(copy)).not.toMatch(/EmbeddedCI|DigitalOcean/i)
  })

  it('takes what the host supplies', () => {
    const copy = resolveHostCopy({ storage: 'Stored in our bucket.', pluginDocsUrl: '/docs/kicad' })
    expect(copy.storage).toBe('Stored in our bucket.')
    expect(copy.pluginDocsUrl).toBe('/docs/kicad')
    expect(copy.signIn).toBe(DEFAULT_HOST_COPY.signIn)
  })
})
