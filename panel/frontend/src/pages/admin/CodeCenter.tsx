import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api'
import { toast } from '../../main'

interface RepoInfo { name: string; path: string; exists: boolean; branch?: string; dirty?: number; remote?: string | null; error?: string }
interface StatusEntry { x: string; y: string; path: string }
interface RepoStatus { branch: string; ahead: number; behind: number; entries: StatusEntry[] }
interface TreeNode { dirs: string[]; files: { name: string; size: number; state?: [string, string] }[] }
interface DeployJob { id: string; repo: string; mode: string; status: string; logs: string[]; steps: string[]; current: string | null }

const STATE_LABEL: Record<string, string> = {
  M: '修改', A: '新增', D: '删除', '?': '未跟踪', R: '重命名', C: '复制',
}

function stateLabel(x: string, y: string): string {
  const a = STATE_LABEL[x === '?' ? '?' : x] ?? x
  const b = STATE_LABEL[y] ?? y
  return x === '?' ? a : a === b ? a : `${a}/${b}`
}

export function CodeCenter() {
  const [repos, setRepos] = useState<RepoInfo[]>([])
  const [repo, setRepo] = useState('')
  const [status, setStatus] = useState<RepoStatus | null>(null)
  const [log, setLog] = useState<{ hash: string; date: string; msg: string }[]>([])
  const [cwd, setCwd] = useState('')
  const [tree, setTree] = useState<TreeNode | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [view, setView] = useState<{ mode: 'edit' | 'diff'; path: string; content: string } | null>(null)
  const [commitMsg, setCommitMsg] = useState('')
  const [busy, setBusy] = useState('')
  const [job, setJob] = useState<DeployJob | null>(null)
  const [assistantNote, setAssistantNote] = useState('')
  const pollRef = useRef<number | null>(null)

  const loadRepos = useCallback(async () => {
    const list = await api.get<RepoInfo[]>('/api/admin/code/repos')
    setRepos(list)
    setRepo(prev => prev || list.find(r => r.exists)?.name || '')
  }, [])

  const refresh = useCallback(async (repoName: string) => {
    if (!repoName) return
    try {
      const [st, lg] = await Promise.all([
        api.get<RepoStatus>(`/api/admin/code/status?repo=${repoName}`),
        api.get<{ hash: string; date: string; msg: string }[]>(`/api/admin/code/log?repo=${repoName}&limit=15`),
      ])
      setStatus(st); setLog(lg)
      setSelected(new Set())
    } catch (err) { toast(String((err as Error).message), 'err') }
  }, [])

  const loadTree = useCallback(async (repoName: string, dir: string) => {
    try {
      setTree(await api.get<TreeNode>(`/api/admin/code/tree?repo=${repoName}&path=${encodeURIComponent(dir)}`))
      setCwd(dir)
    } catch (err) { toast(String((err as Error).message), 'err') }
  }, [])

  useEffect(() => { loadRepos() }, [loadRepos])
  useEffect(() => { if (repo) { refresh(repo); loadTree(repo, '') } }, [repo, refresh, loadTree])

  // 部署任务轮询
  useEffect(() => {
    if (!job || job.status === 'done' || job.status === 'failed') {
      if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null }
      return
    }
    pollRef.current = window.setInterval(async () => {
      try {
        const fresh = await api.get<DeployJob>(`/api/admin/code/deploy/${job.id}`)
        setJob(fresh)
        if (fresh.status === 'done') toast('部署完成')
        if (fresh.status === 'failed') toast('部署失败，见日志', 'err')
      } catch { /* 任务过期即停 */ }
    }, 3000)
    return () => { if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null } }
  }, [job])

  const run = async (name: string, fn: () => Promise<void>) => {
    setBusy(name)
    try { await fn() } catch (err) { toast(String((err as Error).message), 'err') }
    finally { setBusy('') }
  }

  const openFile = (path: string) => run(`open:${path}`, async () => {
    const res = await api.get<{ content: string }>(`/api/admin/code/file?repo=${repo}&path=${encodeURIComponent(path)}`)
    setView({ mode: 'edit', path, content: res.content })
  })

  const showDiff = (path: string) => run(`diff:${path}`, async () => {
    const res = await api.get<{ text: string }>(`/api/admin/code/diff?repo=${repo}&path=${encodeURIComponent(path)}`)
    setView({ mode: 'diff', path, content: res.text })
  })

  const saveFile = () => run('save', async () => {
    if (!view) return
    await api.put('/api/admin/code/file', { repo, path: view.path, content: view.content })
    toast('已保存')
    refresh(repo)
  })

  const doCommit = () => run('commit', async () => {
    const res = await api.post<{ sha: string }>('/api/admin/code/commit', { repo, message: commitMsg, paths: [...selected] })
    toast(`已提交 ${res.sha}`)
    setCommitMsg('')
    refresh(repo)
  })

  const doGit = (action: 'push' | 'pull') => run(action, async () => {
    const res = await api.post<{ note: string }>(`/api/admin/code/${action}`, { repo })
    toast(res.note || (action === 'push' ? '已推送' : '已拉取'))
    refresh(repo)
  })

  const doDeploy = (mode: 'restart' | 'full') => run(`deploy:${mode}`, async () => {
    const res = await api.post<{ job_id: string }>('/api/admin/code/deploy', { repo, mode })
    setJob({ id: res.job_id, repo, mode, status: 'running', logs: [], steps: [], current: null })
    toast('部署任务已启动')
  })

  const sendToAssistant = () => run('assistant', async () => {
    const paths = view?.mode === 'edit' && !selected.size ? [view.path] : [...selected]
    if (!paths.length) { toast('先勾选要交给站娘的文件', 'err'); return }
    const res = await api.post<{ reply: string; session_id: string }>(
      '/api/admin/code/send-to-assistant', { repo, paths, note: assistantNote })
    setView({ mode: 'diff', path: `站娘回复（${paths.length} 个文件）`, content: res.reply })
    toast('站娘已完成处理')
  })

  const toggleSelect = (path: string) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path); else next.add(path)
      return next
    })
  }

  const active = repos.find(r => r.name === repo)
  const breadcrumb = cwd ? cwd.split('/') : []

  return (
    <div className="page code-center">
      <div className="code-toolbar">
        <div className="code-tabs">
          {repos.filter(r => r.exists).map(r => (
            <button key={r.name} className={`tab ${r.name === repo ? 'on' : ''}`} onClick={() => setRepo(r.name)}>
              {r.name}{r.dirty ? ` (${r.dirty})` : ''}
            </button>
          ))}
        </div>
        {active && (
          <div className="code-meta">
            <span className="chip">{active.branch || '—'}</span>
            {status && status.ahead > 0 && <span className="chip warn">↑{status.ahead}</span>}
            {status && status.behind > 0 && <span className="chip warn">↓{status.behind}</span>}
            <button className="ghost" style={{ flex: '1 1 140px' }} disabled={!!busy} onClick={() => doGit('pull')}>拉取</button>
            <button className="ghost" style={{ flex: '1 1 140px' }} disabled={!!busy} onClick={() => doGit('push')}>推送</button>
            <button className="ghost" style={{ flex: '1 1 140px' }} disabled={!!busy} onClick={() => doDeploy('restart')}>重启服务</button>
            <button className="primary" style={{ flex: '1 1 140px' }} disabled={!!busy} onClick={() => doDeploy('full')}>构建并部署</button>
          </div>
        )}
      </div>

      <div className="code-grid">
        <aside className="code-side">
          <div className="card" style={{ maxHeight: 'none', overflow: 'visible' }}>
            <h3>改动 ({status?.entries.length ?? 0})</h3>
            {status?.entries.length === 0 && <div className="hint">工作区干净</div>}
            {status?.entries.map(e => (
              <label key={e.path} className="change-item">
                <input type="checkbox" checked={selected.has(e.path)} onChange={() => toggleSelect(e.path)} />
                <span className="change-state">{stateLabel(e.x, e.y)}</span>
                {/* 路径很长（panel/frontend/src/...），必须允许收缩 + 省略号，
                    否则 390px 手机上会顶出卡片右边（.change-item 是 flex 行） */}
                <button className="link min0 ellipsis" title={e.path} onClick={ev => { ev.preventDefault(); showDiff(e.path) }}>{e.path}</button>
              </label>
            ))}
            {selected.size > 0 && (
              <div className="commit-box">
                <input placeholder="提交信息" value={commitMsg}
                  onChange={e => setCommitMsg(e.target.value)} />
                <div className="row">
                  <button className="primary" disabled={!!busy || !commitMsg.trim()} onClick={doCommit}>提交 {selected.size} 个文件</button>
                </div>
                <div className="row">
                  <input placeholder="给站娘的说明（可选）" value={assistantNote}
                    onChange={e => setAssistantNote(e.target.value)} />
                  <button className="ghost" disabled={!!busy} title="把选中文件发给站娘，让站娘在服务器上自行部署"
                    onClick={sendToAssistant}>发站娘 ↗</button>
                </div>
              </div>
            )}
          </div>

          <div className="card" style={{ maxHeight: 'none', overflow: 'visible' }}>
            <h3>最近提交</h3>
            {log.map(item => (
              <div key={item.hash} className="log-item">
                <code>{item.hash}</code> <span>{item.msg}</span>
              </div>
            ))}
          </div>

          {job && (
            <div className="card" style={{ maxHeight: 'none', overflow: 'visible' }}>
              <h3>部署任务 {job.status === 'running' ? '· 进行中' : job.status === 'done' ? '· 完成' : '· 失败'}</h3>
              <pre className="deploy-log" style={{ whiteSpace: 'pre', overflowX: 'auto' }}>{job.logs.join('\n') || '启动中…'}</pre>
            </div>
          )}
        </aside>

        <section className="code-main">
          <div className="file-nav">
            <button className="ghost" onClick={() => loadTree(repo, '')}>根目录</button>
            {breadcrumb.map((part, i) => (
              <button key={i} className="ghost" onClick={() => loadTree(repo, breadcrumb.slice(0, i + 1).join('/'))}>{part}</button>
            ))}
          </div>
          {tree && (
            <div className="file-list">
              {tree.dirs.map(d => (
                <button key={d} className="file-row dir" onClick={() => loadTree(repo, cwd ? `${cwd}/${d}` : d)}>📁 {d}/</button>
              ))}
              {tree.files.map(f => (
                <div key={f.name} className="file-row file">
                  <span className={`state ${f.state ? 'dirty' : ''}`}>{f.state ? stateLabel(f.state[0], f.state[1]) : ''}</span>
                  <button className="link min0 ellipsis" onClick={() => openFile(cwd ? `${cwd}/${f.name}` : f.name)}>{f.name}</button>
                  <span className="size">{f.size >= 0 ? `${(f.size / 1024).toFixed(1)}K` : ''}</span>
                </div>
              ))}
            </div>
          )}

          {view && (
            <div className="editor-box">
              <div className="editor-head" style={{ flexWrap: 'wrap' }}>
                <strong style={{ minWidth: 0, overflowWrap: 'anywhere' }}>{view.path}</strong>
                <div className="row">
                  {view.mode === 'edit' && <button className="primary" disabled={!!busy} onClick={saveFile}>保存</button>}
                  <button className="ghost" onClick={() => setView(null)}>关闭</button>
                </div>
              </div>
              {view.mode === 'edit'
                ? <textarea className="code-editor" value={view.content} spellCheck={false}
                    style={{ minHeight: '34vh', fontSize: 16 }}
                    onChange={e => setView({ ...view, content: e.target.value })} />
                : <pre className="diff-view" style={{ overflowX: 'auto' }}>{view.content}</pre>}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}
