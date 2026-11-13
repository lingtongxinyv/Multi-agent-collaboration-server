#!/bin/bash
# NapCat 启动包装：先自己起 Xvfb（-nolisten tcp），再交给镜像自带 entrypoint。
#
# 为什么：本机（host 网络）跑 mlikiowa/napcat-docker:latest 时，镜像里的
#   # 镜像里没有 /tmp/.X11-unix，Xvfb 会因建不了 unix socket 直接退出（首跑实测）
mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
rm -f /tmp/.X1-lock

gosu napcat Xvfb :1 -screen ... +extension GLX +render
# 会报 “Cannot establish any listening sockets / server already running” 然后退出，
# 于是 QQ 在没有 DISPLAY 的情况下启动、卡在登录前，日志停在 resourcesPath。
# 加 -nolisten tcp 后 Xvfb 正常。原 entrypoint 随后再起一个 Xvfb 会失败（无害），
# 然后 QQ 连上我们已经起好的 :1。
set -u

# 镜像里没有 /tmp/.X11-unix，Xvfb 会因建不了 unix socket 直接退出（首跑实测）
mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
rm -f /tmp/.X1-lock

gosu napcat Xvfb :1 -screen 0 1080x760x16 +extension GLX +render -nolisten tcp \
    >/tmp/xvfb.log 2>&1 &
sleep 2

exec /app/entrypoint.sh
