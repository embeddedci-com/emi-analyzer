// Vite's `?raw` suffix: the file's text, bundled into the build.
declare module '*?raw' {
  const text: string
  export default text
}
