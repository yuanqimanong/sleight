// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, APIError, endpoint, memory } from '../src/api'

beforeEach(() => { window.history.replaceState({}, '', '/sleight/') })
describe('subpath and error handling', () => {
  it('uses the proxy prefix for assets, SSE and management requests', () => {
    expect(new URL(endpoint('/api/jobs/123/events')).pathname).toBe('/sleight/api/jobs/123/events')
    expect(new URL(endpoint('viewer/prod/default/')).pathname).toBe('/sleight/viewer/prod/default/')
  })
  it('keeps credentials in cookies and serializes the operation body', async () => {
    const request = vi.fn().mockResolvedValue({ok:true,json:async()=>({ok:true})})
    vi.stubGlobal('fetch', request)
    await api('api/hosts','POST',{name:'prod'})
    expect(request.mock.calls[0][1].credentials).toBe('same-origin')
    expect(request.mock.calls[0][1].body).toBe('{"name":"prod"}')
    expect(request.mock.calls[0][0]).not.toContain('token=')
  })
  it('preserves status and the actionable server error', async () => {
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:false,status:409,json:async()=>({detail:'capacity reached'})}))
    await expect(api('api/start')).rejects.toEqual(new APIError(409,'capacity reached'))
    expect(memory(1024*1024*13)).toBe('13 MB')
  })
})
