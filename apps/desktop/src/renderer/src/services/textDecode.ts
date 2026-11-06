/**
 * 文本解码：优先 UTF-8（严格模式），失败后回退 GB18030（兼容 GBK/GB2312）。
 *
 * 国内卷宗里大量 .txt/.csv 是 GBK 编码，Blob.text() 固定按 UTF-8 解码会得到乱码；
 * 这里先尝试严格 UTF-8，出现非法字节再按 GB18030 解码，并把实际编码回传给 UI。
 */
export interface DecodedText {
  text: string
  encoding: 'utf-8' | 'gb18030'
}

export async function decodeBlobText(blob: Blob): Promise<DecodedText> {
  const buffer = await blob.arrayBuffer()
  try {
    return { text: new TextDecoder('utf-8', { fatal: true }).decode(buffer), encoding: 'utf-8' }
  } catch {
    try {
      return { text: new TextDecoder('gb18030').decode(buffer), encoding: 'gb18030' }
    } catch {
      return { text: new TextDecoder('utf-8').decode(buffer), encoding: 'utf-8' }
    }
  }
}
