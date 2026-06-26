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
  Globe,
  Icon as TwakeIcon,
  MultiFiles,
  Right
} from '@linagora/twake-icons'
import BoxRaw from 'cozy-ui/transpiled/react/Box'
import ChipRaw from 'cozy-ui/transpiled/react/Chips'
import IconRaw from 'cozy-ui/transpiled/react/Icon'
import ListRaw from 'cozy-ui/transpiled/react/List'
import ListItemRaw from 'cozy-ui/transpiled/react/ListItem'
import ListItemIconRaw from 'cozy-ui/transpiled/react/ListItemIcon'
import ListItemTextRaw from 'cozy-ui/transpiled/react/ListItemText'
import { useI18n } from 'twake-i18n'
import type { StoredSource } from 'cozy-search'

import { apiFetch } from '../config'

/* eslint-disable @typescript-eslint/no-explicit-any */
const Box = BoxRaw as any
const Chip = ChipRaw as any
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
  doctype?: string
  fileId?: string
  partition?: string
  page?: number
  chunkId?: string
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

const extOf = (name: string): string => name.split('.').pop()?.toLowerCase() || ''

const fileIcon = (name: string): unknown => EXT_ICON[extOf(name)] || FileTypeFiles

// Formats whose chunks carry a meaningful page number (Marker/Docling paginate
// PDFs). Office/markdown/text come back as page 1, so the page is noise there.
const PAGINATED_EXTS = new Set(['pdf'])
const isPaginated = (name: string): boolean => PAGINATED_EXTS.has(extOf(name))

// Directory part of an openRAG source path, minus the server storage prefix.
const dirOf = (path: string): string =>
  path.replace(/\/[^/]+$/, '').replace(/^\/app\/data/, '')

type Kind = 'web' | 'doc'

interface DisplaySource {
  key: string
  kind: Kind
  href?: string // web link
  extractPath?: string // /extract/<id> for best-effort chunk content
  name: string
  /** Directory (documents) or URL (web) shown in the subtitle. */
  meta: string
  /** 1-indexed page for a document chunk (undefined for web / non-paginated). */
  page?: number
  icon: unknown
}

/**
 * Subtitle for a source row: "Page {page} · {dir}", degrading gracefully when
 * either part is missing (page-only, dir-only, or empty). `t` localizes the
 * page label.
 */
export const formatSubtitle = (
  t: (key: string, opts?: { page?: number }) => string,
  page: number | undefined,
  dir: string
): string => {
  const pageLabel = page != null ? t('openrag.sources.page', { page }) : ''
  return [pageLabel, dir].filter(Boolean).join(' · ')
}

const pathOf = (url: string | undefined): string | undefined => {
  if (!url) return undefined
  try {
    return new URL(url).pathname
  } catch {
    return url
  }
}

export const toDisplay = (sources: StoredSource[]): DisplaySource[] => {
  const out: DisplaySource[] = []
  const seen = new Set<string>()
  for (const s of sources as OpenRagSource[]) {
    if (s.sourceType === 'web') {
      const key = `web:${s.url}`
      if (seen.has(key)) continue
      seen.add(key)
      out.push({
        key,
        kind: 'web',
        href: s.url,
        name: s.title || s.url || 'source',
        meta: s.url || '',
        icon: Globe
      })
      continue
    }
    // One entry per chunk (no file-level dedup); only collapse exact repeats of
    // the same chunk. Twake-specific dedup + file links are deferred — every
    // document chunk falls back to the best-effort chunk-content view.
    const chunkKey = `doc:${s.chunkId || s.chunkUrl || s.path || s.title || ''}`
    if (seen.has(chunkKey)) continue
    seen.add(chunkKey)
    const name = s.title || (s.path ? basename(s.path) : 'document')
    out.push({
      key: chunkKey,
      kind: 'doc',
      href: undefined,
      extractPath: pathOf(s.chunkUrl),
      name,
      meta: s.path ? dirOf(s.path) : '',
      // Only paginated formats (PDF) carry a real page; suppress the noisy
      // "Page 1" that docx/md/txt always report.
      page: isPaginated(name) ? s.page : undefined,
      icon: fileIcon(name)
    })
  }
  return out
}

const SourceCard = ({ item }: { item: DisplaySource }): JSX.Element => {
  const [open, setOpen] = useState(false)
  const [content, setContent] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const { t } = useI18n()
  const subtitle = formatSubtitle(
    t as unknown as (key: string, opts?: { page?: number }) => string,
    item.page,
    item.meta
  )

  // Web sources are plain links.
  if (item.href) {
    return (
      <ListItem
        className="openrag-source-card"
        component="a"
        href={item.href}
        target="_blank"
        rel="noopener noreferrer"
        button
      >
        <ListItemIcon>
          <Icon icon={item.icon} size={32} />
        </ListItemIcon>
        <ListItemText primary={item.name} secondary={subtitle} />
      </ListItem>
    )
  }

  // openRAG-only document: best-effort — reveal the chunk content on click.
  const onToggle = async (): Promise<void> => {
    const next = !open
    setOpen(next)
    if (next && content === null && item.extractPath) {
      setLoading(true)
      try {
        const res = await apiFetch(item.extractPath)
        const data = (await res.json()) as { page_content?: string }
        setContent(data.page_content || '')
      } catch {
        setContent('')
      } finally {
        setLoading(false)
      }
    }
  }

  return (
    <div className="openrag-source-card openrag-source-card--expandable">
      <ListItem button onClick={onToggle}>
        <ListItemIcon>
          <Icon icon={item.icon} size={32} />
        </ListItemIcon>
        <ListItemText primary={item.name} secondary={subtitle} />
      </ListItem>
      {open && (
        <div className="openrag-source-extract">
          {loading ? '…' : content || '(extrait indisponible)'}
        </div>
      )}
    </div>
  )
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
    <Box className="openrag-sources u-mt-1-half">
      {/* cozy-ui Chip (same as cozy-search's native Sources) so the label picks
          up the MUI Chip typography rather than a hand-rolled button font. */}
      <Chip
        className="u-mb-1"
        icon={<TwakeIcon icon={MultiFiles} className="u-ml-half" />}
        label={tc('assistant.sources', items.length)}
        deleteIcon={
          <TwakeIcon className="u-h-1" icon={Right} rotate={open ? 90 : 0} />
        }
        clickable
        onClick={() => setOpen(v => !v)}
        onDelete={() => setOpen(v => !v)}
      />
      {open && (
        <List dense={false} className="u-w-100 u-p-0 u-mt-half">
          {items.map(item => (
            <SourceCard key={item.key} item={item} />
          ))}
        </List>
      )}
    </Box>
  )
}

export default OpenRagSources
