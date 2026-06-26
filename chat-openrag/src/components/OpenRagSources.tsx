import React, { useState } from 'react'

import { useI18n } from 'twake-i18n'
import type { StoredSource } from 'cozy-search'

interface OpenRagSource extends StoredSource {
  fileUrl?: string
  chunkUrl?: string
}

const hrefOf = (s: OpenRagSource): string | undefined =>
  s.sourceType === 'web' ? s.url : s.fileUrl || s.chunkUrl || s.url

const FilesIcon = (): JSX.Element => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <path
      d="M4 7a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7Z"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinejoin="round"
    />
  </svg>
)

const Chevron = ({ open }: { open: boolean }): JSX.Element => (
  <svg
    width="14"
    height="14"
    viewBox="0 0 24 24"
    fill="none"
    aria-hidden="true"
    style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }}
  >
    <path d="m9 6 6 6-6 6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
)

const OpenRagSources = ({
  messageId,
  sources
}: {
  messageId: string
  sources: StoredSource[]
}): JSX.Element | null => {
  const [open, setOpen] = useState(false)
  const { t } = useI18n()
  // twake-i18n types `t` as (key) => string, but polyglot accepts a smart_count
  // second arg for pluralization (e.g. "%{smart_count} source |||| ... sources").
  const tc = t as unknown as (key: string, count?: number) => string
  if (!sources.length) return null

  return (
    <div className="openrag-sources">
      <button
        type="button"
        className="openrag-sources-chip"
        aria-expanded={open}
        onClick={() => setOpen(v => !v)}
      >
        <FilesIcon />
        <span>{tc('assistant.sources', sources.length)}</span>
        <Chevron open={open} />
      </button>
      {open && (
        <ul className="openrag-sources-list">
          {(sources as OpenRagSource[]).map((s, i) => {
            const href = hrefOf(s)
            const text = s.title || s.url || s.snippet || 'source'
            return (
              <li key={`${messageId}-${i}`} className="openrag-source-item">
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
