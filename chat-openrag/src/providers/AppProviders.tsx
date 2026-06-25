import React, { ReactNode } from 'react'

import CozyTheme from 'cozy-ui-plus/dist/providers/CozyTheme'
import { BreakpointsProvider } from 'cozy-ui/transpiled/react/providers/Breakpoints'
import { I18n } from 'twake-i18n'
import { locales } from 'cozy-search/decoupled'

import '../styles/index.css'

const AppProviders = ({ children }: { children: ReactNode }): JSX.Element => (
  <I18n lang="en" dictRequire={(lang: string) => locales[lang as keyof typeof locales] || locales.en}>
    {/* ignoreCozySettings bypasses the cozy-stack settings query
        (CozyThemeWithQuery needs a CozyClient/stack we don't have) and uses
        cozy-ui's built-in light palette directly. */}
    <CozyTheme type="light" ignoreCozySettings>
      {/* cozy-ui components (Composer, etc.) call useBreakpoints, which
          requires a BreakpointsProvider ancestor (auto-provided inside a real
          Cozy app, so we supply it here for standalone). */}
      <BreakpointsProvider>{children}</BreakpointsProvider>
    </CozyTheme>
  </I18n>
)

export default AppProviders
