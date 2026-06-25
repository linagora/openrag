import { render, screen } from '@testing-library/react'
import React from 'react'

import AppProviders from './AppProviders'

it('renders children within theme + i18n providers', () => {
  render(
    <AppProviders>
      <div>inside</div>
    </AppProviders>
  )
  expect(screen.getByText('inside')).toBeInTheDocument()
})
