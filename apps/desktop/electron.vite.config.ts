import { defineConfig, externalizeDepsPlugin } from 'electron-vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'

const contractsSrc = resolve(__dirname, '../../packages/contracts/src/index.ts')

/**
 * 生产构建注入严格 CSP；开发环境由 dev server header 放宽（HMR 需要内联脚本）。
 * blob: 用于原件预览：Core 内容会话组装 object URL（图片/Word 内嵌图）、pdf.js worker；
 * frame-src 的 lawbuddy-preview: 用于 Agent 生成 HTML 的沙箱预览 iframe（Main 自定义协议，自带宽松 CSP）；
 * media-src 的 lawbuddy-media: 用于音视频按 Range 流式预览（Main 自定义协议，仅服务已打开的内容会话）。
 */
const cspMeta =
  "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob: lawbuddy-media:; font-src 'self' data: blob:; connect-src 'self' blob:; worker-src 'self' blob:; frame-src 'self' lawbuddy-preview:; object-src 'none'; base-uri 'none'; form-action 'none'"

const prodCspPlugin = {
  name: 'inject-prod-csp',
  apply: 'build' as const,
  transformIndexHtml(html: string) {
    return html.replace(
      '<meta charset="UTF-8" />',
      `<meta charset="UTF-8" />\n    <meta http-equiv="Content-Security-Policy" content="${cspMeta}" />`
    )
  }
}

export default defineConfig({
  main: {
    plugins: [externalizeDepsPlugin({ exclude: ['@lawbuddy/contracts'] })],
    build: {
      outDir: 'out/main',
      rollupOptions: { input: resolve(__dirname, 'src/main/index.ts') }
    },
    resolve: { alias: { '@lawbuddy/contracts': contractsSrc } }
  },
  preload: {
    plugins: [externalizeDepsPlugin({ exclude: ['@lawbuddy/contracts'] })],
    build: {
      outDir: 'out/preload',
      rollupOptions: { input: resolve(__dirname, 'src/preload/index.ts') }
    },
    resolve: { alias: { '@lawbuddy/contracts': contractsSrc } }
  },
  renderer: {
    plugins: [react(), prodCspPlugin],
    root: resolve(__dirname, 'src/renderer'),
    publicDir: resolve(__dirname, 'src/renderer/public'),
    build: { outDir: 'out/renderer' },
    resolve: { alias: { '@lawbuddy/contracts': contractsSrc } },
    server: {
      // 固定端口并 strictPort：localStorage 按 origin（http://localhost:端口）隔离，
      // 端口漂移会导致历史记录与当前案件「丢失」；被占用时直接报错而非静默换端口
      port: 5173,
      strictPort: true,
      headers: {
        // 开发环境：react-refresh 需要内联脚本与 ws 连接，仅 dev 放宽
        'Content-Security-Policy':
          "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob: lawbuddy-media:; connect-src 'self' ws: http://localhost:* blob:; font-src 'self' data: blob:; worker-src 'self' blob:; frame-src 'self' lawbuddy-preview:"
      }
    }
  }
})
