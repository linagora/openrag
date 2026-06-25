import React from 'react'

import { useModel } from '../runtime/ModelContext'

const label = (id: string): string =>
  id === 'openrag-all' ? 'All partitions' : id.replace(/^openrag-/, '')

const OpenRagModelSelector = ({
  disabled
}: {
  disabled?: boolean
}): JSX.Element | null => {
  const { model, setModel, models } = useModel()
  if (!models.length) return null
  return (
    <select
      aria-label="Partition"
      className="u-ml-auto openrag-partition-select"
      disabled={disabled}
      value={model}
      onChange={e => setModel(e.target.value)}
    >
      {models.map(id => (
        <option key={id} value={id}>
          {label(id)}
        </option>
      ))}
    </select>
  )
}

export default OpenRagModelSelector
