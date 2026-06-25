import React, { createContext, useContext, useEffect, useMemo, useState } from 'react'

import { useModels } from '../openrag/useModels'

interface ModelContextValue {
  model: string
  setModel: (id: string) => void
  models: string[]
}

const ModelContext = createContext<ModelContextValue | null>(null)

export const ModelProvider = ({
  children
}: {
  children: React.ReactNode
}): JSX.Element => {
  const { models } = useModels()
  const [model, setModel] = useState('')

  // Default to the first returned partition once models load (spec: no
  // hardcoded default).
  useEffect(() => {
    if (!model && models.length) setModel(models[0])
  }, [model, models])

  const value = useMemo(() => ({ model, setModel, models }), [model, models])
  return <ModelContext.Provider value={value}>{children}</ModelContext.Provider>
}

export const useModel = (): ModelContextValue => {
  const ctx = useContext(ModelContext)
  if (!ctx) throw new Error('useModel must be used within a ModelProvider')
  return ctx
}
