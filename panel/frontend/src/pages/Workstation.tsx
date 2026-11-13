/** 工作站：TRAE 式三栏布局——左栏会话列表、中栏对话流（SSE 流式）、右栏文件引用。
 *  窄屏（≤1100px）三栏改为三个页签，只显示当前一栏；三栏都保持挂载，
 *  切换页签不会丢失对话状态与滚动位置（CSS 在 styles.css 的 .ws-tabs 段）。 */

import { useCallback, useState } from 'react'
import { SessionPane } from '../components/SessionPane'
import { ChatStream } from '../components/ChatStream'
import { ExplorerPane } from '../components/ExplorerPane'

type WsTab = 'sessions' | 'chat' | 'files'

export function WorkstationPage() {
  const [activeId, setActiveId] = useState<string | null>(null)
  const [refs, setRefs] = useState<string[]>([])
  const [listVersion, setListVersion] = useState(0)
  const [tab, setTab] = useState<WsTab>('chat')

  const toggleRef = useCallback((path: string) => {
    setRefs(list => list.includes(path) ? list.filter(p => p !== path) : [...list, path])
  }, [])

  // 在手机上点会话后直接跳到「对话」页签
  const selectSession = useCallback((id: string | null) => {
    setActiveId(id)
    setTab('chat')
  }, [])

  return (
    <div className={`ws tab-${tab}`}>
      <div className="ws-tabs" role="tablist" aria-label="工作站面板">
        <button role="tab" aria-selected={tab === 'sessions'}
          className={tab === 'sessions' ? 'on' : ''} onClick={() => setTab('sessions')}>会话</button>
        <button role="tab" aria-selected={tab === 'chat'}
          className={tab === 'chat' ? 'on' : ''} onClick={() => setTab('chat')}>对话</button>
        <button role="tab" aria-selected={tab === 'files'}
          className={tab === 'files' ? 'on' : ''} onClick={() => setTab('files')}>
          文件{refs.length > 0 && <span className="ws-tab-badge">{refs.length}</span>}
        </button>
      </div>
      <aside className="ws-col ws-left">
        <SessionPane activeId={activeId} onSelect={selectSession} listVersion={listVersion} />
      </aside>
      <section className="ws-col ws-mid">
        <ChatStream
          sessionId={activeId}
          refs={refs}
          onRemoveRef={toggleRef}
          onSessionCreated={selectSession}
          onSessionChanged={() => setListVersion(v => v + 1)}
        />
      </section>
      <aside className="ws-col ws-right">
        <ExplorerPane refs={refs} onToggleRef={toggleRef} />
      </aside>
    </div>
  )
}
