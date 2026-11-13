import { useCallback, useEffect, useState } from 'react'
import { api, fmtTime, type FileItem } from '../api'
import { toast } from '../main'

export function FilesPage() {
  const [path, setPath] = useState('')
  const [items, setItems] = useState<FileItem[]>([])
  const [editing, setEditing] = useState<{ path: string; content: string } | null>(null)

  const load = useCallback((p: string) => {
    api.get<FileItem[]>(`/api/files?path=${encodeURIComponent(p)}`)
      .then(setItems).catch(err => toast(String(err.message), 'err'))
  }, [])
  useEffect(() => { load(path) }, [path, load])

  const open = (item: FileItem) => {
    if (item.type === 'dir') { setPath(item.relpath); return }
    api.get(`/api/files/content?path=${encodeURIComponent(item.relpath)}`)
      .then((r) => setEditing({ path: item.relpath, content: r as unknown as string }))
      .catch(err => toast(String(err.message), 'err'))
  }

  const save = async () => {
    if (!editing) return
    try {
      await api.put('/api/files/content', editing)
      toast('已保存'); setEditing(null); load(path)
    } catch (err) { toast(String(err.message), 'err') }
  }

  const create = async () => {
    const name = prompt('新文件名（可含子目录）')
    if (!name) return
    try {
      await api.put('/api/files/content', { path: path ? `${path}/${name}` : name, content: '' })
      load(path)
    } catch (err) { toast(String(err.message), 'err') }
  }

  const remove = async (item: FileItem) => {
    if (!confirm(`删除 ${item.name}？`)) return
    try { await api.del(`/api/files?path=${encodeURIComponent(item.relpath)}`); load(path) }
    catch (err) { toast(String(err.message), 'err') }
  }

  const parent = path ? path.split('/').slice(0, -1).join('/') : ''

  return (
    <>
      <h2>我的文件（沙箱）</h2>
      <div className="card">
        <div className="row" style={{ marginBottom: 10 }}>
          <button onClick={() => setPath(parent)} disabled={!path}>上一级</button>
          <span className="mono muted min0 ellipsis" style={{ flex: '1 1 120px', maxWidth: '100%' }}>/{path}</span>
          <span className="spacer" style={{ flex: 1 }} />
          <button onClick={create}>新建文件</button>
          <button onClick={() => load(path)}>刷新</button>
        </div>
        <div className="table-wrap">
        <table className="file-list table-cards">
          <thead><tr><th>名称</th><th>类型</th><th>大小</th><th>修改时间</th><th></th></tr></thead>
          <tbody>
            {items.map(item => (
              <tr key={item.relpath}>
                <td className="name" data-label="名称" onClick={() => open(item)}>
                  {item.type === 'dir' ? '📁' : '📄'} {item.name}
                </td>
                <td className="muted" data-label="类型">{item.type === 'dir' ? '目录' : '文件'}</td>
                <td className="muted" data-label="大小">{item.size != null ? `${(item.size / 1024).toFixed(1)} KB` : '-'}</td>
                <td className="muted" data-label="修改时间">{fmtTime(item.mtime)}</td>
                <td data-label=""><button className="danger" onClick={() => remove(item)}>删除</button></td>
              </tr>
            ))}
            {items.length === 0 && <tr><td colSpan={5} className="muted">空目录</td></tr>}
          </tbody>
        </table>
        </div>
      </div>
      {editing && (
        <div className="card">
          <h3 style={{ overflowWrap: 'anywhere', wordBreak: 'break-all' }}>编辑：{editing.path}</h3>
          <textarea rows={10} style={{ width: '100%' }} className="mono"
            value={editing.content}
            onChange={e => setEditing({ ...editing, content: e.target.value })} />
          <div className="row stack end" style={{ marginTop: 10 }}>
            <button onClick={() => setEditing(null)}>关闭</button>
            <button className="primary" onClick={save}>保存</button>
          </div>
        </div>
      )}
    </>
  )
}
