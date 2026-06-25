import { useEffect, useState } from 'react'

import { apiFetch } from '../config'

interface ModelsResponse {
  data: { id: string }[]
}

export const useModels = (): { models: string[]; isLoading: boolean } => {
  const [models, setModels] = useState<string[]>([])
  const [isLoading, setLoading] = useState(true)

  useEffect(() => {
    let active = true
    apiFetch('/v1/models')
      .then(r => r.json() as Promise<ModelsResponse>)
      .then(json => {
        if (!active) return
        setModels((json.data || []).map(m => m.id))
        setLoading(false)
      })
      .catch(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [])

  return { models, isLoading }
}
