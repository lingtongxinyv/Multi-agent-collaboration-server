import { useEffect, useState } from 'react'
import { api } from '../api'
import { useAuth, toast } from '../main'

interface MemoryInfo { summary: string; recent: string[] }

export function ProfilePage() {
  const { me, refresh } = useAuth()
  const [oldPw, setOldPw] = useState('')
  const [newPw, setNewPw] = useState('')
  const [memory, setMemory] = useState<MemoryInfo | null>(null)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const q = me?.quota

  useEffect(() => { api.get<MemoryInfo>('/api/memory').then(setMemory).catch(() => {}) }, [])

  const change = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      await api.post('/api/auth/password', { old: oldPw, new: newPw })
      toast('密码已修改'); setOldPw(''); setNewPw(''); refresh()
    } catch (err) { toast(String(err.message), 'err') }
  }

  const saveMemory = async () => {
    try {
      await api.put('/api/memory', { summary: draft })
      setMemory(m => m ? { ...m, summary: draft } : m)
      setEditing(false)
      toast('记忆已保存')
    } catch (err) { toast(String((err as Error).message), 'err') }
  }

  const clearMemory = async () => {
    if (!confirm('清空站娘对你的全部记忆？该操作不可恢复。')) return
    try {
      await api.del('/api/memory')
      setMemory({ summary: '', recent: [] })
      toast('记忆已清空')
    } catch (err) { toast(String((err as Error).message), 'err') }
  }

  if (!me) return null
  return (
    <>
      <h2>账号设置</h2>
      {me.must_change_pw && (
        <div className="card" style={{ borderColor: 'var(--warn)' }}>
          首次登录请立即修改默认密码。
        </div>
      )}
      {q && (
        <div className="card">
          <h3>配额使用</h3>
          <div className="grid cols2">
            <div className="stat">
              <div className="label">今日 token</div>
              <div className="value">{q.used_tokens} / {q.tokens_daily}</div>
              <div className="progress"><div style={{ width: `${Math.min(100, q.used_tokens / q.tokens_daily * 100)}%` }} /></div>
            </div>
            <div className="stat">
              <div className="label">沙箱磁盘</div>
              <div className="value">{q.used_disk_mb.toFixed(1)} / {q.disk_mb} MB</div>
              <div className="progress"><div style={{ width: `${Math.min(100, q.used_disk_mb / q.disk_mb * 100)}%` }} /></div>
            </div>
          </div>
          <div className="muted" style={{ marginTop: 8 }}>并发任务上限：{q.concurrent}</div>
        </div>
      )}
      <div className="card">
        <h3>站娘的记忆</h3>
        <div className="muted" style={{ marginBottom: 10 }}>
          助手会在任务与对话中自动记下你的偏好和重要信息，下次交流时记得你。可随时修正或清空。
        </div>
        {memory ? (
          <>
            <div className="label">长期记忆</div>
            {editing ? (
              <>
                <textarea rows={5} style={{ width: '100%' }} value={draft} onChange={e => setDraft(e.target.value)} maxLength={8000} />
                <div className="row end" style={{ marginTop: 8 }}>
                  <button onClick={() => setEditing(false)}>取消</button>
                  <button className="primary" onClick={saveMemory}>保存</button>
                </div>
              </>
            ) : (
              <pre className="result" style={{ whiteSpace: 'pre-wrap', minHeight: 40 }}>
                {memory.summary || '（暂无长期记忆，多与站娘交流即可积累）'}
              </pre>
            )}
            {!editing && (
              <div className="row end" style={{ marginTop: 8 }}>
                <button onClick={() => { setDraft(memory.summary); setEditing(true) }}>编辑</button>
                <button className="danger" onClick={clearMemory}>清空记忆</button>
              </div>
            )}
            {memory.recent.length > 0 && (
              <>
                <div className="label" style={{ marginTop: 14 }}>最近互动摘要（自动记录，定期并入长期记忆）</div>
                <pre className="result" style={{ whiteSpace: 'pre-wrap', fontSize: 12.5 }}>
                  {memory.recent.join('\n')}
                </pre>
              </>
            )}
          </>
        ) : <div className="muted">加载中…</div>}
      </div>
      <div className="card">
        <h3>修改密码</h3>
        <form onSubmit={change} className="grid" style={{ gap: 10, maxWidth: 'min(360px, 100%)' }}>
          <input type="password" placeholder="当前密码" value={oldPw}
            onChange={e => setOldPw(e.target.value)} required />
          <input type="password" placeholder="新密码（至少 8 位）" value={newPw}
            onChange={e => setNewPw(e.target.value)} required minLength={8} />
          <button className="primary">修改密码</button>
        </form>
      </div>
    </>
  )
}
