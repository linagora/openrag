import { rspack } from '@rspack/core'

export default {
  entry: './src/main.tsx',
  output: { clean: true, publicPath: '/' },
  resolve: { extensions: ['.tsx', '.ts', '.jsx', '.js'] },
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
          'stylus-loader'
        ]
      },
      { test: /\.css$/, use: [rspack.CssExtractRspackPlugin.loader, 'css-loader'] },
      { test: /\.(png|jpe?g|gif|svg|woff2?|eot|ttf)$/, type: 'asset' }
    ]
  },
  plugins: [
    new rspack.HtmlRspackPlugin({ template: './src/index.html' }),
    new rspack.CssExtractRspackPlugin({}),
    new rspack.DefinePlugin({
      'process.env.OPENRAG_BASE_URL': JSON.stringify(
        process.env.OPENRAG_BASE_URL || 'http://localhost:8080'
      ),
      'process.env.OPENRAG_TOKEN': JSON.stringify(process.env.OPENRAG_TOKEN || '')
    })
  ],
  devServer: { port: 3042, historyApiFallback: true, hot: true },
  // cozy-search & cozy-ui already bundle React; dedupe to one copy
  optimization: { providedExports: true }
}
