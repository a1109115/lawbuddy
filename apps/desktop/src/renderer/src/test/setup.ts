import '@testing-library/jest-dom/vitest'
import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'

afterEach(cleanup)

// jsdom 的 Crypto 未实现 randomUUID（NewCaseDialog 依赖），补齐确定性实现
if (typeof crypto.randomUUID !== 'function') {
  Object.defineProperty(crypto, 'randomUUID', {
    value: (): string => '00000000-0000-4000-8000-000000000001',
    configurable: true
  })
}
