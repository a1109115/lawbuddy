import type { JSX } from 'react'
import { getBridge } from '../services/bridge'
import { useApp } from '../state/store'
import { Icon } from './Icon'

/** 顶部 38px 窗口控制条（最小化 / 最大化 / 关闭） */
export function TitleBar(): JSX.Element {
  const toast = useApp((s) => s.toast)
  const bridge = getBridge()

  return (
    <div className="titlebar">
      <div className="win-btns">
        <button
          className="win-btn"
          aria-label="最小化"
          onClick={() => (bridge ? bridge.window.minimize() : toast('最小化窗口（演示）'))}
        >
          <Icon id="i-minus" />
        </button>
        <button
          className="win-btn"
          aria-label="最大化"
          onClick={() => (bridge ? bridge.window.toggleMaximize() : toast('最大化 / 还原窗口（演示）'))}
        >
          <Icon id="i-square" />
        </button>
        <button
          className="win-btn close"
          aria-label="关闭"
          onClick={() => (bridge ? bridge.window.close() : toast('退出应用（演示）'))}
        >
          <Icon id="i-x" />
        </button>
      </div>
    </div>
  )
}
