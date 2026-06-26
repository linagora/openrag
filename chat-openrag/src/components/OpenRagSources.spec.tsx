import { render, screen } from '@testing-library/react'
import React from 'react'

jest.mock('twake-i18n', () => ({
  useI18n: () => ({
    t: (key: string, count?: number) =>
      key === 'assistant.sources' ? `${count} sources` : key
  })
}))

import OpenRagSources from './OpenRagSources'

const doc = (path: string, chunk: string): unknown => ({
  sourceType: 'document',
  path,
  fileUrl: `http://x/static${path}`,
  chunkUrl: `http://x/extract/${chunk}`,
  title: path.split('/').pop()
})

it('dedupes multiple chunks of the same file into a single source', () => {
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
  // The chip count reflects distinct files, not chunks.
  expect(screen.getByText('1 sources')).toBeInTheDocument()
})

it('counts distinct files', () => {
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
