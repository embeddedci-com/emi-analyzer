import { describe, expect, it } from 'vitest'
import { ApiError, EmiApi, downloadErrorMessage, fetchOk } from './emiApi'

/**
 * A fetch that answers the API's artifact-URL calls with a presigned URL, and the presigned
 * URL itself with whatever `blob` says. Records every URL it was asked for.
 */
function fakeFetch(blob: (url: string) => Response, artifactUrl?: (path: string) => Response) {
  const seen: string[] = []
  const impl = (async (input: RequestInfo | URL) => {
    const url = String(input)
    seen.push(url)
    if (url.startsWith('/api/')) {
      if (artifactUrl) return artifactUrl(url)
      const name = url.split('/').pop()
      return Response.json({ url: `https://storage.test/${name}`, content_type: '', size_bytes: 0 })
    }
    return blob(url)
  }) as typeof fetch
  return { impl, seen }
}

describe('fetchOk', () => {
  it('returns the response on a 2xx', async () => {
    const { impl } = fakeFetch(() => new Response('{}', { status: 200 }))
    const res = await fetchOk(impl, 'https://storage.test/x', 'x')
    expect(res.status).toBe(200)
  })

  it('throws an ApiError carrying the status on anything else', async () => {
    const { impl } = fakeFetch(() => new Response('<Error>AccessDenied</Error>', { status: 403 }))
    const err = await fetchOk(impl, 'https://storage.test/x', 'board.json').catch((e) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(403)
    expect(err.message).toBe(downloadErrorMessage('board.json', 403))
  })
})

describe('downloadErrorMessage', () => {
  it('says what to do about an expired link, a missing object and anything else', () => {
    expect(downloadErrorMessage('board.json', 403)).toMatch(/expired.*Reload/)
    expect(downloadErrorMessage('board.json', 404)).toMatch(/missing/)
    expect(downloadErrorMessage('board.json', 500)).toMatch(/HTTP 500/)
  })
})

describe('EmiApi artifact downloads', () => {
  it('goes through the injected fetch, not the global one', async () => {
    const { impl, seen } = fakeFetch(() => Response.json({ ok: true }))
    const api = new EmiApi({ fetchImpl: impl })
    expect(await api.artifactJson('r1', 'manifest.json')).toEqual({ ok: true })
    expect(seen).toEqual(['/api/emi/runs/r1/artifacts/manifest.json', 'https://storage.test/manifest.json'])
  })

  it('refuses to parse an error page as the board', async () => {
    const { impl } = fakeFetch(() => new Response('<Error/>', { status: 403 }))
    const api = new EmiApi({ fetchImpl: impl })
    await expect(api.fetchBoard('r1')).rejects.toMatchObject({ status: 403 })
  })

  it('treats a missing rules.json as no findings, from the API or from storage', async () => {
    const fromStorage = fakeFetch(() => new Response('', { status: 404 }))
    expect(await new EmiApi({ fetchImpl: fromStorage.impl }).fetchRules('r1')).toBeNull()

    const fromApi = fakeFetch(
      () => Response.json({}),
      () => Response.json({ error: 'no such artifact' }, { status: 404 }),
    )
    expect(await new EmiApi({ fetchImpl: fromApi.impl }).fetchRules('r1')).toBeNull()
  })

  it('still fails on rules.json when the link has expired', async () => {
    const { impl } = fakeFetch(() => new Response('', { status: 403 }))
    await expect(new EmiApi({ fetchImpl: impl }).fetchRules('r1')).rejects.toBeInstanceOf(ApiError)
  })
})
