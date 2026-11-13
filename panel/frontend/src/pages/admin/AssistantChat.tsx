/** 管理端「站娘对话」：API Key 管理 + 管理员快捷对话（/api/admin/assistant/chat 多轮）。 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api'
import { toast } from '../../main'

interface ConfigInfo { configured: boolean; key_hint: string }
interface Msg { role: 'user' | 'assistant'; text: string }

export function AssistantChat() {
  const [config, setConfig] = useState<ConfigInfo | null>(null)
  const [keyInput, setKeyInput] = useState('')
  const [showKeyForm, setShowKeyForm] = useState(false)
  const [messages, setMessages] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const dshSidRef = useRef<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  const loadConfig = useCallback(async () => {
    const info = await api.get<ConfigInfo>('/api/admin/assistant/config')
    setConfig(info)
    setShowKeyForm(!info.configured)
  }, [])

  useEffect(() => { loadConfig() }, [loadConfig])
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, busy])

  const saveKey = async () => {
    try {
      await api.put('/api/admin/assistant/config', { api_key: keyInput.trim() })
      toast('API Key 已保存')
      setKeyInput('')
      await loadConfig()
    } catch (err) { toast(String((err as Error).message), 'err') }
  }

  const send = async () => {
    const text = input.trim()
    if (!text || busy) return
    setMessages(list => [...list, { role: 'user', text }])
    setInput('')
    setBusy(true)
    try {
      const res = await api.post<{ reply: string; session_id: string }>(
        '/api/admin/assistant/chat', { text, session_id: dshSidRef.current })
      dshSidRef.current = res.session_id
      setMessages(list => [...list, { role: 'assistant', text: res.reply }])
    } catch (err) {
      setMessages(list => [...list, { role: 'assistant', text: `⚠ ${String((err as Error).message)}` }])
    } finally { setBusy(false) }
  }

  const reset = () => {
    dshSidRef.current = null
    setMessages([])
  }

  return (
    <div className="page chat-page">
      <div className="chat-head" style={{ flexWrap: 'wrap', gap: 8 }}>
        <h2>站娘对话</h2>
        <div className="row">
          <button className="ghost" onClick={reset}>新会话</button>
        </div>
      </div>

      {showKeyForm && (
        <div className="card key-card">
          <h3>配置站娘 API Key（DeepSeek 官方 sk-…）</h3>
          <div className="hint">Key 仅存于本机面板数据库，用于驱动后台 headless 站娘，与网页 DSH 的配置相互独立。</div>
          <div className="row">
            <input type="password" placeholder="sk-…" value={keyInput} autoFocus style={{ minWidth: 0, width: '100%' }}
              onChange={e => setKeyInput(e.target.value)} />
            <button className="primary" disabled={!keyInput.trim().startsWith('sk-')} onClick={saveKey}>保存</button>
          </div>
        </div>
      )}
      {config?.configured && !showKeyForm && (
        <div className="card" style={{ padding: '8px 16px', marginBottom: 10 }}>
          <div className="row stack" style={{ justifyContent: 'space-between' }}>
            <span className="chip">Key {config.key_hint}</span>
            <button className="ghost" onClick={() => setShowKeyForm(true)}>更换 Key</button>
          </div>
        </div>
      )}

      <div className="chat-scroll">
        {messages.length === 0 && (
          <div className="hint chat-empty">
            管理员快捷对话通道（此页对话不进入用户工作站，也不会写入用户记忆）。
            普通用户对话请前往主站「工作站」。
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`chat-msg ${m.role}`}>
            <div className="bubble">{m.text}</div>
          </div>
        ))}
        {busy && <div className="chat-msg assistant"><div className="bubble typing">助手思考中…</div></div>}
        <div ref={bottomRef} />
      </div>

      <div className="chat-input">
        <textarea
          value={input}
          placeholder={busy ? '助手思考中…' : '输入消息，Enter 发送（Shift+Enter 换行）'}
          disabled={busy}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
          }}
        />
        <button className="primary" disabled={busy || !input.trim()} onClick={send}>发送</button>
      </div>
    </div>
  )
}
