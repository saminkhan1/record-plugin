import { cp, mkdir, readFile, rm } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const source = resolve(root, 'plugin.js')
const outputDir = resolve(root, 'dist', 'record')
const output = resolve(outputDir, 'plugin.js')
const allowedImports = new Set(['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'])
const text = await readFile(source, 'utf8')
const imports = [
  ...text.matchAll(/(?:from\s*|import\s*\(\s*|import\s+)(['"])([^'"]+)\1/g)
].map(match => match[2])
const unsupported = [...new Set(imports.filter(specifier => !allowedImports.has(specifier)))]

if (unsupported.length) {
  throw new Error(`Unsupported desktop plugin import(s): ${unsupported.join(', ')}`)
}

if (!text.includes("const ID = 'record'") || !text.includes('export default')) {
  throw new Error('plugin.js must export the record desktop plugin')
}

await rm(resolve(root, 'dist'), { force: true, recursive: true })
await mkdir(outputDir, { recursive: true })
await cp(source, output)

console.log(`Built ${output}`)
