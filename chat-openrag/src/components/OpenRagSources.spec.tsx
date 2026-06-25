import { fireEvent, render, screen } from '@testing-library/react'
import React from 'react'

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
