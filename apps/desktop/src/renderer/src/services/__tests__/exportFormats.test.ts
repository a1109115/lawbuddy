import { describe, expect, it } from 'vitest'
import { exportOptionsFor, graphicOptions, supportsGraphicExport } from '../exportFormats'

describe('产物导出能力', () => {
  it('文件类产物第一项是原格式（字节复制保真）', () => {
    const opts = exportOptionsFor({ kind: 'file', title: '起诉书.docx' })
    expect(opts[0].mode).toBe('native')
    expect(opts[0].fmt).toBe('docx')
    expect(opts[0].label).toContain('.docx')
    // 允许原生转换
    expect(opts.some((o) => o.fmt === 'pdf')).toBe(true)
  })

  it('HTML 文件原格式导出而不是转成 Word 壳', () => {
    const opts = exportOptionsFor({ kind: 'file', title: '阅卷报告.html' })
    expect(opts[0].fmt).toBe('html')
    expect(opts[0].mode).toBe('native')
  })

  it('图像/音视频不提供跨格式转换，避免生成伪文件', () => {
    const img = exportOptionsFor({ kind: 'file', title: '现场照片.png' })
    expect(img).toHaveLength(1)
    expect(img[0].mode).toBe('native')
    const video = exportOptionsFor({ kind: 'file', title: '讯问录像.mp4' })
    expect(video).toHaveLength(1)
    expect(video[0].fmt).toBe('mp4')
  })

  it('结构化产物提供原生渲染格式，且不把 json 标成转换', () => {
    const opts = exportOptionsFor({ kind: 'report', title: '阅卷报告' })
    for (const fmt of ['markdown', 'docx', 'pdf', 'html', 'txt']) {
      expect(opts.some((o) => o.fmt === fmt)).toBe(true)
    }
    expect(opts.find((o) => o.fmt === 'json')?.mode).toBe('data')
    // 报告不默认提供 csv（避免无表格时导出空表）
    expect(opts.some((o) => o.fmt === 'csv')).toBe(false)
  })

  it('可视化与证据类产物额外提供 CSV 数据导出', () => {
    const viz = exportOptionsFor({ kind: 'viz', title: '时间线' })
    expect(viz.some((o) => o.fmt === 'csv' && o.mode === 'data')).toBe(true)
    const evidence = exportOptionsFor({ kind: 'evidence', title: '证据卡' })
    expect(evidence.some((o) => o.fmt === 'csv')).toBe(true)
  })

  it('可视化产物提供 SVG/PNG 图形导出', () => {
    expect(supportsGraphicExport({ kind: 'viz' })).toBe(true)
    expect(supportsGraphicExport({ kind: 'report', hasVizBody: true })).toBe(true)
    expect(supportsGraphicExport({ kind: 'report' })).toBe(false)
    const formats = graphicOptions().map((o) => o.fmt)
    expect(formats).toEqual(['svg', 'png'])
    expect(graphicOptions().every((o) => o.mode === 'graphic')).toBe(true)
  })
})
