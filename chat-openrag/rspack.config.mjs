import { rspack } from '@rspack/core'

export default {
  entry: './src/main.tsx',
  output: { clean: true, publicPath: '/' },
  resolve: {
    extensions: ['.tsx', '.ts', '.jsx', '.js'],
    alias: {
      react: new URL('./node_modules/react', import.meta.url).pathname,
      'react-dom': new URL('./node_modules/react-dom', import.meta.url).pathname,
      // cozy-search's Sidebar/useConversation call react-router hooks; they must
      // share this app's single react-router-dom instance or useNavigate() can't
      // see the app's <Router> (duplicate-context error).
      'react-router-dom': new URL('./node_modules/react-router-dom', import.meta.url).pathname,
      'react-router': new URL('./node_modules/react-router', import.meta.url).pathname,
      '@assistant-ui/react': new URL('./node_modules/@assistant-ui/react', import.meta.url).pathname,
      'twake-i18n': new URL('./node_modules/twake-i18n', import.meta.url).pathname,
      'cozy-ui': new URL('./node_modules/cozy-ui', import.meta.url).pathname,
      'cozy-ui-plus': new URL('./node_modules/cozy-ui-plus', import.meta.url).pathname,
      // cozy-search is symlinked, so its bare requires for peers must be
      // pinned back to this app's installs.
      '@linagora/twake-icons': new URL(
        './node_modules/@linagora/twake-icons',
        import.meta.url
      ).pathname,
      'cozy-device-helper': new URL(
        './node_modules/cozy-device-helper',
        import.meta.url
      ).pathname
    },
    // Browser fallbacks for Node core modules pulled in by cozy-search's
    // transitive deps (vfile, replace-ext, mime-types).
    fallback: {
      path: new URL('./node_modules/path-browserify', import.meta.url).pathname,
      process: new URL('./node_modules/process/browser.js', import.meta.url).pathname
    }
  },
  module: {
    rules: [
      {
        test: /\.(t|j)sx?$/,
        exclude: /node_modules\/(?!cozy-search)/,
        use: { loader: 'babel-loader' }
      },
      {
        test: /\.styl$/,
        use: [
          rspack.CssExtractRspackPlugin.loader,
          { loader: 'css-loader', options: { modules: { namedExport: false, exportLocalsConvention: 'as-is' } } },
          {
            loader: 'stylus-loader',
            options: {
              // cozy-search's .styl files do `@require 'settings/...'` against
              // cozy-ui's stylus tree; without these search paths stylus fails
              // and the component CSS is silently dropped.
              stylusOptions: {
                paths: [
                  new URL('./node_modules/cozy-ui/stylus', import.meta.url)
                    .pathname,
                  new URL('./node_modules', import.meta.url).pathname
                ]
              }
            }
          }
        ]
      },
      { test: /\.css$/, use: [rspack.CssExtractRspackPlugin.loader, 'css-loader'] },
      { test: /\.(png|jpe?g|gif|svg|woff2?|eot|ttf)$/, type: 'asset' }
    ]
  },
  plugins: [
    new rspack.HtmlRspackPlugin({ template: './src/index.html' }),
    new rspack.CssExtractRspackPlugin({}),
    // cozy-ui / twake-i18n / cozy-client transitively reference the Node
    // `process` global, which the browser lacks. Provide a browser shim.
    new rspack.ProvidePlugin({
      process: new URL('./node_modules/process/browser.js', import.meta.url).pathname
    }),
    new rspack.DefinePlugin({
      'process.env.OPENRAG_BASE_URL': JSON.stringify(
        process.env.OPENRAG_BASE_URL || 'http://localhost:8080'
      ),
      'process.env.OPENRAG_TOKEN': JSON.stringify(process.env.OPENRAG_TOKEN || ''),
      'process.env.NODE_ENV': JSON.stringify(process.env.NODE_ENV || 'development')
    })
  ],
  devServer: { port: 3042, historyApiFallback: true, hot: true }
}
