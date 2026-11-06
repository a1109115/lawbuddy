import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

/** 渲染进程组件与状态测试（需求文档 12.2）：
 *  与 tsconfig.web.json 的 include / @lawbuddy/contracts paths 对齐 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@lawbuddy/contracts': fileURLToPath(new URL('../../packages/contracts/src/index.ts', import.meta.url))
    }
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/renderer/src/test/setup.ts'],
    include: ['src/renderer/src/**/*.test.{ts,tsx}']
  }
})
