"""【重测 / 手动】大文件中断续传与吞吐实测（隔离服务，可选规模）。

⚠ 刻意用 `heavy_` 前缀且**不列入** tests/run_all.sh —— 它按设计要跑大文件，
   面板那个"一键自检"不该被它拖慢。想跑时手动执行：

    BELLHUB_HEAVY_MB=32 ~/dsh-suite/.venv/bin/python tests/heavy_resume_large.py
    # 不设环境变量则默认 100MB（原实测口径）

验证点（与 2026-10-13 首次实测同一套）：
  1. 中断后重新 init 必须命中同一会话（resumed=True）
  2. 续传时**不重传**已完成的片（用服务端 received 增量证明）
  3. 最终下载回来 sha256 与源一致

用法：~/dsh-suite/.venv/bin/python tests/heavy_resume_large.py
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "panel" / "backend"

ROOT = Path(tempfile.mkdtemp(prefix="heavyresume-"))
os.environ["DSH_SUITE_DATA"] = str(ROOT / "data")
os.environ["DSH_DRIVE_ROOT"] = str(ROOT / "users")
PORT = 8095
BASE = f"http://127.0.0.1:{PORT}"
CHUNK = 8 * 1024 * 1024
SIZE = int(os.environ.get("BELLHUB_HEAVY_MB", "100")) * 1024 * 1024
FIRST_WANT = 4                           # 想先传几片再"断线"（片数不够时自动缩小）

sys.path.insert(0, str(BACKEND))
from app import auth as A, db as D  # noqa: E402

assert str(D.DB_PATH).startswith(str(ROOT)), f"隔离失败: {D.DB_PATH}"
from app import drive as dr  # noqa: E402

assert str(dr.DRIVE_USERS_ROOT).startswith(str(ROOT)), f"隔离失败: {dr.DRIVE_USERS_ROOT}"

acc = D.get_db().create_account("heavy-admin", "pw-heavy-1", "admin", "重测管理员")
COOKIE = A._encode({"uid": acc["id"], "role": acc["role"],
                    "exp": time.time() + 3600, "iat": time.time()})
HDRS = {"Cookie": f"panel_session={COOKIE}"}
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  → {detail}" if detail and not cond else ""))


def req(method, path, json_body=None, data=None, headers=None, raw=False):
    h = dict(HDRS)
    h.update(headers or {})
    body = data
    if json_body is not None:
        body = json.dumps(json_body).encode()
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=300) as resp:
            rb = resp.read()
            return resp.status, (rb if raw else (json.loads(rb) if rb else None))
    except urllib.error.HTTPError as e:
        rb = e.read()
        try:
            return e.code, json.loads(rb)
        except Exception:                               # noqa: BLE001
            return e.code, rb.decode("utf-8", "replace")[:200]


def multipart(fields, files):
    b = "----resumetest"
    parts = []
    for k, v in fields.items():
        parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for k, (fn, content) in files.items():
        parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\n"
                     f"Content-Type: application/octet-stream\r\n\r\n".encode() + content + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"


srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                        "--port", str(PORT), "--log-level", "warning"],
                       cwd=str(BACKEND), env={**os.environ},
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
for _ in range(60):
    try:
        urllib.request.urlopen(BASE + "/api/drive/list", timeout=1)
        break
    except urllib.error.HTTPError:
        break
    except Exception:
        time.sleep(0.5)

try:
    print(f"① 造 {SIZE // 1048576}MB 测试数据并算 sha256")
    data = os.urandom(SIZE)
    sha = hashlib.sha256(data).hexdigest()
    expect = (SIZE + CHUNK - 1) // CHUNK
    # 至少留 1 片给"续传"阶段：小规模（如 BELLHUB_HEAVY_MB=24）时自动缩小首批
    FIRST = min(FIRST_WANT, max(1, expect - 1))
    print(f"   sha256 {sha[:24]}…｜分片 {CHUNK // 1048576}MB → 共 {expect} 片｜"
          f"先传 {FIRST} 片再断线")

    req("POST", "/api/drive/mkdir", json_body={"path": "__resume_test__"})
    target = "__resume_test__/large.bin"

    print("\n② 第一次 init（新会话）")
    st, s1 = req("POST", "/api/drive/upload/init",
                 json_body={"path": target, "size": SIZE, "sha256": sha, "chunk_size": CHUNK})
    check("init 200", st == 200, f"{st} {str(s1)[:120]}")
    sid = s1["sid"]
    check("resumed=False（首次）", s1["resumed"] is False)
    check(f"总片数={expect}", s1["total_chunks"] == expect, str(s1["total_chunks"]))

    print(f"\n③ 传前 {FIRST} 片，然后模拟断线")
    t0 = time.time()
    r = {}
    for i in range(FIRST):
        piece = data[i * CHUNK:(i + 1) * CHUNK]
        body, ct = multipart({"sid": sid, "index": str(i)}, {"data": (f"{i}.part", piece)})
        st, r = req("POST", "/api/drive/upload/chunk", data=body, headers={"Content-Type": ct})
        if st != 200:
            check(f"第 {i} 片上传", False, f"{st} {str(r)[:100]}")
            break
    elapsed = time.time() - t0
    print(f"   已传 {FIRST} 片（{FIRST * CHUNK // 1048576}MB），耗时 {elapsed:.1f}s，"
          f"等效 {FIRST * CHUNK / 1048576 / max(elapsed, 1e-6):.1f} MB/s")

    st, stt = req("GET", f"/api/drive/upload/status?sid={sid}")
    check(f"服务端确认已收 {FIRST} 片", stt.get("received") == FIRST, str(stt.get("received")))
    check("已收分片序号正确", stt.get("uploaded") == list(range(FIRST)), str(stt.get("uploaded")))

    print("\n④ 【关键】重新 init —— 模拟用户重新打开页面/重传同一文件")
    st, s2 = req("POST", "/api/drive/upload/init",
                 json_body={"path": target, "size": SIZE, "sha256": sha, "chunk_size": CHUNK})
    check("命中同一会话（sid 相同）", s2.get("sid") == sid, f"{s2.get('sid')} vs {sid}")
    check("resumed=True", s2.get("resumed") is True)
    check(f"服务端记住已传 {FIRST} 片", s2.get("uploaded") == list(range(FIRST)),
          str(s2.get("uploaded")))

    print(f"\n⑤ 续传剩余 {expect - FIRST} 片（应只传缺失的片，不重传）")
    t1 = time.time()
    for i in range(FIRST, expect):
        piece = data[i * CHUNK:(i + 1) * CHUNK]
        body, ct = multipart({"sid": sid, "index": str(i)}, {"data": (f"{i}.part", piece)})
        st, r = req("POST", "/api/drive/upload/chunk", data=body, headers={"Content-Type": ct})
        if st != 200:
            check(f"续传第 {i} 片", False, f"{st} {str(r)[:100]}")
            break
    elapsed1 = time.time() - t1
    print(f"   续传 {expect - FIRST} 片（{(expect - FIRST) * CHUNK // 1048576}MB），"
          f"耗时 {elapsed1:.1f}s")
    check(f"续传完成全部 {expect} 片", st == 200 and r.get("received") == expect, str(r))

    print("\n⑥ 拼接完成")
    st, done = req("POST", "/api/drive/upload/complete", json_body={"sid": sid})
    check("complete 200", st == 200, f"{st} {str(done)[:150]}")
    check("返回大小一致", done.get("size") == SIZE, str(done.get("size")))

    print("\n⑦ 下载并校验哈希（端到端正确性）")
    st, got = req("GET", f"/api/drive/download?path={urllib.parse.quote(target)}", raw=True)
    check("下载 200", st == 200, f"{st}")
    got_sha = hashlib.sha256(got).hexdigest() if isinstance(got, bytes) else "?"
    check("sha256 与源文件一致", got_sha == sha, f"{got_sha[:24]} vs {sha[:24]}")

    print("\n⑧ 会话清理与残留检查")
    st, _ = req("GET", f"/api/drive/upload/status?sid={sid}")
    check("会话已删除（404）", st == 404, f"{st}")
    st, r = req("POST", "/api/drive/delete", json_body={"paths": ["__resume_test__"]})
    check("删除测试目录", st == 200 and r.get("deleted", 0) >= 1, f"{st} {r}")
    st, r = req("GET", "/api/drive/list")
    check("配额归零", r.get("used_mb") == 0.0, f"used={r.get('used_mb')}")
    check("无残留目录", "__resume_test__" not in [i["name"] for i in (r.get("items") or [])])
    check("没有写进真实存储根", not (Path.home() / "dsh-users" / "heavy-admin").exists())
finally:
    srv.terminate()
    try:
        srv.wait(timeout=8)
    except subprocess.TimeoutExpired:
        srv.kill()

total = elapsed + elapsed1
print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
print(f"性能参考：首传 {FIRST} 片 {elapsed:.1f}s｜续传 {expect - FIRST} 片 {elapsed1:.1f}s"
      f"（{SIZE // 1048576}MB 合计约 {total:.1f}s，约 {(SIZE / 1048576) / max(total, 1e-6):.1f} MB/s）")
if FAIL:
    print("失败项: " + "、".join(FAIL))
    sys.exit(1)
print("中断续传实测 全部通过 ✅")
