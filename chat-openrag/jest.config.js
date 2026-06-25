module.exports = {
  testEnvironment: '<rootDir>/jest.env.js',
  setupFilesAfterEnv: ['<rootDir>/jest.setup.ts'],
  transform: { '^.+\\.(t|j)sx?$': 'babel-jest' },
  transformIgnorePatterns: ['/node_modules/(?!cozy-search)'],
  moduleNameMapper: {
    '^react$': '<rootDir>/node_modules/react',
    '^react-dom$': '<rootDir>/node_modules/react-dom',
    '^@assistant-ui/react$': '<rootDir>/jest.assistantUiMock.js',
    '^twake-i18n$': '<rootDir>/node_modules/twake-i18n',
    '^@linagora/twake-icons$': '<rootDir>/jest.twakeIconsMock.js',
    '^cozy-ui/(.*)$': '<rootDir>/jest.cozyUiMock.js',
    '^cozy-ui-plus/(.*)$': '<rootDir>/jest.cozyUiPlusMock.js',
    '^cozy-device-helper$': '<rootDir>/jest.cozyDeviceHelperMock.js',
    '\\.(styl|css)$': '<rootDir>/jest.styleMock.js'
  }
}
