import React, { useEffect, useRef, useState } from 'react'

import { useModel } from '../runtime/ModelContext'

const label = (id: string): string =>
  id === 'openrag-all' ? 'All partitions' : id.replace(/^openrag-/, '')

const SparkleIcon = (): JSX.Element => (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M12 2l1.6 4.8L18 8.4l-4.4 1.6L12 15l-1.6-5L6 8.4l4.4-1.6L12 2zM18 13l.9 2.6L21 16.5l-2.1.9L18 20l-.9-2.6L15 16.5l2.1-.9L18 13z" />
  </svg>
)

const Chevron = ({ open }: { open: boolean }): JSX.Element => (
  <svg
    width="12"
    height="12"
    viewBox="0 0 24 24"
    fill="none"
    aria-hidden="true"
    style={{ transform: open ? 'rotate(180deg)' : 'none', transition: 'transform .15s' }}
  >
    <path d="m6 9 6 6 6-6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
)

const OpenRagModelSelector = ({
  disabled
}: {
  disabled?: boolean
}): JSX.Element | null => {
  const { model, setModel, models } = useModel()
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent): void => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  if (!models.length) return null

  const filtered = query
    ? models.filter(id => label(id).toLowerCase().includes(query.toLowerCase()))
    : models

  return (
    <div className="openrag-selector u-ml-auto" ref={ref}>
      <button
        type="button"
        className="openrag-selector-chip"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen(v => !v)}
      >
        <SparkleIcon />
        <span className="openrag-selector-label">{label(model)}</span>
        <Chevron open={open} />
      </button>
      {open && !disabled && (
        <div className="openrag-selector-menu" role="listbox">
          <input
            className="openrag-selector-search"
            type="text"
            autoFocus
            placeholder="…"
            value={query}
            onChange={e => setQuery(e.target.value)}
          />
          <ul className="openrag-selector-options">
            {filtered.map(id => (
              <li key={id}>
                <button
                  type="button"
                  role="option"
                  aria-selected={id === model}
                  className={
                    'openrag-selector-option' +
                    (id === model ? ' is-selected' : '')
                  }
                  onClick={() => {
                    setModel(id)
                    setOpen(false)
                    setQuery('')
                  }}
                >
                  {label(id)}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

export default OpenRagModelSelector
