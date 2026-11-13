/**
 * 登录后主页：欢迎、配额总览、功能矩阵、五步使用说明、排障速查、常见问题。
 *
 * 「排障速查」的每一条都来自真实故障的处置经验（2026-10-13 复盘 + 运维知识库 §M）：
 * 现象 → 它到底是什么意思 → 该怎么办。新增条目时请保持这三段式。
 */

import { useAuth } from '../main'

/** 排障速查：现象 / 语义 / 处置 */
const TROUBLE: { sym: string; why: string; fix: string }[] = [
  {
    sym: '发送时提示「上一轮还在收尾」',
    why: '这个会话的上一轮还没真正结束——服务端要等站娘进程退出才算完。它的语义是「稍等」，不是「失败」。',
    fix: '等几秒再发送即可，会话状态会自动收敛；反复出现说明上一轮任务确实很重，稍等一会儿。',
  },
  {
    sym: '界面长时间没有新字',
    why: '站娘的思考与工具调用是整块输出的。她跑脚本、读大文件时不会逐字吐字，几分钟静默属于正常。',
    fix: '看会话标题旁的状态圆点：还在转就是在干活。不要刷新页面，刷新不会让她更快。',
  },
  {
    sym: '中途提示「连接中断 / network error」',
    why: '长任务静默期间被网络链路按空闲超时切断，或本地网络抖动。铃枢每 15 秒发一次心跳保活，但公网链路偶尔仍会抖动。',
    fix: '已生成的内容都保留着，直接重发那句话；任务很大时拆成几步，会比一口气跑更稳。',
  },
  {
    sym: '点了「停止」，文件后来还是出现了',
    why: '「停止」停的是你这边继续等待，站娘的后台任务可能仍在跑完——这是设计如此。',
    fix: '重新打开该会话，通常能看到完整结果。',
  },
  {
    sym: '提示配额不足（token / 并发 / 磁盘）',
    why: '三种配额各自独立：token 按当日累计、并发限制同时执行的会话数（默认 5）、磁盘限制你的文件空间。',
    fix: 'token 次日零点自动重置；并发等前一个会话跑完即释放；磁盘到「我的文件」清理。都不够可找管理员调整。',
  },
  {
    sym: '引用了大文件后变慢或被拒',
    why: '被引用的文件内容会拼进这次提问一起发给模型，越大的文件越占 token。',
    fix: '只引用相关的那几个文件或片段；二进制文件先让站娘转换或摘录再引用。单条消息上限 10 万字符。',
  },
  {
    sym: '设备一直「离线」或指令停在「待取走」',
    why: '设备上的 agent 没在跑、配置不一致、设备休眠，或网络不通。指令要等下一次心跳才会被取走。',
    fix: '按「我的设备」页的接入教程逐项检查（进程、config.json 的 name/token、能否访问站点）；设备唤醒后会自动恢复。',
  },
  {
    sym: '设备指令长时间停在「执行中」',
    why: '设备已取走指令，handler 还在跑或已异常退出。单条 handler 的最长执行时间是 30 分钟。',
    fix: '用内置指令 !info / !ps 远程体检（详见「我的设备」页），必要时重启设备上的 agent。',
  },
]

