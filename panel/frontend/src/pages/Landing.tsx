/**
 * 未登录落地页：网站介绍 + 使用教程（五步上手 / 进阶玩法 / 常见问题）。
 *
 * 内容依据（改版时请同步更新）：
 * - 2026-10-13 复盘 docs/retro-2026-10-13-trae-mcp.md（开发经验 / 错误对照表）
 * - 运维知识库 §M 长连接（SSE）规范、§K Lessons Learned
 * - 面板实际能力：工作站（会话即任务）、我的网盘、共享空间、我的设备、我的文件
 *
 * 注意：本页只介绍**普通用户可用**的功能，不出现管理端内容。
 */

export function Landing() {
  return (
    <div className="landing">
      <header className="l-hero">
        <nav className="l-nav">
          <div className="l-logo">
            <img src="/favicon.svg" alt="" />
            <span className="zh">铃枢</span>
            <span className="en">BellHub</span>
          </div>
          <a className="l-btn ghost small" href="/login">登录</a>
        </nav>
        <div className="l-hero-body">
          <div className="l-halo" />
          <h1>铃枢 <span className="l-en">BellHub</span></h1>
          <p className="l-slogan">
            摇响铃铛，你的专属 AI 站娘「助手」即刻上线——会聊天、能干活、全天候待命。
          </p>
          <div className="l-hero-actions">
            <a className="l-btn primary" href="/login">立即登录</a>
            <a className="l-btn ghost" href="#start">开始使用</a>
          </div>
        </div>
      </header>

      <section className="l-section" id="about">
        <h2>这是什么？</h2>
        <p className="l-lead">
          铃枢（BellHub）是基于 DeepSeek Harness 打造的站娘协作平台：
          一位有独立人格与长期记忆的站娘在线待命，一个可视化工作台替你打理对话、文件与设备，
          每次对话都是一次可追溯的任务，全程沙箱隔离。
        </p>
        <div className="l-cards four">
          <div className="l-card">
            <div className="l-ico">{icoBot}</div>
            <h3>专属站娘「助手」</h3>
            <p>
              拥有独立人格与长期记忆，跨会话记得你的偏好与约定。写代码、跑脚本、查资料、整理文件都在行，
              而且她的思考过程与工具调用都会摊开给你看——不是黑箱。
            </p>
          </div>
          <div className="l-card">
            <div className="l-ico">{icoPanel}</div>
            <h3>三栏工作站</h3>
            <p>
              左栏会话、中栏流式对话、右栏文件树。像用 IDE 一样和站娘协作：随时引用工作区的文件作为上下文，
              她的产出你能立刻看到、随时追问。
            </p>
          </div>
          <div className="l-card">
            <div className="l-ico">{icoCloud}</div>
            <h3>网盘与共享</h3>
            <p>
              「我的网盘」支持大文件分片上传与断点续传，中断了不用从头再来；要给别人就生成分享链接，
              同站登录用户即可取用。
            </p>
          </div>
          <div className="l-card">
            <div className="l-ico">{icoShield}</div>
            <h3>安全沙箱</h3>
            <p>
              每个账号独立工作目录、独立磁盘配额，严格防御路径穿越；任务与敏感操作全程留痕，
              你的数据只属于你。
            </p>
          </div>
        </div>
      </section>

      <section className="l-section alt" id="start">
        <h2>五步上手</h2>
        <p className="l-lead">从零开始，五步摇响铃铛。</p>
        <div className="l-steps">
          <div className="l-step"><span className="no">1</span><div>
            <h3>找管理员开通账号</h3>
            <p>铃枢采用邀请制。联系管理员说明用途，领取你的专属账号——默认每日 10 万 token、
              5 个并发会话、1GB 文件空间，管理员可按需调整。</p>
          </div></div>
          <div className="l-step"><span className="no">2</span><div>
            <h3>登录铃枢</h3>
            <p>输入账号密码，进入属于你的工作台。若拿到的是初始密码，请先到「设置」页改掉。</p>
          </div></div>
          <div className="l-step"><span className="no">3</span><div>
            <h3>在新会话里说人话</h3>
            <p>进入「工作站」，左栏点「＋ 新建会话」，中栏写下你的需求，Enter 发送（Shift+Enter 换行）。
              会话标题会自动取自你的第一句话，一个会话就是一段持续的任务。</p>
          </div></div>
          <div className="l-step"><span className="no">4</span><div>
            <h3>看站娘流式干活</h3>
            <p>回复是流式吐出来的：她的思考、每一次工具调用都以可折叠卡片展示，点开就知道她读了哪个文件、
              跑了什么命令。想让她参考现有文件，输入 <code>@</code> 或点右栏文件行的「@」即可引用。</p>
          </div></div>
          <div className="l-step"><span className="no">5</span><div>
            <h3>收获成果</h3>
            <p>产出文件直接落在「我的文件」里，可在线预览、编辑、下载；要分享或存放较大的资料就放进
              「我的网盘」。同一个会话可以继续追问，她带着上下文接着干。</p>
          </div></div>
        </div>
      </section>

      <section className="l-section" id="advanced">
        <h2>进阶玩法</h2>
        <p className="l-lead">基础之外，还有更大的世界。</p>
        <div className="l-cards four">
          <div className="l-card">
            <div className="l-ico">{icoLaptop}</div>
            <h3>把自己的设备接进来</h3>
            <p>
              在「我的设备」页添加设备、下载 agent，你的电脑或手机就能被站娘远程派活。
              默认处理器只回显指令、不执行任何操作——真正的权限边界始终握在你手里。
            </p>
          </div>
          <div className="l-card">
            <div className="l-ico">{icoNodes}</div>
            <h3>接进你的 AI 客户端</h3>
            <p>
              铃枢提供 MCP 桥接脚本，可把站娘的设备工具接进 Trae 等支持 MCP 的客户端；
              登录后在「我的设备」页底部即可下载（配置与密钥说明见该页）。
            </p>
          </div>
          <div className="l-card">
            <div className="l-ico">{icoFolder}</div>
            <h3>大文件与分享</h3>
            <p>
              超过 8MB 的文件自动走分片上传，断线后可从断点继续，半成品保留 7 天；
              分享链接仅同站登录用户可打开，随时可撤销。
            </p>
          </div>
          <div className="l-card">
            <div className="l-ico">{icoStar}</div>
            <h3>她记得你</h3>
            <p>
              站娘拥有按用户隔离的长期记忆：互动积累到一定条数会自动提炼成摘要，
              下次对话她会带着这些记忆继续。你可以在「设置」页查看、编辑或清空她记下的一切。
            </p>
          </div>
        </div>
      </section>

      <section className="l-section alt" id="faq">
        <h2>常见问题</h2>
        <p className="l-lead">新手最容易卡住的几件事，先看这里。</p>
        <div className="l-faq">
          <details>
            <summary>如何获得账号？</summary>
            <p>铃枢目前采用管理员邀请制。联系管理员说明用途即可开通专属账号，默认每日 10 万 token、
              5 个并发会话、1GB 文件空间，管理员可按需调整。</p>
          </details>
          <details>
            <summary>站娘「助手」能做什么？</summary>
            <p>日常聊天与陪伴；编写与运行代码；整理、转换与生成文件；在她自己的工作目录里执行运维类操作；
              若管理员装了联网工具，她还能帮你查资料。需要产出的任务，建议直接说明文件名与格式。</p>
          </details>
          <details>
            <summary>她好久不说话，是卡住了吗？</summary>
            <p>通常不是。站娘的思考与工具执行是<b>整块</b>输出的：她跑一段脚本、读一批文件时，
              界面可能几分钟都不出新字，这种「长静默」属于正常现象。看会话标题旁的状态圆点——
              还在转就说明她还在干活，别急着刷新页面。</p>
          </details>
          <details>
            <summary>发送时提示「上一轮还在收尾」？</summary>
            <p>这是说这个会话的上一轮还没真正结束（服务端要等站娘进程退出才算完），并不是故障。
              等几秒再发送即可，会话状态会自动更新。</p>
          </details>
          <details>
            <summary>对话中途提示「连接中断」？</summary>
            <p>多半是长任务静默期间被网络链路按空闲超时切断，或本地网络抖动。铃枢会定时发送心跳包保活，
              但公网链路偶尔仍会抖动。<b>已经生成的内容会保留</b>，直接重发那句话就行；
              任务特别大时，拆成几步会更稳。</p>
          </details>
          <details>
            <summary>「停止」按钮停的是什么？</summary>
            <p>停的是「你这边继续等待」，站娘的后台任务可能仍在跑完并把文件写好。
              停止后重新打开该会话，往往能看到完整结果。</p>
          </details>
          <details>
            <summary>配额是怎么算的？超额了怎么办？</summary>
            <p>三种配额各自独立：<b>token</b> 按当日累计，用完当天不能继续对话，次日零点自动重置；
              <b>并发会话</b>限制同一时刻正在执行的会话数（默认 5），等前一个跑完就释放；
              <b>磁盘</b>限制你的文件空间，到「我的文件」清理即可。额度不够可联系管理员调整。</p>
          </details>
          <details>
            <summary>「我的文件」和「我的网盘」有什么区别？</summary>
            <p>「我的文件」是站娘的工作沙箱，她读写与产出都在这里，适合边做边看；
              「我的网盘」是给你存放与分享的仓库，支持大文件分片上传与断点续传，可以生成分享链接。
              两者各自独立计配额。</p>
          </details>
          <details>
            <summary>我的数据安全吗？</summary>
            <p>每个账号拥有独立工作目录，严格隔离并防御路径穿越；站娘执行任务的工作目录就是你的空间。
              任务与敏感操作均记录审计日志。<b>提醒</b>：设备 agent 的 token 等价于在你设备上下指令的权限，
              请像保管密码一样保管它，不要写进会被提交的代码或公开贴出。</p>
          </details>
          <details>
            <summary>忘记密码怎么办？</summary>
            <p>联系管理员重置密码；重置后请尽快在「设置」页修改。</p>
          </details>
        </div>
      </section>

      <footer className="l-footer">
        <div>
          <img src="/favicon.svg" alt="" /> 铃枢 BellHub · DSH Suite
        </div>
        <div className="muted">© 2026 agent.example.com · <a href="/login">登录入口</a></div>
      </footer>
    </div>
  )
}

