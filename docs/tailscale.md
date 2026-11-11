# 使用 Tailscale 私有 HTTPS 访问 WebUI

使用自己的电脑和手机访问时，可以用 Tailscale Serve：不需要购买域名，也不需要开放公网入站端口。访问设备须安装官方 Tailscale 客户端、加入同一个 tailnet，并符合该网络的访问规则；WebUI 仍然要求输入 `dashboard.yml` 中的访问密码。

访问路径：浏览器 → Tailscale 私有 HTTPS → `127.0.0.1:4173` 密码网关 → `127.0.0.1:8080` API。后端和网关继续仅监听 localhost。

脚本提供项目内的标准用户态 Tailscale 节点。它不需要 sudo、不安装系统服务、不修改系统 DNS/路由/防火墙，通过官方 CLI 管理、升级和诊断，无需自编译入口。Serve 的 HTTPS 入口指向密码网关。请不要为此项目启用 Funnel、子网路由、出口节点或 Tailscale SSH。

用户态网络会将到达节点的其他端口连接转发到 localhost 同端口。因此，符合 tailnet 访问规则的设备可能直接访问服务器的其他服务，包括内部的 8080 API；WebUI 密码不会保护绕过网关的连接。此部署按用户接受的个人设备场景使用，tailnet 的成员和访问规则构成外层访问边界。如果未来邀请他人或共享设备，先将本节点的访问规则收紧至 TCP 443。脚本不会修改账号级 ACL/Grants，也不会停止或改配服务器上的其他服务。

## 服务器安装与启动

在 Linux 服务器的仓库根目录运行：

```sh
python3 scripts/tailscale_webui.py install
python3 scripts/tailscale_webui.py start
python3 scripts/tailscale_webui.py login
```

安装仅下载官方稳定版静态包，并提取 `tailscale` 和 `tailscaled` 两个程序；不安装 Python 依赖。已有下载包时，可以用 `install --archive /path/to/tailscale_VERSION_amd64.tgz` 避免重复下载。安装不会覆盖已有程序。

`login` 会打印账号授权链接，请在自己的浏览器完成登录，将 `autocanvas-webui` 加入自己的 tailnet；不要把账号密码填写到项目代码。等待超时不会删除登录状态，可用 `status` 查看结果。如果 tailnet 开启了设备审批，还需要在 Tailscale 管理台批准此设备。

## 启用私有 HTTPS

确认项目密码网关已在 `127.0.0.1:4173` 运行，再执行：

```sh
python3 scripts/tailscale_webui.py serve
```

首次使用时，Tailscale 可能打印一个配置链接，请在浏览器按提示启用 Serve 所需的 HTTPS；如果提示，也启用 MagicDNS。此步骤使用 Tailscale 的账号界面。完成后重新运行 `serve`。

成功后显示 `https://autocanvas-webui.<你的 tailnet>.ts.net/`。使用命令实际输出的地址，不要直接照抄示例。电脑/手机登录同一 tailnet 后，即使位于外网，也可以打开这个地址，再输入 WebUI 访问密码。SSE 实时事件通过同一 HTTPS 连接转发。

Serve 只供符合 tailnet 访问规则的设备使用，不是公网网站。如果这个 tailnet 还包含其他用户或共享设备，访问范围取决于其 ACL/Grants；“同一个 tailnet”不自动等于“只有你的设备”。需要更小的范围时，应在 Tailscale 管理台针对该节点的 TCP 443 配置访问规则。脚本不会更改账号级规则。

## 状态与停止

```sh
python3 scripts/tailscale_webui.py status
python3 scripts/tailscale_webui.py stop
```

程序和私有状态保存在 `artifacts/deployment/tailscale/`，已由仓库的 `artifacts/` 忽略规则覆盖。`state/` 含设备私钥，目录权限为 0700，不应提交、公开或复制到 WebUI 静态目录。

Tailscale 守护进程运行在独立 tmux socket `autocanvas-deploy` 的 `autocanvas:tailscale` 窗口，不触碰默认 socket 中的其他会话。

```sh
tmux -L autocanvas-deploy attach -t autocanvas
```

`stop` 只停止本项目的 Tailscale 进程，保留后端、网关、配置和设备身份。之后运行 `start` 即可恢复；`serve --bg` 的配置保存在设备状态中。tmux 不提供服务器重启后的自动启动，重启后仍需启动项目和本项目 Tailscale。

如果 Windows 客户端未登录，先打开系统托盘的 Tailscale 完成登录；手机也使用同一个账号/tailnet。不要在服务器安装桌面环境。

## 验收

在已接入 tailnet 的另一台设备上，检查 HTTPS 证书、密码页、登录后页面和 API、SSE 实时更新；未登录网关时，`/api/*`、`/assets/*` 和 `/health` 应返回 401。已登录 Tailscale 不会绕过 WebUI 密码。

如果局域网可达但外网不通，需要从手机移动数据或另一个外部网络复测，结合 `status` 和 Tailscale 连通性诊断排查。仅验证 daemon 启动或分配到 IP，不等于完成外部访问验收。

官方资料：[Serve](https://tailscale.com/docs/features/tailscale-serve)、[用户态网络](https://tailscale.com/docs/concepts/userspace-networking)、[Linux 静态安装](https://tailscale.com/docs/install/linux)。
