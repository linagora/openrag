export interface AppConfig {
  baseURL: string
  token: string
}

export const getConfig = (): AppConfig => {
  // localStorage overrides allow per-browser dev tokens without rebuilding.
  const ls = typeof localStorage !== 'undefined' ? localStorage : null
  return {
    baseURL:
      ls?.getItem('openrag.baseURL') ||
      process.env.OPENRAG_BASE_URL ||
      'http://localhost:8080',
    token: ls?.getItem('openrag.token') || process.env.OPENRAG_TOKEN || ''
  }
}

export const apiFetch = (
  path: string,
  init: RequestInit = {}
): Promise<Response> => {
  const { baseURL, token } = getConfig()
  return fetch(`${baseURL}${path}`, {
    ...init,
    headers: {
      ...(init.headers || {}),
      Authorization: `Bearer ${token}`
    }
  })
}
