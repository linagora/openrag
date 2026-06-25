import React, { ReactNode } from 'react'

import CozyTheme from 'cozy-ui-plus/dist/providers/CozyTheme'
import { I18n } from 'twake-i18n'
import { locales } from 'cozy-search/decoupled'

import '../styles/index.css'

const AppProviders = ({ children }: { children: ReactNode }): JSX.Element => (
  <I18n lang="en" dictRequire={(lang: string) => locales[lang as keyof typeof locales] || locales.en}>
    <CozyTheme type="light">{children}</CozyTheme>
  </I18n>
)

export default AppProviders
