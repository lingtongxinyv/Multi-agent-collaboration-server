import { useCallback, useEffect, useRef, useState } from 'react'
import {
  api, cancelChunkedUpload, chunkedUpload, computeFileSha256, downloadDriveFile,
  fmtSize, fmtTime, uploadDriveFile,
  type DriveListing, type DriveItem,
} from '../api'
import { toast } from '../main'

interface UploadTask {
  id: number
  name: string
  loaded: number
  total: number
  done: boolean
  error?: string
  phase?: string                  // 阶段提示：分片/续传/重试/拼接
  mode?: 'direct' | 'chunked'
  cancelled?: boolean
}

const CHUNK_THRESHOLD = 8 * 1024 * 1024      // ≤8MB 直传，大于则走分片

const imgExt = /\.(png|jpe?g|gif|webp|bmp|svg|avif)$/i
const txtExt = /\.(txt|md|json|ya?ml|log|csv|ts|tsx|js|jsx|py|sh|css|html?|xml|ini|conf|toml)$/i

export function DrivePage() {
  const [path, setPath] = useState('')
  const [data, setData] = useState<DriveListing | null>(null)
  const [sel, setSel] = useState<Set<string>>(new Set())
  const [tasks, setTasks] = useState<UploadTask[]>([])
  const [dragging, setDragging] = useState(false)
  const [preview, setPreview] = useState<{ path: string; kind: 'img' | 'txt'; text?: string } | null>(null)
  const [shareFor, setShareFor] = useState<DriveItem | null>(null)
  const [busy, setBusy] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const dragDepth = useRef(0)
  const stopRef = useRef<Map<number, boolean>>(new Map())   // 任务 id → 是否请求停止

  const load = useCallback((p: string) => {
    api.get<DriveListing>(`/api/drive/list?path=${encodeURIComponent(p)}`)
      .then(d => { setData(d); setSel(new Set()) })
      .catch(err => toast(String(err.message), 'err'))
  }, [])

  useEffect(() => { load(path) }, [path, load])

  const items = data?.items ?? []
  const usedPct = data && data.quota_mb > 0
    ? Math.min(100, (data.used_mb / data.quota_mb) * 100) : 0

  const openItem = (it: DriveItem) => {
    if (it.type === 'dir') { setPath(it.relpath); return }
    if (imgExt.test(it.name)) { setPreview({ path: it.relpath, kind: 'img' }); return }
    if (txtExt.test(it.name)) {
      api.get<string>(`/api/drive/preview?path=${encodeURIComponent(it.relpath)}`)
        .then(t => setPreview({ path: it.relpath, kind: 'txt', text: typeof t === 'string' ? t : JSON.stringify(t) }))
        .catch(err => toast(String(err.message), 'err'))
      return
    }
    downloadDriveFile(it.relpath, it.name)
  }

  /**
   * 上传一批文件（串行，逐个显示进度；单个失败不影响其余）。
   * ≤8MB 走一次性直传（省往返）；>8MB 走分片上传（可跨会话续传、失败可重试）。
   * 公网隧道仅约 0.8MB/s，大文件必须让用户看得见进度，也必须有续传能力。
   */
  const uploadFiles = async (files: FileList | File[]) => {
    const list = Array.from(files)
    if (!list.length) return
    const maxMb = data?.max_file_mb ?? 2048
    for (const f of list) {
      if (f.size > maxMb * 1048576) {
        toast(`${f.name} 超过单文件上限 ${maxMb}MB，已跳过`, 'err')
        continue
      }
      const id = Date.now() + Math.random()
      const dest = path ? `${path}/${f.name}` : f.name
      const chunked = f.size > CHUNK_THRESHOLD
      stopRef.current.set(id, false)
      setTasks(t => [...t, {
        id, name: f.name, loaded: 0, total: f.size, done: false,
        mode: chunked ? 'chunked' : 'direct',
        phase: chunked ? '准备中…' : '传输中',
      }])
      const upd = (patch: Partial<UploadTask>) =>
        setTasks(t => t.map(x => x.id === id ? { ...x, ...patch } : x))
      try {
        if (chunked) {
          // 先算内容哈希（用于更准确的续传匹配），并行于上传不可行——
          // 服务端 init 需要它，但算不到也不阻断（退化为路径+大小匹配）
          upd({ phase: '计算文件指纹…' })
          const sha = await computeFileSha256(f, (done) => {
            upd({ phase: `计算指纹 ${(done / f.size * 100).toFixed(0)}%` })
          })
          await chunkedUpload(f, dest, sha, {
            onSession: (s) => {
              if (s.resumed && s.uploaded.length) {
                upd({ phase: `续传：已存在 ${s.uploaded.length}/${s.total_chunks} 片`, loaded: 0 })
                toast(`检测到未完成上传，从第 ${s.uploaded.length} 片继续`)
              }
            },
            onProgress: (loaded, total, phase) => {
              upd({ loaded, total: total || f.size, phase: phase === 'uploading' ? '分片传输中' : phase })
            },
            shouldStop: () => stopRef.current.get(id) === true,
          })
        } else {
          await uploadDriveFile(dest, f, (loaded, total) => upd({ loaded, total }))
        }
        upd({ done: true, loaded: f.size, phase: '完成' })
        toast(`已上传 ${f.name}`)
      } catch (err) {
        const msg = String((err as Error).message)
        upd({ done: true, error: msg })
        toast(`${f.name} 上传失败：${msg}`, 'err')
      } finally {
        stopRef.current.delete(id)
      }
      load(path)
    }
    setTimeout(() => setTasks(t => t.filter(x => !x.done || x.error)), 6000)
  }

  /** 取消一个正在上传的任务：停止后续分片并清理服务端半成品。 */
  const cancelTask = async (t: UploadTask) => {
    stopRef.current.set(t.id, true)
    try {
      // 本地只标记停止；服务端半成品保留 7 天，用户下次传同一文件会自动续传，
      // 因此这里不主动删会话（删了就丢续传机会）。仅当用户明确选择"放弃"时才清理。
      toast(`${t.name} 已停止，已传分片保留 7 天，重新上传同一文件可续传`)
    } catch (err) { toast(String(err.message), 'err') }
  }

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault(); dragDepth.current = 0; setDragging(false)
    if (e.dataTransfer?.files?.length) uploadFiles(e.dataTransfer.files)
  }

  const mkdir = async () => {
    const name = prompt('新建文件夹名称')
    if (!name) return
    try {
      await api.post('/api/drive/mkdir', { path: path ? `${path}/${name}` : name })
      load(path); toast('文件夹已创建')
    } catch (err) { toast(String(err.message), 'err') }
  }

  const rename = async (it: DriveItem) => {
    const name = prompt('新名称', it.name)
    if (!name || name === it.name) return
    try {
      await api.post('/api/drive/rename', { path: it.relpath, new_name: name })
      load(path); toast('已重命名')
    } catch (err) { toast(String(err.message), 'err') }
  }

  const removeSel = async () => {
    const paths = sel.size ? [...sel] : []
    if (!paths.length) return
    if (!confirm(`确认删除选中的 ${paths.length} 项？目录会连同内容一起删除。`)) return
    setBusy(true)
    try {
      const r = await api.post<{ deleted: number; used_mb: number }>('/api/drive/delete', { paths })
      toast(`已删除 ${r.deleted} 项`); load(path)
    } catch (err) { toast(String(err.message), 'err') } finally { setBusy(false) }
  }

  const createShare = async (hours: number | null, note: string) => {
    if (!shareFor) return
    try {
      const r = await api.post<{ id: string }>('/api/drive/share',
        { path: shareFor.relpath, note, expire_hours: hours })
      const url = `${location.origin}/shared/${r.id}`
      try { await navigator.clipboard.writeText(url); toast('分享链接已复制到剪贴板') }
      catch { toast('分享已创建（复制失败，可在“共享”页查看）') }
      setShareFor(null)
    } catch (err) { toast(String(err.message), 'err') }
  }

  const toggleSel = (rel: string) => {
    setSel(s => { const n = new Set(s); n.has(rel) ? n.delete(rel) : n.add(rel); return n })
  }
  const allSelected = items.length > 0 && items.every(i => sel.has(i.relpath))

  const crumbs = path ? path.split('/') : []

  return (
    <>
      <h2>我的网盘</h2>

      {/* 配额条 */}
      <div className="card" style={{ marginBottom: 12 }}>
        <div className="row" style={{ alignItems: 'center' }}>
          <div style={{ flex: 1 }}>
            <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>
              已用 {data?.used_mb ?? 0} MB / {data?.quota_mb ?? '-'} MB（单文件上限 {data?.max_file_mb ?? '-'} MB）
            </div>
            <div style={{ height: 8, background: 'var(--line, #e5eaf2)', borderRadius: 4, overflow: 'hidden' }}>
              <div style={{
                width: `${usedPct}%`, height: '100%',
                background: usedPct > 90 ? '#e5484d' : (usedPct > 70 ? '#f5a524' : 'var(--accent, #4a8cff)'),
                transition: 'width .3s',
              }} />
            </div>
          </div>
          <div style={{ marginLeft: 16, textAlign: 'right' }} className="muted">
            {usedPct.toFixed(1)}% 已使用
          </div>
        </div>
      </div>

      {/* 工具栏 */}
      <div className="card">
        <div className="row" style={{ marginBottom: 10, flexWrap: 'wrap', gap: 8 }}>
          <button onClick={() => setPath('')} disabled={!path}>根目录</button>
          <button onClick={() => setPath(data?.parent ?? '')} disabled={!path}>上一级</button>
          <span className="mono muted min0 ellipsis" style={{ flex: '1 1 120px', maxWidth: '100%' }}>
            /{crumbs.join(' / ')}
          </span>
          <span style={{ flex: 1 }} />
          <button onClick={() => fileInput.current?.click()}>上传文件</button>
          <button onClick={mkdir}>新建文件夹</button>
          <button className="danger" onClick={removeSel} disabled={!sel.size || busy}>
            删除{sel.size ? ` (${sel.size})` : ''}
          </button>
          <button onClick={() => load(path)}>刷新</button>
          <input ref={fileInput} type="file" multiple hidden
            onChange={e => { if (e.target.files) uploadFiles(e.target.files); e.target.value = '' }} />
        </div>

        {/* 上传进度 */}
        {tasks.length > 0 && (
          <div style={{ marginBottom: 10 }}>
            {tasks.map(t => {
              const pct = t.total ? Math.min(100, (t.loaded / t.total) * 100) : 0
              return (
                <div key={t.id} style={{ marginBottom: 6 }}>
                  <div className="muted" style={{ fontSize: 12, display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 6 }}>
                    <span className="min0" style={{ overflowWrap: 'anywhere' }}>
                      {t.error ? '❌' : (t.done ? '✅' : (t.mode === 'chunked' ? '🧩' : '⬆'))} {t.name}
                    </span>
                    <span>
                      {t.mode === 'chunked' && !t.done && <span> （分片）</span>} — {fmtSize(t.loaded)} / {fmtSize(t.total)}
                      {t.phase && !t.done && <span> · {t.phase}</span>}
                      {t.error ? ` — ${t.error}` : ` (${pct.toFixed(0)}%)`}
                    </span>
                    {!t.done && !t.error && (
                      <button style={{ padding: '5px 10px', fontSize: 12.5 }} onClick={() => cancelTask(t)}>
                        停止
                      </button>
                    )}
                  </div>
                  <div style={{ height: 6, background: 'var(--line, #e5eaf2)', borderRadius: 3, overflow: 'hidden' }}>
                    <div style={{
                      width: `${pct}%`, height: '100%',
                      background: t.error ? '#e5484d' : '#4a8cff', transition: 'width .2s',
                    }} />
                  </div>
                </div>
              )
            })}
          </div>
        )}

        {/* 拖拽区 + 文件表 */}
        <div
          onDragEnter={e => { e.preventDefault(); dragDepth.current++; setDragging(true) }}
          onDragOver={e => e.preventDefault()}
          onDragLeave={e => { e.preventDefault(); if (--dragDepth.current <= 0) setDragging(false) }}
          onDrop={onDrop}
          style={{
            border: dragging ? '2px dashed var(--accent, #4a8cff)' : '2px dashed transparent',
            borderRadius: 8, transition: 'border-color .15s',
          }}
        >
          <div className="table-wrap">
          <table className="file-list table-cards">
            <thead>
              <tr>
                <th>
                  <input type="checkbox" checked={allSelected}
                    onChange={e => setSel(e.target.checked ? new Set(items.map(i => i.relpath)) : new Set())} />
                </th>
                <th>名称</th><th>大小</th>
                <th>修改时间</th><th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map(it => (
                <tr key={it.relpath} style={{ background: sel.has(it.relpath) ? 'rgba(74,140,255,.08)' : undefined }}>
                  <td data-label="">
                    <input type="checkbox" checked={sel.has(it.relpath)} onChange={() => toggleSel(it.relpath)} />
                  </td>
                  <td className="name" data-label="名称" onClick={() => openItem(it)} style={{ cursor: 'pointer' }}>
                    {it.type === 'dir' ? '📁' : '📄'} {it.name}
                  </td>
                  <td className="muted" data-label="大小">{it.type === 'dir' ? '-' : fmtSize(it.size)}</td>
                  <td className="muted" data-label="修改时间">{fmtTime(it.mtime)}</td>
                  <td data-label="">
                    {it.type === 'file' && (
                      <button onClick={() => downloadDriveFile(it.relpath, it.name)}>下载</button>
                    )}{' '}
                    <button onClick={() => rename(it)}>重命名</button>{' '}
                    <button onClick={() => setShareFor(it)}>分享</button>
                  </td>
                </tr>
              ))}
              {items.length === 0 && (
                <tr><td colSpan={5} className="muted" style={{ textAlign: 'center', padding: 28 }}>
                  空目录 —— 把文件拖到这里，或点「上传文件」
                </td></tr>
              )}
            </tbody>
          </table>
          </div>
        </div>
      </div>

      {/* 分享弹窗 */}
      {shareFor && (
        <ShareDialog item={shareFor} onCancel={() => setShareFor(null)} onCreate={createShare} />
      )}

      {/* 预览浮层 */}
      {preview && (
        <div onClick={() => setPreview(null)}
          style={{
            position: 'fixed', inset: 0, background: 'rgba(0,0,0,.55)', zIndex: 60,
            display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24,
          }}>
          <div onClick={e => e.stopPropagation()}
            style={{ background: 'var(--card, #fff)', borderRadius: 10, padding: 16, maxWidth: '90vw', maxHeight: '86vh', overflow: 'auto' }}>
            <div className="row" style={{ marginBottom: 8 }}>
              <strong className="mono min0" style={{ overflowWrap: 'anywhere' }}>{preview.path}</strong>
              <span style={{ flex: 1 }} />
              <button onClick={() => downloadDriveFile(preview.path)}>下载</button>
              <button onClick={() => setPreview(null)}>关闭</button>
            </div>
            {preview.kind === 'img'
              ? <img src={`/api/drive/preview?path=${encodeURIComponent(preview.path)}`}
                  alt={preview.path}
                  style={{ maxWidth: '100%', maxHeight: '70vh', width: 'auto', height: 'auto', objectFit: 'contain', display: 'block' }} />
              : <pre className="mono" style={{ maxWidth: '80vw', whiteSpace: 'pre-wrap', fontSize: 12 }}>{preview.text}</pre>}
          </div>
        </div>
      )}
    </>
  )
}

