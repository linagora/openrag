import React, { useMemo, useState } from 'react'

import {
  FileTypeAudio,
  FileTypeFiles,
  FileTypeImage,
  FileTypePdf,
  FileTypeSheet,
  FileTypeSlide,
  FileTypeText,
  FileTypeVideo,
  FileTypeZip,
  Globe
} from '@linagora/twake-icons'
import IconRaw from 'cozy-ui/transpiled/react/Icon'
import MultiFilesIcon from 'cozy-ui/transpiled/react/Icons/MultiFiles'
import ListRaw from 'cozy-ui/transpiled/react/List'
import ListItemRaw from 'cozy-ui/transpiled/react/ListItem'
import ListItemIconRaw from 'cozy-ui/transpiled/react/ListItemIcon'
import ListItemTextRaw from 'cozy-ui/transpiled/react/ListItemText'
import { useI18n } from 'twake-i18n'
import type { StoredSource } from 'cozy-search'

/* eslint-disable @typescript-eslint/no-explicit-any */
const Icon = IconRaw as any
const List = ListRaw as any
const ListItem = ListItemRaw as any
const ListItemIcon = ListItemIconRaw as any
const ListItemText = ListItemTextRaw as any
/* eslint-enable @typescript-eslint/no-explicit-any */

interface OpenRagSource extends StoredSource {
  fileUrl?: string
  chunkUrl?: string
  path?: string
}

const EXT_ICON: Record<string, unknown> = {
  pdf: FileTypePdf,
  ppt: FileTypeSlide,
  pptx: FileTypeSlide,
  key: FileTypeSlide,
  doc: FileTypeText,
  docx: FileTypeText,
  odt: FileTypeText,
  rtf: FileTypeText,
  txt: FileTypeText,
  md: FileTypeText,
  xls: FileTypeSheet,
  xlsx: FileTypeSheet,
  csv: FileTypeSheet,
  ods: FileTypeSheet,
  png: FileTypeImage,
  jpg: FileTypeImage,
  jpeg: FileTypeImage,
  gif: FileTypeImage,
  svg: FileTypeImage,
  webp: FileTypeImage,
  mp4: FileTypeVideo,
  mov: FileTypeVideo,
  avi: FileTypeVideo,
  webm: FileTypeVideo,
  mp3: FileTypeAudio,
  wav: FileTypeAudio,
  zip: FileTypeZip,
  gz: FileTypeZip,
  tar: FileTypeZip
}

const basename = (p: string): string => p.split('/').pop() || p

const fileIcon = (name: string): unknown => {
  const ext = name.split('.').pop()?.toLowerCase() || ''
  return EXT_ICON[ext] || FileTypeFiles
}

// Directory part of an openRAG source path, minus the server storage prefix.
const dirOf = (path: string): string => {
  const dir = path.replace(/\/[^/]+$/, '')
  return dir.replace(/^\/app\/data/, '') // strip the internal storage root
}

interface DisplaySource {
  key: string
  href?: string
  name: string
  meta: string
  icon: unknown
}

const toDisplay = (sources: StoredSource[]): DisplaySource[] => {
  const out: DisplaySource[] = []
  const seen = new Set<string>()
  for (const s of sources as OpenRagSource[]) {
    if (s.sourceType === 'web') {
      const key = `web:${s.url}`
      if (seen.has(key)) continue
      seen.add(key)
      out.push({
        key,
        href: s.url,
        name: s.title || s.url || 'source',
        meta: s.url || '',
        icon: Globe
      })
      continue
    }
    // document: dedupe by file (same file split into many chunks)
    const fileKey = s.path || s.fileUrl || s.title || ''
    if (seen.has(`doc:${fileKey}`)) continue
    seen.add(`doc:${fileKey}`)
    const name = s.title || (s.path ? basename(s.path) : 'document')
    out.push({
      key: `doc:${fileKey}`,
      href: s.fileUrl || s.chunkUrl || s.url,
      name,
      meta: s.path ? dirOf(s.path) : '',
      icon: fileIcon(name)
    })
  }
  return out
}

const OpenRagSources = ({
  sources
}: {
  messageId: string
  sources: StoredSource[]
}): JSX.Element | null => {
  const [open, setOpen] = useState(false)
  const { t } = useI18n()
  const tc = t as unknown as (key: string, count?: number) => string

  const items = useMemo(() => toDisplay(sources), [sources])
  if (!items.length) return null

  return (
    <div className="openrag-sources">
      <button
        type="button"
        className="openrag-sources-chip"
        aria-expanded={open}
        onClick={() => setOpen(v => !v)}
      >
        <Icon icon={MultiFilesIcon} size={16} />
        <span>{tc('assistant.sources', items.length)}</span>
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
      </button>
      {open && (
        <List dense={false} className="u-w-100 u-p-0 u-mt-half">
          {items.map(item => (
            <ListItem
              key={item.key}
              className="openrag-source-card"
              component={item.href ? 'a' : 'div'}
              href={item.href}
              target={item.href ? '_blank' : undefined}
              rel={item.href ? 'noopener noreferrer' : undefined}
              button={Boolean(item.href)}
            >
              <ListItemIcon>
                <Icon icon={item.icon} size={32} />
              </ListItemIcon>
              <ListItemText primary={item.name} secondary={item.meta} />
            </ListItem>
          ))}
        </List>
      )}
    </div>
  )
}

export default OpenRagSources
