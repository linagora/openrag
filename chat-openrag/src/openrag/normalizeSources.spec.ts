import { normalizeSources } from './normalizeSources'

it('normalizes document and web sources from an extra JSON string', () => {
  const extra = JSON.stringify({
    sources: [
      {
        source_type: 'document',
        file_url: 'http://x/static/a.pdf',
        chunk_url: 'http://x/extract/c1',
        _id: 'c1',
        source: '/data/a.pdf',
        page: 3
      },
      {
        source_type: 'web',
        url: 'https://example.com',
        title: 'Example',
        snippet: 'hello'
      }
    ]
  })
  const out = normalizeSources(extra)
  expect(out).toEqual([
    {
      sourceType: 'document',
      title: 'a.pdf',
      fileUrl: 'http://x/static/a.pdf',
      chunkUrl: 'http://x/extract/c1',
      path: '/data/a.pdf',
      url: 'http://x/static/a.pdf',
      page: 3,
      chunkId: 'c1'
    },
    {
      sourceType: 'web',
      url: 'https://example.com',
      title: 'Example',
      snippet: 'hello'
    }
  ])
})

it('reads page from page_number when page is absent', () => {
  const extra = JSON.stringify({
    sources: [
      {
        source_type: 'document',
        chunk_url: 'http://x/extract/c2',
        _id: 'c2',
        source: '/data/b.docx',
        page_number: 7
      }
    ]
  })
  const [doc] = normalizeSources(extra)
  expect(doc.page).toBe(7)
  expect(doc.chunkId).toBe('c2')
})

it('returns [] for empty / malformed extra', () => {
  expect(normalizeSources('{}')).toEqual([])
  expect(normalizeSources('not json')).toEqual([])
  expect(normalizeSources(undefined)).toEqual([])
})
