import { useCallback, useEffect, useRef, useState } from 'react'
import { api, fmtTime } from '../../api'
import { toast } from '../../main'

/** 伙伴-A 风格七字段 + 通用字段，键顺序即表单顺序。 */
const CARD_FIELDS: [string, string][] = [
  ['name', '名称'],
  ['description', '描述'],
  ['性格原型', '性格原型'],
  ['性格', '性格'],
  ['口癖', '口癖'],
  ['爱好', '爱好'],
  ['雷点', '雷点'],
  ['隐藏设定', '隐藏设定'],
  ['一句话台词', '一句话台词'],
  ['first_mes', '开场白'],
]

interface PersonaData {
  card: Record<string, string>
  prefix: string
  suffix: string
  updated_at: number
}

export function PersonaPage() {
  const [data, setData] = useState<PersonaData | null>(null)
  const [importing, setImporting] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const load = useCallback(() => {
    api.get<PersonaData>('/api/admin/persona').then(setData).catch(err => toast(String(err.message), 'err'))
  }, [])
  useEffect(load, [load])

  const save = async () => {
    if (!data) return
    try {
      const r = await api.put<{ note: string }>('/api/admin/persona', data)
      toast(r.note)
      load()
    } catch (err) { toast(String(err.message), 'err') }
  }

  const doImport = async (file: File) => {
    setImporting(true)
    try {
      const archive = JSON.parse(await file.text())
      const r = await api.post<{ name: string }>('/api/admin/persona/import', archive)
      toast(`已导入人格「${r.name}」，重启 dsh-admin 后生效`)
      load()
    } catch (err) { toast(`导入失败: ${String(err.message)}`, 'err') }
    finally { setImporting(false) }
  }

  if (!data) return <h2>站娘人格</h2>
  const set = (k: string, v: string) => setData({ ...data, card: { ...data.card, [k]: v } })

  return (
    <>
      <h2>站娘人格</h2>
      <div className="row stack" style={{ marginBottom: 12 }}>
        <button className="primary" onClick={save}>保存</button>
        <a className="btn" href="/api/admin/persona/export" download>导出归档</a>
        <button onClick={() => fileRef.current?.click()} disabled={importing}>
          {importing ? '导入中…' : '导入归档'}
        </button>
        <input ref={fileRef} type="file" accept=".json,application/json" style={{ display: 'none' }}
          onChange={e => { const f = e.target.files?.[0]; if (f) doImport(f); e.target.value = '' }} />
        <span className="muted">最后更新：{fmtTime(data.updated_at)}</span>
      </div>

      <div className="card">
        <h3>人格卡（伙伴-A 风格）</h3>
        {CARD_FIELDS.map(([key, label]) => (
          <div key={key} className="form-row">
            <label>{label}</label>
            {key === 'first_mes' || key === '性格' || key === '口癖' ? (
              <textarea rows={key === 'first_mes' ? 5 : 3} value={data.card[key] ?? ''}
                onChange={e => set(key, e.target.value)} />
            ) : (
              <input value={data.card[key] ?? ''} onChange={e => set(key, e.target.value)} />
            )}
          </div>
        ))}
        {Object.keys(data.card).some(k => !CARD_FIELDS.some(([f]) => f === k)) && (
          <p className="muted">归档中还包含其他自定义字段（导入时保留，此处不展示）。</p>
        )}
      </div>

      <div className="card">
        <h3>DSH Persona 注入</h3>
        <div className="form-row">
          <label>prefix（写入 preset persona 行）</label>
          <textarea rows={6} value={data.prefix}
            onChange={e => setData({ ...data, prefix: e.target.value })} />
        </div>
        <div className="form-row">
          <label>suffix（可选）</label>
          <textarea rows={4} value={data.suffix}
            onChange={e => setData({ ...data, suffix: e.target.value })} />
        </div>
        <p className="muted">保存后补丁写入 standard.patch.yml，需在「服务管理」重启 dsh-admin 才对新会话生效。</p>
      </div>
    </>
  )
}
