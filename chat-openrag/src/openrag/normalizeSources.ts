import type { StoredSource } from 'cozy-search'

export interface NormalizedSource extends StoredSource {
  sourceType: 'web' | 'document'
  fileUrl?: string
  chunkUrl?: string
  /** Raw file path from openRAG (e.g. /app/data/foo.pdf) — for dedup + path. */
  path?: string
  /** openRAG chunk metadata to distinguish a Twake (Cozy) file from an
      openRAG-only upload, and to build a Twake link. */
  doctype?: string
  fileId?: string
  partition?: string
}

interface RawDoc {
  source_type?: string
  file_url?: string
  chunk_url?: string
  url?: string
  title?: string
  snippet?: string
  source?: string
  doctype?: string
  file_id?: string
  partition?: string
}

const basename = (p: string): string => p.split('/').pop() || p

export const normalizeSources = (raw: unknown): NormalizedSource[] => {
  let parsed: unknown = raw
  if (typeof raw === 'string') {
    try {
      parsed = JSON.parse(raw)
    } catch {
      return []
    }
  }
  const sources = (parsed as { sources?: RawDoc[] })?.sources
  if (!Array.isArray(sources)) return []

  return sources.map((s): NormalizedSource => {
    if (s.source_type === 'web') {
      return {
        sourceType: 'web',
        url: s.url,
        title: s.title,
        snippet: s.snippet
      }
    }
    const link = s.file_url || s.chunk_url || s.url
    return {
      sourceType: 'document',
      title: s.title || (s.source ? basename(s.source) : undefined),
      fileUrl: s.file_url,
      chunkUrl: s.chunk_url,
      path: s.source,
      doctype: s.doctype,
      fileId: s.file_id,
      partition: s.partition,
      url: link
    }
  })
}