function ShareDialog({ item, onCancel, onCreate }: {
  item: DriveItem
  onCancel: () => void
  onCreate: (hours: number | null, note: string) => void
}) {
  const [hours, setHours] = useState<number | null>(24)
  const [note, setNote] = useState('')
  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,.45)', zIndex: 60,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>
      <div className="card" style={{ width: 'min(380px, calc(100vw - 32px))', minWidth: 0 }}>
        <h3>分享：{item.name}</h3>
        <p className="muted" style={{ fontSize: 12 }}>
          仅 BellHub 登录用户可打开该链接（外网游客即使拿到链接也无法访问）。
        </p>
        <div className="row" style={{ margin: '10px 0' }}>
          <label>有效期：</label>
          <select value={hours ?? 0} onChange={e => setHours(Number(e.target.value) || null)}>
            <option value={1}>1 小时</option>
            <option value={24}>1 天</option>
            <option value={168}>7 天</option>
            <option value={720}>30 天</option>
            <option value={0}>永久</option>
          </select>
        </div>
        <input placeholder="备注（可选）" value={note} onChange={e => setNote(e.target.value)}
          style={{ width: '100%', marginBottom: 12 }} />
        <div className="row end">
          <button onClick={onCancel}>取消</button>
          <button className="primary" onClick={() => onCreate(hours, note)}>创建并复制链接</button>
        </div>
      </div>
    </div>
  )
}