const icoBot = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <rect x="5" y="8" width="14" height="11" rx="3" />
    <path d="M12 8V4M9 4h6" />
    <circle cx="9.5" cy="13" r="1.2" fill="currentColor" stroke="none" />
    <circle cx="14.5" cy="13" r="1.2" fill="currentColor" stroke="none" />
    <path d="M9.5 16.5h5" />
  </svg>
)
const icoPanel = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="4" width="18" height="14" rx="2.5" />
    <path d="M3 9h18M8 21h8M12 18v3" />
  </svg>
)
const icoCloud = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <path d="M7 18h9.5a3.5 3.5 0 0 0 .4-6.98A5.5 5.5 0 0 0 6.4 10.6A3.7 3.7 0 0 0 7 18z" />
    <path d="M12 16.5v-5M9.9 13.4L12 11.3l2.1 2.1" />
  </svg>
)
const icoShield = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z" />
    <path d="M9 12l2 2 4-4" />
  </svg>
)
const icoLaptop = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3.5" y="5" width="17" height="11" rx="2" />
    <path d="M2 19h20M9.5 9.5l-1.8 1.6 1.8 1.6M14.5 9.5l1.8 1.6-1.8 1.6" />
  </svg>
)
const icoNodes = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="5" r="2.5" />
    <circle cx="5" cy="18" r="2.5" />
    <circle cx="19" cy="18" r="2.5" />
    <path d="M10.7 7.2L6.2 15.8M13.3 7.2l4.5 8.6M7.5 18h9" />
  </svg>
)
const icoFolder = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
  </svg>
)
const icoStar = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 3.6l2.6 5.3 5.8.8-4.2 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8L3.6 9.7l5.8-.8z" />
  </svg>
)
