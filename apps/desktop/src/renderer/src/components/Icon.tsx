import type { JSX } from 'react'

/** 线性 SVG 图标库（24x24 stroke），与设计规范 §4 一致 */
const SPRITE: Record<string, JSX.Element> = {
  'i-plus': <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" fill="none" />,
  'i-upload': (
    <>
      <path d="M12 16V4m0 0L8 8m4-4 4 4" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" fill="none" />
      <path d="M4 15v3.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V15" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" fill="none" />
    </>
  ),
  'i-clock': (
    <>
      <circle cx="12" cy="12" r="8.2" stroke="currentColor" strokeWidth="1.7" fill="none" />
      <path d="M12 7.5V12l2.8 2.4" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" fill="none" />
    </>
  ),
  'i-book': (
    <path
      d="M2.5 4.5h6.8a3 3 0 0 1 2.7 1.7 3 3 0 0 1 2.7-1.7h6.8v13.4h-7.5a2.7 2.7 0 0 0-2 1.3 2.7 2.7 0 0 0-2-1.3H2.5Zm9.5 1.7v13"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinejoin="round"
      fill="none"
    />
  ),
  'i-user': (
    <>
      <circle cx="12" cy="8" r="3.6" stroke="currentColor" strokeWidth="1.7" fill="none" />
      <path d="M5 20a7 7 0 0 1 14 0" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" fill="none" />
    </>
  ),
  'i-gear': (
    <>
      <path
        d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"
        stroke="currentColor"
        strokeWidth="1.6"
        fill="none"
      />
      <circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="1.6" fill="none" />
    </>
  ),
  'i-doc': (
    <>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" fill="none" />
      <path d="M14 3v5h5M9 13h6M9 17h6" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" fill="none" />
    </>
  ),
  'i-chart': (
    <>
      <path d="M4 4v15a1 1 0 0 0 1 1h15" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" fill="none" />
      <path d="M9 16v-5M14 16V7M19 16v-3" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" fill="none" />
    </>
  ),
  'i-search': (
    <>
      <circle cx="11" cy="11" r="7" stroke="currentColor" strokeWidth="1.7" fill="none" />
      <path d="m21 21-4.3-4.3" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </>
  ),
  'i-bulb': (
    <>
      <path
        d="M12 3a6 6 0 0 0-3.9 10.6c.6.5.9 1.3.9 2.1v.8h6v-.8c0-.8.3-1.6.9-2.1A6 6 0 0 0 12 3Z"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
        fill="none"
      />
      <path d="M9.5 19.5h5M10.5 22h3" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" fill="none" />
    </>
  ),
  'i-shield': (
    <path d="M12 22s8-3.6 8-10V5.2L12 2 4 5.2V12c0 6.4 8 10 8 10Z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" fill="none" />
  ),
  // 脱敏处理按钮：星号打码示意（圆 + 四射星点）
  'i-eye-off': (
    <>
      <path d="M12 8.5c-3.6 0-6.5 2.6-7.5 5 .5 1.2 1.4 2.3 2.5 3.2M12 8.5c3.6 0 6.5 2.6 7.5 5-.5 1.2-1.4 2.3-2.5 3.2M12 8.5v-2M8.8 9.4 7.6 7.8M15.2 9.4l1.2-1.6M5 12h-2M21 12h-2" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" fill="none" />
      <circle cx="12" cy="14.5" r="2.6" stroke="currentColor" strokeWidth="1.6" fill="none" />
    </>
  ),
  'i-link': (
    <>
      <path d="M10 13.2a5 5 0 0 0 7.5.5l2.6-2.6a5 5 0 0 0-7-7L11.4 5.8" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" fill="none" />
      <path d="M14 10.8a5 5 0 0 0-7.5-.5l-2.6 2.6a5 5 0 0 0 7 7l1.7-1.6" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" fill="none" />
    </>
  ),
  'i-calendar': (
    <>
      <rect x="3.5" y="5" width="17" height="16" rx="2" stroke="currentColor" strokeWidth="1.7" fill="none" />
      <path d="M8 3v4M16 3v4M3.5 10.5h17" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" fill="none" />
    </>
  ),
  'i-edit': (
    <>
      <path d="M12 20h9" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
      <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" fill="none" />
    </>
  ),
  'i-mic': (
    <>
      <rect x="9" y="2.5" width="6" height="11" rx="3" stroke="currentColor" strokeWidth="1.7" fill="none" />
      <path d="M5.5 10.5v.5a6.5 6.5 0 0 0 13 0v-.5M12 17.5V21" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" fill="none" />
    </>
  ),
  'i-send': (
    <>
      <path d="M22 2 11 13" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M22 2 15 22l-4-9-9-4Z" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" fill="none" />
    </>
  ),
  'i-clip': (
    <path
      d="m21.4 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      fill="none"
    />
  ),
  'i-folder': (
    <path d="M4 7a2 2 0 0 1 2-2h3.6a2 2 0 0 1 1.6.8L12.5 7H18a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2Z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" fill="none" />
  ),
  'i-zap': <path d="M13 2 3 14h7l-1 8 11-14h-8Z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" fill="none" />,
  'i-layers': (
    <>
      <path d="m12 2 10 5-10 5L2 7Z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" fill="none" />
      <path d="m2 12 10 5 10-5M2 17l10 5 10-5" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" fill="none" />
    </>
  ),
  'i-globe': (
    <>
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.7" fill="none" />
      <path
        d="M3 12h18M12 3a14.5 14.5 0 0 1 0 18M12 3a14.5 14.5 0 0 0 0 18"
        stroke="currentColor"
        strokeWidth="1.7"
        fill="none"
      />
    </>
  ),
  'i-caret': <path d="m6 9 6 6 6-6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none" />,
  'i-chevl': <path d="m14 6-6 6 6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none" />,
  'i-chevr': <path d="m10 6 6 6-6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none" />,
  'i-arrow': <path d="M5 12h14M13 6l6 6-6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none" />,
  'i-up': <path d="M12 19V5M6 11l6-6 6 6" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" fill="none" />,
  'i-down': <path d="M12 5v14M18 13l-6 6-6-6" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" fill="none" />,
  // 导出：下载到本地
  'i-download': (
    <>
      <path d="M12 3v12M7.5 10.5 12 15l4.5-4.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" fill="none" />
      <path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" fill="none" />
    </>
  ),
  'i-chev2': (
    <path d="m6 17 5-5-5-5M13 17l5-5-5-5" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" fill="none" />
  ),
  'i-dots': (
    <>
      <circle cx="12" cy="5" r="1.7" fill="currentColor" />
      <circle cx="12" cy="12" r="1.7" fill="currentColor" />
      <circle cx="12" cy="19" r="1.7" fill="currentColor" />
    </>
  ),
  'i-undo': (
    <>
      <path d="M9 14 4 9l5-5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" fill="none" />
      <path d="M4 9h9.5a6 6 0 0 1 0 12H10" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" fill="none" />
    </>
  ),
  'i-redo': (
    <>
      <path d="m15 14 5-5-5-5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" fill="none" />
      <path d="M20 9h-9.5a6 6 0 0 0 0 12H14" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" fill="none" />
    </>
  ),
  'i-expand': (
    <path
      d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      fill="none"
    />
  ),
  'i-list': (
    <>
      <path d="M8.5 6h12M8.5 12h12M8.5 18h12" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      <circle cx="4" cy="6" r="1.3" fill="currentColor" />
      <circle cx="4" cy="12" r="1.3" fill="currentColor" />
      <circle cx="4" cy="18" r="1.3" fill="currentColor" />
    </>
  ),
  'i-shrink': (
    <path
      d="M8 3v5H3M16 3v5h5M8 21v-5H3M16 21v-5h5"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      fill="none"
    />
  ),
  'i-rotate': (
    <>
      <path d="M21 12a9 9 0 1 1-2.64-6.36" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" fill="none" />
      <path d="M21 3v5.5h-5.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" fill="none" />
    </>
  ),
  'i-panel': (
    <>
      <rect x="3" y="4.5" width="18" height="15" rx="2" stroke="currentColor" strokeWidth="1.8" fill="none" />
      <path d="M14.5 4.5v15" stroke="currentColor" strokeWidth="1.8" />
      <path d="M16 6.5h3v11h-3z" fill="currentColor" opacity=".4" />
    </>
  ),
  'i-pin': (
    <path
      d="M9.5 3.5h5l-.7 5.6 3.4 3.4h-4.7v4.5l-.5 3-.5-3v-4.5H6.8l3.4-3.4z"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinejoin="round"
      fill="none"
    />
  ),
  'i-minus': <path d="M5 12h14" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />,
  'i-music': (
    <>
      <path d="M9 18V5l10-2v13" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" fill="none" />
      <circle cx="6" cy="18" r="2.6" stroke="currentColor" strokeWidth="1.8" fill="none" />
      <circle cx="16" cy="16" r="2.6" stroke="currentColor" strokeWidth="1.8" fill="none" />
    </>
  ),
  'i-square': <rect x="5.5" y="5.5" width="13" height="13" rx="1.5" stroke="currentColor" strokeWidth="1.8" fill="none" />,
  'i-x': <path d="M18 6 6 18M6 6l12 12" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" />,
  'i-check': <path d="M20 6 9 17l-5-5" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" fill="none" />,
  'i-copy': (
    <>
      <rect x="9" y="9" width="11" height="11" rx="2" stroke="currentColor" strokeWidth="1.8" fill="none" />
      <path d="M5 15H4.5A1.5 1.5 0 0 1 3 13.5v-9A1.5 1.5 0 0 1 4.5 3h9A1.5 1.5 0 0 1 15 4.5V5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" fill="none" />
    </>
  ),
  'i-new': (
    <>
      <path d="M3 12a9 9 0 1 0 2.6-6.3L3 8" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" fill="none" />
      <path d="M3 3v5h5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" fill="none" />
    </>
  ),
  'i-alert': (
    <>
      <path d="M12 3 2.5 20h19Z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" fill="none" />
      <path d="M12 9.5V14M12 16.8v.2" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </>
  )
}

export function Icon({ id, className = 'icon' }: { id: keyof typeof SPRITE | string; className?: string }): JSX.Element {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true">
      {SPRITE[id] ?? null}
    </svg>
  )
}