export function HomePage() {
  const { me } = useAuth()
  const name = me?.display_name || me?.username || ''
  const isAdmin = me?.role === 'admin'
  const quota = me?.quota

  return (
    <>
      <h2>你好，{name} ♪</h2>

      <div className="card home-hero">
        <div>
          <h3>欢迎回到铃枢 BellHub</h3>
          <p className="muted">
            这里是你的站娘协作工作台：在「工作站」里和站娘「助手」流式对话，她会实时回复、调用工具、
            把文件产出到你的专属沙箱；右栏资源管理器随时浏览与引用文件。
            文件要分享或存大件就去「我的网盘」，想让电脑、手机也被她派活就去「我的设备」。
          </p>
        </div>
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <a className="l-btn primary small" href="/workstation">进入工作站</a>
          <a className="l-btn ghost small" href="/files">查看我的文件</a>
        </div>
      </div>

      {quota && (
        <div className="grid cols4" style={{ marginBottom: 16 }}>
          <div className="stat">
            <div className="label">今日 token 用量</div>
            <div className={quota.used_tokens > quota.tokens_daily * 0.9 ? 'value warn' : 'value'}>
              {quota.used_tokens.toLocaleString()}<small className="muted"> / {quota.tokens_daily.toLocaleString()}</small>
            </div>
          </div>
          <div className="stat">
            <div className="label">并发会话上限</div>
            <div className="value">{quota.concurrent}<small className="muted"> 个</small></div>
          </div>
          <div className="stat">
            <div className="label">文件空间</div>
            <div className={quota.used_disk_mb > quota.disk_mb * 0.9 ? 'value warn' : 'value'}>
              {quota.used_disk_mb.toFixed(1)}<small className="muted"> / {quota.disk_mb} MB</small>
            </div>
          </div>
          <div className="stat">
            <div className="label">身份</div>
            <div className="value" style={{ fontSize: 16 }}>{isAdmin ? '管理员' : '普通用户'}</div>
          </div>
        </div>
      )}

      <div className="card">
        <h3>功能地图 · 你能在这里做什么</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          铃枢（BellHub）是基于 DeepSeek Harness 打造的站娘协作平台：
          一位拥有独立人格与记忆的站娘在线待命，一个可视化工作台管理对话、文件与设备，全程沙箱隔离、审计可追溯。
        </p>
        <div className="l-cards" style={{ marginTop: 14 }}>
          <div className="l-card">
            <h3>工作站</h3>
            <p>三栏对话式工作台：左栏管理会话（新建/重命名/置顶/删除）、中栏与站娘流式对话（实时回复 + 可折叠的思考与工具调用卡片）、右栏 IDE 风格文件树资源管理器。</p>
            <p className="muted" style={{ fontSize: 12.5 }}>入口：导航菜单「工作站」</p>
          </div>
          <div className="l-card">
            <h3>我的网盘</h3>
            <p>存放与分享资料的仓库：支持拖拽上传、图片与文本在线预览、重命名与删除。超过 8MB 的文件自动分片上传，断线后可从断点继续（半成品保留 7 天），还能生成分享链接。</p>
            <p className="muted" style={{ fontSize: 12.5 }}>入口：导航菜单「我的网盘」</p>
          </div>
          <div className="l-card">
            <h3>共享空间</h3>
            <p>集中查看全站仍然有效的分享：谁分享的、哪个文件、何时过期一目了然，自己发出的分享可随时撤销。出于安全考虑，分享仅对已登录用户开放。</p>
            <p className="muted" style={{ fontSize: 12.5 }}>入口：导航菜单「共享空间」</p>
          </div>
          <div className="l-card">
            <h3>我的设备</h3>
            <p>把你的电脑、手机接入铃枢：设备上跑一个零依赖的 agent，站娘就能远程给它派活并取回结果。默认处理器只回显，不执行任何操作——权限边界在你手里。</p>
            <p className="muted" style={{ fontSize: 12.5 }}>入口：导航菜单「我的设备」（含接入教程）</p>
          </div>
          <div className="l-card">
            <h3>我的智能体</h3>
            <p>把本机已装的 AI 应用编入站娘的队伍：下一次「铃枢连接器」，浏览器里点一次确认就上线。
              站娘把活派给本机 AI 执行（连接器本身不代跑命令），涉及写文件或跑命令时会在本机二次确认。
              每个账号各自一份凭据，互不可见。</p>
            <p className="muted" style={{ fontSize: 12.5 }}>入口：导航菜单「我的智能体」（四步接入向导）</p>
          </div>
          <div className="l-card">
            <h3>我的文件</h3>
            <p>站娘产出的所有文件都落在你的独立沙箱里，这里集中浏览、在线编辑、新建与删除文件，支持子目录导航。每个账号的沙箱相互隔离，互不干扰。</p>
            <p className="muted" style={{ fontSize: 12.5 }}>入口：导航菜单「我的文件」</p>
          </div>
          <div className="l-card">
            <h3>设置</h3>
            <p>修改登录密码、查看今日 token 与磁盘配额用量、管理站娘对你的长期记忆——可查看、编辑或清空她记下的偏好与约定。</p>
            <p className="muted" style={{ fontSize: 12.5 }}>入口：导航菜单左下角「设置」</p>
          </div>
        </div>
      </div>

      <div className="card">
        <h3>五步上手 · 从新建会话到收获成果</h3>
        <div className="l-steps" style={{ marginTop: 8 }}>
          <div className="l-step"><span className="no">1</span><div>
            <h3>新建会话</h3>
            <p>进入「工作站」，左栏顶部点「＋ 新建会话」。每个会话既是一次任务也是一段对话，标题会自动取自你发的第一条消息，可右键重命名、置顶或删除。</p>
          </div></div>
          <div className="l-step"><span className="no">2</span><div>
            <h3>发送消息</h3>
            <p>在中栏输入框打字，Enter 发送（Shift+Enter 换行）。站娘会<strong>流式实时回复</strong>，
              思考过程与工具调用以可折叠卡片展示，展开可看详情。她在跑脚本、读文件时会安静一会儿，那是正常的长静默。</p>
          </div></div>
          <div className="l-step"><span className="no">3</span><div>
            <h3>引用文件做上下文</h3>
            <p>输入框输入 <code>@</code> 可触发文件选择，或点右栏文件行的「@」按钮，
              把沙箱里的文件引用为这次提问的上下文。站娘会读取被引用文件的内容一起发给模型，回答更精准——
              只引用相关文件即可，引用越多越费 token。</p>
          </div></div>
          <div className="l-step"><span className="no">4</span><div>
            <h3>查看产出文件</h3>
            <p>站娘生成的文件直接落到你的沙箱，右栏资源管理器展开目录即可看到，点击文件浮层预览；
              也可去「我的文件」页在线编辑、新建或下载，要分享就放到「我的网盘」生成链接。</p>
          </div></div>
          <div className="l-step"><span className="no">5</span><div>
            <h3>管理会话与任务</h3>
            <p>左栏会话项右键弹出菜单：重命名、置顶、删除。会话按最后更新时间倒序排列，置顶的排最前。
              一轮回复执行中可点右上角「停止」不再等待（站娘可能仍在后台完成），每轮对话计入 token 配额，并发会话数受配额限制。</p>
          </div></div>
        </div>
      </div>

      <div className="card">
        <h3>排障速查 · 这些情况其实都不是故障</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          下面每一条都来自真实的用户疑问：先看「它是什么意思」，再决定要不要动手。
        </p>
        <div style={{ display: 'grid', gap: 14, marginTop: 12 }}>
          {TROUBLE.map(t => (
            <div key={t.sym} style={{ borderLeft: '3px solid var(--accent2)', paddingLeft: 12 }}>
              <b>{t.sym}</b>
              <div className="muted" style={{ fontSize: 13, marginTop: 3, lineHeight: 1.8 }}>
                <span style={{ opacity: 0.75 }}>为什么：</span>{t.why}
              </div>
              <div style={{ fontSize: 13, marginTop: 3, lineHeight: 1.8 }}>
                <span className="muted" style={{ opacity: 0.75 }}>怎么办：</span>{t.fix}
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="card">
        <h3>常见问题</h3>
        <div className="l-faq">
          <details>
            <summary>站娘能记住我多久之前说的话？</summary>
            <p>站娘有按用户隔离的长期记忆：每次对话都会自动记录，超过一定条数后会把最旧的互动提炼压缩进长期记忆摘要。
              下次对话时她会把这些记忆注入上下文，所以跨会话也能想起你的偏好与约定。可在「设置」页查看或编辑她的记忆。</p>
          </details>
          <details>
            <summary>一个会话里能多轮追问吗？</summary>
            <p>可以。同一会话里多发几条消息，站娘会带着上下文续接对话，每轮都流式实时回复。每个会话相当于一次持续的任务，
              状态、token 用量、产出文件都挂在这个会话上。</p>
          </details>
          <details>
            <summary>「我的文件」和「我的网盘」该把东西放哪？</summary>
            <p>「我的文件」是站娘的工作沙箱——她读写、产出都在这里，适合边做边看；
              「我的网盘」是存放与分享的仓库，支持大文件分片上传与断点续传，可生成分享链接给同站登录用户。
              两者独立计算配额。</p>
          </details>
          <details>
            <summary>今日 token 配额用完了怎么办？</summary>
            <p>每轮对话会按实际用量计入当日配额，超额后新的对话会被拒绝（提示配额不足）。次日零点自动重置。
              管理员可在「账号管理」页为单个账号调整配额。</p>
          </details>
          <details>
            <summary>并发会话数限制是什么？</summary>
            <p>同一时刻你最多能有 N 个会话处于「流式执行中」状态（默认 5），由配额的并发上限控制。
              一个会话流式回复完成后会释放并发槽，可继续发新会话。避免短时间开太多会话同时跑。</p>
          </details>
          <details>
            <summary>引用文件有大小限制吗？</summary>
            <p>引用文件的内容会拼到提问正文前发给模型，过大文件会占用 token 配额。建议引用文本类文件（代码、文档、配置），
              二进制文件请先用站娘转换或摘录。单条消息总长度上限 10 万字符。</p>
          </details>
          <details>
            <summary>我的数据安全吗？</summary>
            <p>每个账号的沙箱目录相互隔离，站娘执行任务时的工作目录就是你的沙箱，严格防御路径穿越；
              所有任务与敏感操作都记录审计日志，管理员可追溯。
              <b>提醒</b>：设备 agent 的 token 等价于「在你设备上下指令」的权限，
              请像保管密码一样保管，不要写进会被提交的代码或公开贴出；怀疑泄露就删掉设备重新添加。</p>
          </details>
          <details>
            <summary>忘记密码怎么办？</summary>
            <p>联系管理员重置密码；重置后请在「设置」页尽快修改。</p>
          </details>
        </div>
      </div>

      {isAdmin && (
        <div className="card" style={{ background: 'linear-gradient(120deg, #ffffff 30%, #e8f5ff)' }}>
          <div className="row" style={{ alignItems: 'center', justifyContent: 'space-between' }}>
            <div>
              <h3 style={{ margin: '0 0 4px' }}>管理员入口</h3>
              <span className="muted">你以管理员身份登录，可进入独立的管理控制台管理账号、监控、站娘人格等。</span>
            </div>
            <a className="l-btn primary small" href="/admin">进入管理控制台 ↗</a>
          </div>
        </div>
      )}
    </>
  )
}
