import { normalizeSources } from './normalizeSources'

it('normalizes document and web sources from an extra JSON string', () => {
  const extra = JSON.stringify({
    sources: [
      {
        source_type: 'document',
        file_url: 'http://x/static/a.pdf',
        chunk_url: 'http://x/extract/c1',
        _id: 'c1',
        source: '/data/a.pdf'
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
      url: 'http://x/static/a.pdf'
    },
    {
      sourceType: 'web',
      url: 'https://example.com',
      title: 'Example',
      snippet: 'hello'
    }
  ])
})

it('returns [] for empty / malformed extra', () => {
  expect(normalizeSources('{}')).toEqual([])
  expect(normalizeSources('not json')).toEqual([])
  expect(normalizeSources(undefined)).toEqual([])
})
