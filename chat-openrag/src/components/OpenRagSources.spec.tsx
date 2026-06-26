import { fireEvent, render, screen } from '@testing-library/react'
import React from 'react'

jest.mock('twake-i18n', () => ({
  useI18n: () => ({
    t: (key: string, count?: number) =>
      key === 'assistant.sources' ? `${count} sources` : key,
    lang: 'en'
  })
}))

import OpenRagSources from './OpenRagSources'

it('renders a collapsible sources list with links', () => {
  render(
    <OpenRagSources
      messageId="m1"
      sources={[
        { sourceType: 'document', title: 'a.pdf', fileUrl: 'http://x/a.pdf' } as never,
        { sourceType: 'web', title: 'Example', url: 'https://e.com' } as never
      ]}
    />
  )
  // count chip visible
  expect(screen.getByText(/2/)).toBeInTheDocument()
  // expand
  fireEvent.click(screen.getByText(/2/))
  const links = screen.getAllByRole('link')
  expect(links.map(a => a.getAttribute('href'))).toEqual([
    'http://x/a.pdf',
    'https://e.com'
  ])
})
