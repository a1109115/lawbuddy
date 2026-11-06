import { useEffect, useState } from 'react'
import type { DocumentContentInfo } from '@lawbuddy/contracts'
import { getBridge } from '../services/bridge'

export interface DocContentState {
  status: 'idle' | 'loading' | 'ready' | 'error'
  info: DocumentContentInfo | null
  blob: Blob | null
  /** 组装后的 object URL（供 pdf.js / 图片 / Word / Excel 渲染使用） */
  url: string | null
  error: string | null
}

const IDLE: DocContentState = { status: 'idle', info: null, blob: null, url: null, error: null }

function base64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i)
  return out
}

/**
 * 原件内容（REQ-DOC-014）：Core 物料化只读副本 → 分块取字节 → 组装 Blob → object URL。
 *
 * Renderer 全程只持有不透明 contentId 与字节，不接触文件路径；
 * 卸载或切换文档时关闭会话（临时明文随会话释放），并回收 object URL。
 */
export function useDocContent(
  docId: string | null,
  enabled: boolean,
  pageNo?: number,
  /** 内容来源：真实卷宗（document）或文件类产物（artifact，AI 生成文档） */
  source: 'document' | 'artifact' = 'document'
): DocContentState {
  const [state, setState] = useState<DocContentState>(IDLE)

  useEffect(() => {
    const bridge = getBridge()
    if (!enabled || !docId || !bridge) {
      setState(IDLE)
      return
    }
    let cancelled = false
    let contentId: string | null = null
    let objectUrl: string | null = null
    setState({ ...IDLE, status: 'loading' })

    const close = (): void => {
      if (contentId) void bridge.document.closeContent(contentId).catch(() => undefined)
    }

    void (async () => {
      try {
        const info =
          source === 'artifact'
            ? await bridge.artifact.openContent({ artifactId: docId })
            : await bridge.document.openContent({ documentId: docId, pageNo })
        contentId = info.contentId
        if (cancelled) {
          close()
          return
        }
        const parts: BlobPart[] = []
        let offset = 0
        for (;;) {
          const chunk = await bridge.document.readContent({
            contentId: info.contentId,
            offset
          })
          if (cancelled) {
            close()
            return
          }
          const bytes = base64ToBytes(chunk.dataBase64)
          if (bytes.length === 0) break // 防御：空块不再推进，避免死循环
          parts.push(bytes.buffer as ArrayBuffer)
          offset += bytes.length
          if (chunk.eof) break
        }
        const blob = new Blob(parts, { type: info.mime || 'application/octet-stream' })
        objectUrl = URL.createObjectURL(blob)
        setState({ status: 'ready', info, blob, url: objectUrl, error: null })
      } catch (e) {
        if (cancelled) return
        setState({
          ...IDLE,
          status: 'error',
          error: e instanceof Error ? e.message : String(e)
        })
      }
    })()

    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
      close()
    }
  }, [docId, enabled, pageNo, source])

  return state
}