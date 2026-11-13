/** 工作站左栏：会话列表（TRAE 风格）——新建、置顶、右键重命名/删除，置顶优先按更新时间倒序。 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type Session } from '../api'
import { toast } from '../main'

const statusDot: Record<string, string> = {
  idle: '', running: 'run', done: '', failed: 'fail', cancelled: 'fail',
}

function relTime(ts: number): string {
  const diff = Date.now() / 1000 - ts
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return new Date(ts * 1000).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
}

export function SessionPane({ activeId, onSelect, listVersion }:
  { activeId: string | null; onSelect: (id: string | null) => void; listVersion: number }) {
  const [sessions, setSessions] = useState<Session[]>([])
  const [renaming, setRenaming] = useState<string | null>(null)
  const [renameText, setRenameText] = useState('')
  const [menu, setMenu] = useState<{ sid: string; x: number; y: number } | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  const load = useCallback(() => {
    api.get<Session[]>('/api/sessions').then(setSessions).catch(() => {})
  }, [])
  useEffect(() => { load() }, [load, listVersion])

  // 5s 轮询：流式期间其他端可见状态变化；流式结束后由父组件 bump listVersion
  useEffect(() => {
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [load])

  // 关闭右键菜单
  useEffect(() => {
    if (!menu) return
    const close = () => setMenu(null)
    window.addEventListener('click', close)
    return () => window.removeEventListener('click', close)
  }, [menu])

  const startRename = (s: Session) => {
    setRenaming(s.id)
    setRenameText(s.title)
  }

  /** 打开操作菜单并把坐标夹在视口内（右键在屏幕右下角时菜单会跑出屏幕外）。 */
  const openMenuAt = (sid: string, x: number, y: number) => {
    const w = 148, h = 140
    setMenu({
      sid,
      x: Math.max(8, Math.min(x, window.innerWidth - w - 8)),
      y: Math.max(8, Math.min(y, window.innerHeight - h - 8)),
    })
  }

  const saveRename = async (sid: string) => {
    const title = renameText.trim()
    setRenaming(null)
    if (!title) return
    try {
      await api.patch(`/api/sessions/${sid}`, { title })
      load()
    } catch (err) { toast(String((err as Error).message), 'err') }
  }

  const togglePin = async (s: Session) => {
    try {
      await api.post(`/api/sessions/${s.id}/pin`, { pinned: !s.pinned })
      load()
    } catch (err) { toast(String((err as Error).message), 'err') }
  }

  const remove = async (s: Session) => {
    if (!confirm(`删除会话「${s.title || '未命名'}」？消息记录将一并清除。`)) return
    try {
      await api.del(`/api/sessions/${s.id}`)
      if (activeId === s.id) onSelect(null)
      load()
    } catch (err) { toast(String((err as Error).message), 'err') }
  }

  return (
    <div className="ws-pane ws-sess">
      <div className="ws-pane-head">
        <h3>会话</h3>
        <button className="primary small" onClick={() => onSelect(null)}>＋ 新会话</button>
      </div>
      <div className="ws-pane-body ws-sess-body">
        {sessions.length === 0 && (
          <div className="muted" style={{ padding: 8, fontSize: 12 }}>
            暂无会话。发送第一条消息后自动创建。
          </div>
        )}
        {sessions.map(s => (
          <div
            key={s.id}
            className={`ws-sess-item ${activeId === s.id ? 'active' : ''}`}
            onClick={() => onSelect(s.id)}
            onContextMenu={e => {
              e.preventDefault()
              openMenuAt(s.id, e.clientX, e.clientY)
            }}
            title={s.title || '未命名会话'}
          >
            {renaming === s.id ? (
              <input
                className="ws-sess-rename"
                autoFocus
                value={renameText}
                maxLength={120}
                onClick={e => e.stopPropagation()}
                onChange={e => setRenameText(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter') saveRename(s.id)
                  if (e.key === 'Escape') setRenaming(null)
                }}
                onBlur={() => saveRename(s.id)}
              />
            ) : (
              <>
                <span className={`ws-sess-dot ${statusDot[s.status] ?? ''}`} />
                <span className="ws-sess-title">
                  {s.pinned ? <span className="ws-sess-pin">📌</span> : null}
                  {s.title || '未命名会话'}
                </span>
                <span className="ws-sess-time">{relTime(s.updated_at)}</span>
                {/* 触屏没有右键（长按在安卓/微信里也不可靠），所以必须有可见的入口 */}
                <button
                  className="ws-sess-more"
                  title="重命名 / 置顶 / 删除"
                  aria-label="会话操作"
                  onClick={e => {
                    e.stopPropagation()
                    const r = (e.currentTarget as HTMLElement).getBoundingClientRect()
                    openMenuAt(s.id, r.right - 148, r.bottom + 6)
                  }}
                >⋯</button>
              </>
            )}
          </div>
        ))}
      </div>

      {menu && (
        <div className="ws-ctx" ref={menuRef} style={{ left: menu.x, top: menu.y }}>
          {(() => {
            const s = sessions.find(x => x.id === menu.sid)
            if (!s) return null
            return (
              <>
                <button onClick={() => { setMenu(null); startRename(s) }}>重命名</button>
                <button onClick={() => { setMenu(null); togglePin(s) }}>
                  {s.pinned ? '取消置顶' : '置顶'}
                </button>
                <button className="danger" onClick={() => { setMenu(null); remove(s) }}>删除</button>
              </>
            )
          })()}
        </div>
      )}
    </div>
  )
}
