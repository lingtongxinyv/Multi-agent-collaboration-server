# 参与开发

## 先跑起来再改

```bash
bash tests/run_all.sh
```

18 套 / 561 项，全绿再动手。这个仓库的测试是**真起服务、走真 HTTP**，不 mock ——
看起来对的改动经常真的不通，测试会告诉你。

## 提 PR 前

1. `bash tests/run_all.sh` 全绿
2. 改了前端要 `npm run build`（有两条套件核对构建产物）
3. 新增环境变量 → 同步 `docs/configuration.md` 与 `.env.example`
4. 新增接口 → 同步 `docs/`（有 `test_docs_drift.py` 会核对 TSX 里的命令是否进了产物）

## 写测试的约定

- 每套用隔离临时库：`os.environ["DSH_SUITE_DATA"] = tempfile.mkdtemp(...)`，
  并立刻断言 `DB_PATH` 在该目录下（**绝不碰真实 panel.db**）
- 路径用仓库相对定位，不写 `/home/xxx` 绝对路径
- 断言前先读实现分支：产品常常比想象更严
- 清理自己的残留

## 代码风格

- Python：3.12，类型标注尽量写，`from __future__ import annotations`
- 注释写**为什么**，不写「这行在干什么」
- 中文注释没问题，但公开接口的描述用中英都能读懂的话
- 提交信息：`type(scope): 一句话`，type 用 `feat/fix/docs/test/chore/refactor`

## 不要提交

- `.env`、`data/`、任何 token / 私钥 / 证书
- `workers/*.config.json`（含 agent token）
- 真实域名、QQ 号、内网 IP、个人路径
- 构建产物（`dist/`、单文件二进制）

## 安全相关

涉及认证、路径、命令执行的改动，PR 描述里写清楚威胁模型与验证方式。
拿不准就先开 issue 讨论，别直接合。
