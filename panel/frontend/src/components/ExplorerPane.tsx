/** 工作站右栏：资源管理器风格文件树，点击文件浮层预览，「引用」把文件挂进对话上下文。 */

import { useCallback, useEffect, useState } from 'react'
import { api, type FileItem } from '../api'
import { toast } from '../main'

export function ExplorerPane({ refs, onToggleRef }:
  { refs: string[]; onToggleRef: (path: string) => void }) {
  const [path, setPath] = useState('')
  const [items, setItems] = useState<FileItem[]>([])
  const [preview, setPreview] = useState<{ path: string; content: string } | null>(null)

  const load = useCallback((p: string) => {
    api.get<FileItem[]>(`/api/files?path=${encodeURIComponent(p)}`)
      .then(setItems).catch(err => toast(String(err.message), 'err'))
  }, [])
  useEffect(() => { load(path) }, [path, load])

  const open = (item: FileItem) => {
    if (item.type === 'dir') { setPath(item.relpath); return }
    api.get<{ content: string } | string>(`/api/files/content?path=${encodeURIComponent(item.relpath)}`)
      .then(r => setPreview({ path: item.relpath, content: typeof r === 'string' ? r : (r as { content: string }).content }))
      .catch(err => toast(String(err.message), 'err'))
  }

  const toggle = (relpath: string) => onToggleRef(relpath)

  const crumbs = path ? path.split('/') : []

  return (
    <div className="ws-pane">
      <div className="ws-pane-head">
        <h3>文件</h3>
        <button className="small" onClick={() => load(path)}>刷新</button>
      </div>
      <div className="ws-crumbs">
        <a onClick={() => setPath('')}>根目录</a>
        {crumbs.map((c, i) => (
          <span key={i}> / <a onClick={() => setPath(crumbs.slice(0, i + 1).join('/'))}>{c}</a></span>
        ))}
      </div>
      <div className="ws-pane-body">
        {items.length === 0 && <div className="muted" style={{ padding: 8, fontSize: 13 }}>空目录</div>}
        {items.map(item => {
          const referenced = refs.includes(item.relpath)
          return (
            <div key={item.relpath} className={`ws-file ${referenced ? 'referenced' : ''}`}
              onClick={() => open(item)}>
              <span className="ws-file-name">
                {item.type === 'dir' ? '📁' : '📄'} {item.name}
              </span>
              <span className="ws-file-ops">
                {item.size != null && (
                  <span className="muted" style={{ fontSize: 11 }}>
                    {(item.size / 1024).toFixed(1)}K
                  </span>
                )}
                {item.type === 'file' && (
                  <button
                    className={`small ws-ref-btn ${referenced ? 'on' : ''}`}
                    title={referenced ? '取消引用' : '引用到对话'}
                    onClick={e => { e.stopPropagation(); toggle(item.relpath) }}
                  >{referenced ? '已引用' : '引用'}</button>
                )}
              </span>
            </div>
          )
        })}
      </div>

      {preview && (
        <div className="ws-preview" onClick={() => setPreview(null)}>
          <div className="ws-preview-box" onClick={e => e.stopPropagation()}>
            <div className="ws-pane-head">
              <h3 className="mono" style={{ fontSize: 13, minWidth: 0, overflowWrap: 'anywhere' }}>{preview.path}</h3>
              <div className="row">
                <button className="small" onClick={() => { toggle(preview.path); setPreview(null) }}>
                  {refs.includes(preview.path) ? '取消引用' : '引用到对话'}
                </button>
                <button className="small" onClick={() => setPreview(null)}>关闭</button>
              </div>
            </div>
            <pre className="result" style={{ maxHeight: '60vh' }}>{preview.content}</pre>
          </div>
        </div>
      )}
    </div>
  )
}
