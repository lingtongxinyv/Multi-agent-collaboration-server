import { useState } from 'react'
import { api } from '../api'
import { toast } from '../main'

interface LoginResponse { role: string }

export function Login({ onSuccess, admin = false }: { onSuccess: () => Promise<void>; admin?: boolean }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true); setError('')
    try {
      const res = await api.post<LoginResponse>('/api/auth/login', { username, password })
      if (admin && res.role !== 'admin') {
        await api.post('/api/auth/logout').catch(() => {})
        setError('该入口仅限管理员账号登录')
        return
      }
      await onSuccess()
      toast('登录成功')
    } catch (err) {
      setError(String((err as Error).message))
    } finally { setBusy(false) }
  }

  return (
    <div className="login-wrap">
      <form className="login-box" onSubmit={submit}>
        <div className="login-logo"><img src="/favicon.svg" alt="" /></div>
        <h1>铃枢 BellHub</h1>
        <div className="hint">{admin ? '管理控制台 · 仅限管理员' : '欢迎回来 · 站娘协作平台'}</div>
        <input placeholder="用户名" value={username} autoFocus
          onChange={e => setUsername(e.target.value)} />
        <input placeholder="密码" type="password" value={password}
          onChange={e => setPassword(e.target.value)} />
        {error && <div className="error-text">{error}</div>}
        <button className="primary" disabled={busy || !username || !password}>
          {busy ? '登录中…' : '登 录'}
        </button>
        {!admin && <a className="back-home" href="/">← 返回首页</a>}
      </form>
    </div>
  )
}
