import { render, screen } from '@testing-library/react'
import React from 'react'

jest.mock('twake-i18n', () => ({
  useI18n: () => ({
    t: (key: string, opts?: number | { page?: number }) =>
      key === 'assistant.sources'
        ? `${opts as number} sources`
        : key === 'openrag.sources.page'
          ? `Page ${(opts as { page?: number }).page}`
          : key
  })
}))

import OpenRagSources, { formatSubtitle, toDisplay } from './OpenRagSources'

const doc = (path: string, chunk: string, page?: number): unknown => ({
  sourceType: 'document',
  path,
  chunkId: chunk,
  fileUrl: `http://x/static${path}`,
  chunkUrl: `http://x/extract/${chunk}`,
  title: path.split('/').pop(),
  page
})

it('renders one source per chunk (no file-level dedup)', () => {
  render(
    <OpenRagSources
      messageId="m"
      sources={
        [
          doc('/app/data/a.pdf', 'c1'),
          doc('/app/data/a.pdf', 'c2'),
          doc('/app/data/a.pdf', 'c3')
        ] as never
      }
    />
  )
  // Three chunks of the same file are three independent sources now.
  expect(screen.getByText('3 sources')).toBeInTheDocument()
})

it('dedupes only exact-duplicate chunks (same chunk id)', () => {
  render(
    <OpenRagSources
      messageId="m"
      sources={[doc('/app/data/a.pdf', 'c1'), doc('/app/data/a.pdf', 'c1')] as never}
    />
  )
  expect(screen.getByText('1 sources')).toBeInTheDocument()
})

it('counts distinct chunks across files', () => {
  render(
    <OpenRagSources
      messageId="m"
      sources={
        [doc('/app/data/a.pdf', 'c1'), doc('/app/data/b.docx', 'c2')] as never
      }
    />
  )
  expect(screen.getByText('2 sources')).toBeInTheDocument()
})

it('renders nothing when there are no sources', () => {
  const { container } = render(<OpenRagSources messageId="m" sources={[]} />)
  expect(container).toBeEmptyDOMElement()
})

describe('toDisplay page gating', () => {
  it('keeps the page for paginated formats (pdf)', () => {
    const [d] = toDisplay([doc('/app/data/a.pdf', 'c1', 3)] as never)
    expect(d.page).toBe(3)
  })

  it('drops the page for non-paginated formats (docx)', () => {
    const [d] = toDisplay([doc('/app/data/a.docx', 'c1', 1)] as never)
    expect(d.page).toBeUndefined()
  })

  it('drops the page for markdown / text', () => {
    expect(toDisplay([doc('/app/data/a.md', 'c1', 1)] as never)[0].page).toBeUndefined()
    expect(toDisplay([doc('/app/data/a.txt', 'c2', 1)] as never)[0].page).toBeUndefined()
  })
})

describe('formatSubtitle', () => {
  const t = (key: string, opts?: { page?: number }): string =>
    key === 'openrag.sources.page' ? `Page ${opts?.page}` : key

  it('combines page and directory', () => {
    expect(formatSubtitle(t, 3, '/sub')).toBe('Page 3 · /sub')
  })

  it('shows page alone when there is no directory', () => {
    expect(formatSubtitle(t, 3, '')).toBe('Page 3')
  })

  it('omits the Page prefix when page is undefined', () => {
    expect(formatSubtitle(t, undefined, '/sub')).toBe('/sub')
  })

  it('is empty when neither page nor directory is present', () => {
    expect(formatSubtitle(t, undefined, '')).toBe('')
  })
})
