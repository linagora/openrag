import React, { ReactNode } from 'react'

import CozyTheme from 'cozy-ui-plus/dist/providers/CozyTheme'
import { BreakpointsProvider } from 'cozy-ui/transpiled/react/providers/Breakpoints'
import { I18n } from 'twake-i18n'
import { locales } from 'cozy-search/ai-chat-ui'

import '../styles/index.css'

// Use the browser locale (cozy-search ships en/fr/ru/vi); fall back to en.
const detectLang = (): string => {
  const l = (typeof navigator !== 'undefined' && navigator.language) || 'en'
  const short = l.slice(0, 2)
  return short in locales ? short : 'en'
}

// App-specific strings not provided by cozy-search, under an `openrag`
// namespace so they merge alongside cozy-search's dictionary without collision.
// `%{page}` is the polyglot interpolation token cozy-ui i18n uses.
const APP_LOCALES: Record<string, { openrag: { sources: { page: string } } }> = {
  en: { openrag: { sources: { page: 'Page %{page}' } } },
  fr: { openrag: { sources: { page: 'Page %{page}' } } }
}

const AppProviders = ({ children }: { children: ReactNode }): JSX.Element => (
  <I18n
    lang={detectLang()}
    dictRequire={(lang: string) => ({
      ...(locales[lang as keyof typeof locales] || locales.en),
      ...(APP_LOCALES[lang] || APP_LOCALES.en)
    })}
  >
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
