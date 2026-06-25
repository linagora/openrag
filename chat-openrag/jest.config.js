module.exports = {
  testEnvironment: '<rootDir>/jest.env.js',
  setupFilesAfterEnv: ['<rootDir>/jest.setup.ts'],
  transform: { '^.+\\.(t|j)sx?$': 'babel-jest' },
  transformIgnorePatterns: ['/node_modules/(?!cozy-search)'],
  moduleNameMapper: { '\\.(styl|css)$': '<rootDir>/jest.styleMock.js' }
}
