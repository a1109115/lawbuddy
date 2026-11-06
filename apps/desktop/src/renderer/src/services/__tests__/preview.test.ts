import { describe, expect, it } from 'vitest'
import {
  isAudioKind,
  isCsvKind,
  isMediaKind,
  isVideoKind,
  resolvePreview,
  supportsOriginalPreview
} from '../preview'

describe('预览能力注册表', () => {
  it('核心办公格式均为原生渲染且可定位引证', () => {
    for (const kind of ['pdf', 'docx', 'xlsx']) {
      const cap = resolvePreview(kind)
      expect(cap.renderer).toBe(kind)
      expect(cap.paginated).toBe(true)
      expect(cap.textSelectable).toBe(true)
      expect(cap.citationLocatable).toBe(true)
      expect(cap.fallback).toBeNull()
    }
  })

  it('文本类按各自渲染器分发', () => {
    expect(resolvePreview('md').renderer).toBe('markdown')
    expect(resolvePreview('html').renderer).toBe('html')
    expect(resolvePreview('csv').renderer).toBe('csv')
    expect(resolvePreview('tsv').renderer).toBe('csv')
    expect(resolvePreview('json').renderer).toBe('code')
    expect(resolvePreview('code').renderer).toBe('code')
    expect(resolvePreview('txt').renderer).toBe('text')
  })

  it('图片与媒体类型不误判为不支持', () => {
    for (const kind of ['png', 'jpg', 'gif', 'webp', 'bmp', 'svg', 'tiff']) {
      expect(resolvePreview(kind).renderer).toBe('image')
      expect(supportsOriginalPreview(kind)).toBe(true)
    }
    expect(isAudioKind('mp3')).toBe(true)
    expect(isVideoKind('mp4')).toBe(true)
    expect(isMediaKind('mp4')).toBe(true)
    expect(isMediaKind('docx')).toBe(false)
    expect(resolvePreview('mp4').renderer).toBe('video')
    expect(resolvePreview('mp3').renderer).toBe('audio')
  })

  it('旧版 Office 只给文本降级并说明原因，不伪装成版式预览', () => {
    const cap = resolvePreview('doc')
    expect(cap.renderer).toBe('text')
    expect(cap.fallback).toContain('.docx')
    expect(cap.citationLocatable).toBe(false)
  })

  it('未知类型明确不支持并给出提示', () => {
    const cap = resolvePreview('xyz')
    expect(cap.renderer).toBe('unsupported')
    expect(supportsOriginalPreview('xyz')).toBe(false)
    expect(cap.fallback).toContain('XYZ')
  })

  it('CSV 判定只覆盖 csv/tsv', () => {
    expect(isCsvKind('csv')).toBe(true)
    expect(isCsvKind('tsv')).toBe(true)
    expect(isCsvKind('txt')).toBe(false)
  })
})
