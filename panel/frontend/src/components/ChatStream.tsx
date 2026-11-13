/** 工作站中栏：对话流（SSE 流式回复）+ 可折叠思考/工具过程卡片 + 引用文件上下文。 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, type SessionDetail } from '../api'
import { toast } from '../main'

interface ToolStep {
  callId: string; tool: string; input: string
  result?: string; status?: string
}

interface LiveMsg {
  role: 'user' | 'assistant'
  text: string
  thinking?: string
  tools?: ToolStep[]
  tokens?: number
  error?: string
  streaming?: boolean
}

const emptySession = (id: string): SessionDetail => ({
  id, user_id: '', title: '', pinned: 0, status: 'running',
  dsh_session_id: '', tokens_used: 0, created_at: 0, updated_at: 0, messages: [],
})

/** 读取 SSE 流：POST + ReadableStream，逐事件回调。 */
async function readSSE(
  path: string, body: unknown,
  onEvent: (ev: Record<string, unknown>) => void,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`
    try {
      const data = await resp.json()
      detail = String(data.detail ?? data.error ?? detail)
    } catch { /* ignore */ }
    throw new ApiError(resp.status, detail)
  }
  const reader = resp.body!.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const parts = buf.split('\n\n')
    buf = parts.pop() ?? ''
    for (const part of parts) {
      for (const line of part.split('\n')) {
        if (line.startsWith('data:')) {
          try { onEvent(JSON.parse(line.slice(5).trim())) } catch { /* 跳过坏帧 */ }
        }
      }
    }
  }
}

const suggestions = [
  '帮我写一个 Python 脚本，统计目录下各类型文件的数量和总大小',
  '把「我的文件」里的笔记整理成一份带目录的 Markdown 文档',
  '解释一段代码的作用，并指出可以改进的地方',
]

/** 触屏设备：软键盘没有 Shift+Enter，换行键同样触发 Enter，
 *  若沿用「Enter 即发送」会导致手机上无法输入多行。触屏改为只用「发送」按钮提交。 */
const isTouch = typeof window !== 'undefined'
  && typeof window.matchMedia === 'function'
  && window.matchMedia('(pointer: coarse)').matches

export function ChatStream({ sessionId, refs, onRemoveRef, onSessionCreated, onSessionChanged }:
  {
    sessionId: string | null
    refs: string[]
    onRemoveRef: (path: string) => void
    onSessionCreated: (sid: string | null) => void
    onSessionChanged: () => void
  }) {
  const [detail, setDetail] = useState<SessionDetail | null>(null)
  const [msgs, setMsgs] = useState<LiveMsg[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [phase, setPhase] = useState('')
  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const busyRef = useRef(false)
  // 世代守卫：会话切换/新发送递增，使旧的流式回调失效，避免错写到其他会话
  const genRef = useRef(0)
  // 草稿首条消息自建的会话：切换 activeId 时不重载历史（正在流式中）
  const selfCreatedRef = useRef<string | null>(null)

  const scrollBottom = useCallback((smooth = true) => {
    requestAnimationFrame(() =>
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: smooth ? 'smooth' : 'auto' }))
  }, [])

  // 切换会话：中止进行中的流并加载历史
  useEffect(() => {
    if (!sessionId) {
      genRef.current++
      abortRef.current?.abort()
      setPhase('')
      // 会话被删/清空时也要复位忙态，否则输入框会永久禁用
      busyRef.current = false
      setBusy(false)
      setDetail(null); setMsgs([])
      return
    }
    if (selfCreatedRef.current === sessionId) {
      // 自己刚创建的会话：流正在进行，保留现场（不得 gen++/abort，否则会中止自己的流）
      selfCreatedRef.current = null
      setDetail(d => d ?? emptySession(sessionId))
      return
    }
    genRef.current++
    abortRef.current?.abort()
    setPhase('')
    busyRef.current = false
    setBusy(false)
    api.get<SessionDetail>(`/api/sessions/${sessionId}`)
      .then(d => {
        setDetail(d)
        setMsgs(d.messages.map(m => ({ role: m.role, text: m.text })))
        scrollBottom(false)
      })
      .catch(() => { setDetail(null); setMsgs([]) })
  }, [sessionId, scrollBottom])

  // 流式期间滚动跟随
  useEffect(() => { if (busy) scrollBottom() }, [msgs, busy, scrollBottom])

  const send = async () => {
    const text = input.trim()
    if (!text || busyRef.current) return
    busyRef.current = true
    setBusy(true)
    setInput('')
    // 登记 AbortController：此前这里漏了赋值，abortRef.current 恒为 null，
    // 导致 readSSE 拿不到 signal、「停止」按钮是空操作（2026-10-13 修复）
    const ctrl = new AbortController()
    abortRef.current = ctrl
    setMsgs(list => [...list, { role: 'user', text }, { role: 'assistant', text: '', streaming: true }])
    setPhase('连接中…')
    scrollBottom()

    const myGen = ++genRef.current
    const patchIf = (fn: (m: LiveMsg) => LiveMsg) => {
      if (genRef.current !== myGen) return
      setMsgs(list => list.length === 0 ? list : [...list.slice(0, -1), fn(list[list.length - 1])])
    }
    const livePatch = (fn: (l: LiveMsg[]) => LiveMsg[]) => {
      if (genRef.current !== myGen) return
      setMsgs(fn)
    }

    let sid = sessionId
    let createdHere = false
    let streamStarted = false
    let runTokens = 0
    try {
      if (!sid) {
        const s = await api.post<{ id: string }>('/api/sessions', { title: '' })
        sid = s.id
        createdHere = true
        selfCreatedRef.current = sid
        onSessionCreated(sid)
      }
      let current = ''
      await readSSE(`/api/sessions/${sid}/stream`, { text, refs }, ev => {
        streamStarted = true
        const type = ev.type as string
        if (type === 'session') { if (genRef.current === myGen) setPhase('站娘已就绪'); return }
        if (type === 'thinking') {
          if (genRef.current === myGen) setPhase('思考中…')
          patchIf(m => ({ ...m, thinking: (m.thinking ?? '') + ((ev.text as string) ?? '') }))
          return
        }
        if (type === 'text') {
          if (genRef.current === myGen) setPhase('')
          current = current ? `${current}\n\n${ev.text as string}` : (ev.text as string)
          patchIf(m => ({ ...m, text: current }))
          return
        }
        if (type === 'tool_call') {
          if (genRef.current === myGen) setPhase(`调用工具 ${ev.tool as string}…`)
          patchIf(m => ({
            ...m,
            tools: [...(m.tools ?? []), {
              callId: (ev.call_id as string) || '', tool: (ev.tool as string) || '',
              input: typeof ev.input === 'string' ? ev.input : JSON.stringify(ev.input ?? ''),
            }],
          }))
          return
        }
        if (type === 'tool_result') {
          patchIf(m => ({
            ...m,
            tools: (m.tools ?? []).map(t => t.callId && t.callId === ev.call_id
              ? { ...t, result: (ev.result as string) ?? '', status: ev.status as string } : t),
          }))
          return
        }
        if (type === 'final') {
          const ft = (ev.text as string) || current
          runTokens = (ev.tokens as number) || 0
          patchIf(m => ({ ...m, text: ft, tokens: runTokens || m.tokens }))
          return
        }
        if (type === 'error') {
          patchIf(m => ({ ...m, error: (ev.message as string) || '未知错误' }))
          return
        }
        if (type === 'done') {
          patchIf(m => ({ ...m, streaming: false }))
        }
      }, ctrl.signal)
      if (genRef.current === myGen) {
        setPhase('')
        setDetail(d => d ? { ...d, status: 'done', tokens_used: (d.tokens_used || 0) + runTokens } : d)
      }
    } catch (err) {
      const msg = (err as Error).message
      if ((err as Error).name === 'AbortError') {
        if (genRef.current === myGen) {
          toast('已停止等待（站娘可能仍在后台执行）')
          patchIf(m => ({ ...m, streaming: false, error: m.error ?? '已手动停止等待' }))
        }
      } else if (!streamStarted) {
        // 流未建立（配额/冲突/服务错误）：回滚乐观消息
        livePatch(list => (list.length >= 2 ? list.slice(0, -2) : []))
        if (genRef.current === myGen) setInput(text)
        if (createdHere && sid) {
          // 刚建的空会话一并清理，回到草稿
          api.del(`/api/sessions/${sid}`).catch(() => {})
          selfCreatedRef.current = null
          onSessionCreated(null)
        }
        // 409 = 上一轮还在收尾（服务端仍在等站娘进程退出），语义是「稍等」而非「失败」
        const status = err instanceof ApiError ? err.status : 0
        toast(status === 409
          ? '上一轮还在收尾，请等几秒再发送（会话状态会自动更新）'
          : status === 429 ? `额度不足：${msg}` : msg, 'err')
      } else {
        // 流已建立后中断：多为长静默被中间代理掐断，或站娘进程异常退出
        const friendly = /network error|Failed to fetch|NetworkError|terminated/i.test(msg)
          ? '连接中断（长任务静默超时或网络抖动）；已生成的内容已保留，可直接重发'
          : msg
        patchIf(m => ({ ...m, streaming: false, error: m.error ?? friendly }))
      }
      if (genRef.current === myGen) setPhase('')
    } finally {
      if (genRef.current === myGen) {
        busyRef.current = false
        setBusy(false)
        abortRef.current = null
      }
      onSessionChanged()
    }
  }

  const stop = () => abortRef.current?.abort()

  const title = detail?.title || '新会话'
  const running = detail?.status === 'running'

  return (
    <div className="ws-pane ws-chat">
      <div className="ws-pane-head">
        <h3 className="ws-chat-title">
          <span className={`ws-sess-dot ${running ? 'run' : ''}`} />
          {title}
          {detail && detail.tokens_used > 0 && (
            <span className="muted" style={{ fontSize: 11, fontWeight: 400 }}>
              {detail.tokens_used.toLocaleString()} tokens
            </span>
          )}
        </h3>
        {busy && <button className="small" onClick={stop}>停止</button>}
      </div>

      <div className="ws-chat-scroll" ref={scrollRef}>
        {msgs.length === 0 && (
          <div className="ws-chat-empty">
            <img src="/favicon.svg" alt="" style={{ width: 44, height: 44 }} />
            <h3>和助手聊聊吧</h3>
            <p>查资料、写文档、跑脚本、整理文件都可以。她记得你的偏好与约定，产出的文件会放在「我的文件」空间。</p>
            <div className="ws-suggest">
              {suggestions.map(s => (
                <button key={s} onClick={() => setInput(s)}>{s}</button>
              ))}
            </div>
          </div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={`chat-msg ${m.role}`}>
            {m.role === 'assistant' ? (
              <div className="ws-msg-main">
                {(m.thinking || (m.tools && m.tools.length > 0)) && (
                  <div className="ws-proc">
                    {m.thinking && (
                      <details className="ws-proc-card" open={!!m.streaming}>
                        <summary>思考过程</summary>
                        <pre>{m.thinking}</pre>
                      </details>
                    )}
                    {m.tools?.map((t, ti) => (
                      <details key={t.callId || ti} className="ws-proc-card tool"
                        open={!!m.streaming && t.result == null}>
                        <summary>
                          <span className={`ws-tool-dot ${t.status === 'error' ? 'fail' : t.result != null ? 'ok' : 'run'}`} />
                          工具 · {t.tool}
                        </summary>
                        {t.input && <pre className="ws-tool-io">{t.input}</pre>}
                        {t.result != null && <pre className="ws-tool-io">{t.result}</pre>}
                      </details>
                    ))}
                  </div>
                )}
                <div className={`bubble ${m.error ? 'ws-bubble-err' : ''}`}>
                  {m.text || (m.streaming ? '' : m.error ? '' : '（无文本回复）')}
                  {m.streaming && !m.text && <span className="ws-cursor" />}
                  {m.tokens ? <span className="ws-tokens">{m.tokens} tokens</span> : null}
                </div>
                {m.error && <div className="ws-err-line">⚠ {m.error}</div>}
              </div>
            ) : (
              <div className="bubble">{m.text}</div>
            )}
          </div>
        ))}
        {busy && phase && (
          <div className="chat-msg assistant"><div className="bubble typing">{phase}</div></div>
        )}
      </div>

      <div className="ws-chat-foot">
        {refs.length > 0 && (
          <div className="ws-ref-chips">
            {refs.map(r => (
              <span key={r} className="ws-ref-chip" title={`引用文件 ${r}`}>
                📄 {r}
                <a onClick={() => onRemoveRef(r)}>×</a>
              </span>
            ))}
          </div>
        )}
        <div className="chat-input">
          <textarea
            value={input}
            placeholder={busy
              ? '助手执行中…'
              : isTouch
                ? '输入消息，点「发送」提交（文件可在「文件」页引用）'
                : '输入消息，Enter 发送（Shift+Enter 换行），可从右侧引用文件作为上下文'}
            disabled={busy}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => {
              // 触屏上不拦截 Enter，否则手机的换行键会直接把消息发出去
              if (e.key === 'Enter' && !e.shiftKey && !isTouch) { e.preventDefault(); send() }
            }}
          />
          <button className="primary" disabled={busy || !input.trim()} onClick={send}>发送</button>
        </div>
      </div>
    </div>
  )
}
