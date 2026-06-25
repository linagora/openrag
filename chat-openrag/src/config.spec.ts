import { apiFetch, getConfig } from './config'

describe('config', () => {
  it('reads base URL and token', () => {
    const cfg = getConfig()
    expect(typeof cfg.baseURL).toBe('string')
    expect(typeof cfg.token).toBe('string')
  })

  it('apiFetch prefixes baseURL and sets bearer auth', async () => {
    const spy = jest
      .spyOn(global, 'fetch')
      .mockResolvedValue(new Response('ok'))
    await apiFetch('/v1/models')
    const [url, init] = spy.mock.calls[0]
    expect(String(url)).toMatch(/\/v1\/models$/)
    expect((init?.headers as Record<string, string>).Authorization).toMatch(/^Bearer /)
    spy.mockRestore()
  })
})
