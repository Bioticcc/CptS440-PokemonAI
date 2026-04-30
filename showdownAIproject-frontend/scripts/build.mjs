import { build } from 'esbuild';
import { mkdir, rm } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const frontendRoot = path.resolve(__dirname, '..');
const distDir = path.join(frontendRoot, 'dist');
const appJsPath = path.join(distDir, 'app.js');

const backendOrigin = (process.env.PSAI_BACKEND_ORIGIN || 'http://127.0.0.1:8000').trim();

await rm(distDir, { recursive: true, force: true });
await mkdir(distDir, { recursive: true });

await build({
  entryPoints: [path.join(frontendRoot, 'src', 'main.jsx')],
  bundle: true,
  format: 'esm',
  jsx: 'automatic',
  outfile: appJsPath,
  minify: true,
  sourcemap: false,
  loader: {
    '.jpg': 'file',
    '.jpeg': 'file',
  },
  define: {
    __PSAI_BACKEND_ORIGIN__: JSON.stringify(backendOrigin),
  },
});

console.log(`Built desktop UI bundle at ${distDir}`);
