import JSZip from 'jszip'
import { describe, expect, it } from 'vitest'
import { readSheetViewMeta } from '../xlsxMeta'

async function makeXlsx(sheetXml: string, relTarget = 'worksheets/sheet1.xml'): Promise<ArrayBuffer> {
  const zip = new JSZip()
  zip.file(
    'xl/workbook.xml',
    '<?xml version="1.0"?><workbook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">' +
      '<sheets><sheet name="明细" sheetId="1" r:id="rId1"/></sheets></workbook>'
  )
  zip.file(
    'xl/_rels/workbook.xml.rels',
    '<?xml version="1.0"?><Relationships>' +
      `<Relationship Id="rId1" Type="worksheet" Target="${relTarget}"/></Relationships>`
  )
  zip.file(`xl/${relTarget}`, sheetXml)
  return zip.generateAsync({ type: 'arraybuffer' })
}

describe('XLSX 视图元数据解析', () => {
  it('解析冻结窗格与自动筛选范围', async () => {
    const buffer = await makeXlsx(
      '<worksheet><sheetViews><sheetView workbookViewId="0">' +
        '<pane xSplit="1" ySplit="2" topLeftCell="B3" activePane="bottomRight" state="frozen"/>' +
        '</sheetView></sheetViews><autoFilter ref="A1:D20"/></worksheet>'
    )
    const [meta] = await readSheetViewMeta(buffer)
    expect(meta.freezeRows).toBe(2)
    expect(meta.freezeCols).toBe(1)
    expect(meta.autoFilter).toEqual({ startRow: 0, startCol: 0, endRow: 19, endCol: 3 })
  })

  it('仅冻结列时按 topLeftCell 推导冻结列数', async () => {
    const buffer = await makeXlsx(
      '<worksheet><sheetViews><sheetView workbookViewId="0">' +
        '<pane topLeftCell="C1" state="frozen"/>' +
        '</sheetView></sheetViews></worksheet>'
    )
    const [meta] = await readSheetViewMeta(buffer)
    expect(meta.freezeRows).toBe(0)
    expect(meta.freezeCols).toBe(2)
    expect(meta.autoFilter).toBeNull()
  })

  it('无冻结/筛选时返回空元数据', async () => {
    const buffer = await makeXlsx('<worksheet><sheetData/></worksheet>')
    const [meta] = await readSheetViewMeta(buffer)
    expect(meta).toEqual({ freezeRows: 0, freezeCols: 0, autoFilter: null })
  })

  it('损坏文件不抛错（降级为空）', async () => {
    const meta = await readSheetViewMeta(new Uint8Array([1, 2, 3]).buffer)
    expect(meta).toEqual([])
  })
})
