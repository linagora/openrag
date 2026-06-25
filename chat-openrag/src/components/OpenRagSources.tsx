import React, { useState } from 'react'

import type { StoredSource } from 'cozy-search'

interface OpenRagSource extends StoredSource {
  fileUrl?: string
  chunkUrl?: string
}

const hrefOf = (s: OpenRagSource): string | undefined =>
  s.sourceType === 'web' ? s.url : s.fileUrl || s.chunkUrl || s.url

const OpenRagSources = ({
  messageId,
  sources
}: {
  messageId: string
  sources: StoredSource[]
}): JSX.Element | null => {
  const [open, setOpen] = useState(false)
  if (!sources.length) return null

  return (
    <div className="u-mt-1-half">
      <button
        type="button"
        className="u-mb-1"
        onClick={() => setOpen(v => !v)}
      >
        {sources.length} sources
      </button>
      {open && (
        <ul>
          {(sources as OpenRagSource[]).map((s, i) => {
            const href = hrefOf(s)
            const text = s.title || s.url || s.snippet || 'source'
            return (
              <li key={`${messageId}-${i}`}>
                {href ? (
                  <a href={href} target="_blank" rel="noreferrer">
                    {text}
                  </a>
                ) : (
                  <span>{text}</span>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

export default OpenRagSources
