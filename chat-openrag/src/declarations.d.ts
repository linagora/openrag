declare module '*.styl' {
  const classes: { [key: string]: string }
  export default classes
}
declare module '*.css'
declare module '*.svg'
declare module 'cozy-flags'
declare module 'cozy-search' {
  export interface StoredSource {
    id?: string
    doctype?: string
    sourceType?: string
    url?: string
    title?: string
    snippet?: string
  }
}
