import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import type { Plugin } from 'vite';

function speechAssets(): Plugin {
  const require = createRequire(import.meta.url);
  const vad = dirname(require.resolve('@ricky0123/vad-web'));
  const ort = dirname(require.resolve('onnxruntime-web/wasm'));
  const assets = new Map([
    ['speech-vad/silero_vad_v5.onnx', [vad, 'application/octet-stream']],
    ['speech-vad/vad.worklet.bundle.min.js', [vad, 'text/javascript']],
    ['speech-vad/ort-wasm-simd-threaded.mjs', [ort, 'text/javascript']],
    ['speech-vad/ort-wasm-simd-threaded.wasm', [ort, 'application/wasm']],
  ]);
  const read = (name: string, folder: string) => readFileSync(join(folder, name.split('/').at(-1)!));

  return {
    name: 'speech-vad-assets',
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const path = request.url?.split('?')[0] ?? '';
        if (!path.startsWith(server.config.base)) return next();
        const name = path.slice(server.config.base.length);
        const asset = assets.get(name);
        if (!asset) return next();
        response.setHeader('Content-Type', asset[1]);
        response.end(read(name, asset[0]));
      });
    },
    generateBundle() {
      for (const [fileName, [folder]] of assets) {
        this.emitFile({ type: 'asset', fileName, source: read(fileName, folder) });
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), speechAssets()],
  server: { port: 5173, strictPort: true, proxy: { '/api': { target: process.env.BUS_API_TARGET ?? 'http://127.0.0.1:8000', changeOrigin: true } } },
  test: { environment: 'jsdom', restoreMocks: true, clearMocks: true },
});
