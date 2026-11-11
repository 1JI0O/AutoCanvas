#!/usr/bin/env python3
"""Run a project-local Tailscale Serve node without root or system changes."""
import argparse
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / 'artifacts' / 'deployment' / 'tailscale'
BIN = HOME / 'bin'
STATE = HOME / 'state'
SOCKET = STATE / 'tailscaled.sock'
TMUX = ['tmux', '-L', 'autocanvas-deploy']
SESSION = 'autocanvas'
WINDOW = 'tailscale'
TARGET = 'http://127.0.0.1:4173'


def run(args, **kwargs):
    return subprocess.run([str(arg) for arg in args], cwd=ROOT, **kwargs)


def cli(*args, **kwargs):
    if not (BIN / 'tailscale').is_file():
        raise SystemExit('先运行 install 安装项目内的 Tailscale。')
    return run([BIN / 'tailscale', f'--socket={SOCKET}', *args], **kwargs)


def install(archive=None):
    if any((BIN / name).exists() for name in ('tailscale', 'tailscaled')):
        raise SystemExit('项目内二进制已存在；为避免覆盖运行中的程序，未重复安装。')
    architecture = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    if not architecture:
        raise SystemExit('仅支持 Linux amd64/arm64 静态安装。')
    HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    HOME.chmod(0o700)
    BIN.mkdir(mode=0o700, exist_ok=True)
    # Only unpack the two executables; do not install bundled systemd services.
    with tempfile.TemporaryDirectory(dir=HOME) as temporary:
        if archive:
            source = Path(archive).resolve()
        else:
            with urlopen('https://pkgs.tailscale.com/stable/?mode=json', timeout=30) as response:
                info = json.load(response)
            name = info['Tarballs'][architecture]
            if Path(name).name != name or not name.endswith('.tgz'):
                raise SystemExit('下载站返回了无效的包名。')
            print(f'下载 Tailscale {info["Version"]} ({architecture})', flush=True)
            source = Path(temporary) / name
            with urlopen('https://pkgs.tailscale.com/stable/' + name, timeout=60) as response, source.open('xb') as destination:
                shutil.copyfileobj(response, destination)
        with tarfile.open(source, 'r:gz') as package:
            for name in ('tailscale', 'tailscaled'):
                matches = [member for member in package.getmembers()
                           if member.isfile() and Path(member.name).name == name]
                if len(matches) != 1:
                    raise SystemExit(f'安装包缺少唯一的 {name} 可执行文件。')
                staged = Path(temporary) / name
                with package.extractfile(matches[0]) as contents, staged.open('xb') as destination:
                    shutil.copyfileobj(contents, destination)
                staged.chmod(0o700)
            for name in ('tailscale', 'tailscaled'):
                (Path(temporary) / name).rename(BIN / name)
    run([BIN / 'tailscale', 'version'], check=True)


def start():
    if not (BIN / 'tailscaled').is_file():
        raise SystemExit('先运行 install 安装项目内的 Tailscale。')
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    HOME.chmod(0o700)
    STATE.chmod(0o700)
    existing = run([*TMUX, 'list-windows', '-t', '=' + SESSION, '-F', '#{window_name}'],
                   capture_output=True, text=True)
    if existing.returncode == 0 and WINDOW in existing.stdout.splitlines():
        print('本项目的 tailscale 窗口已存在，未重复启动。')
        return
    command = shlex.join([str(BIN / 'tailscaled'), '--tun=userspace-networking',
                          f'--statedir={STATE}', f'--socket={SOCKET}', '--port=0'])
    if existing.returncode == 0:
        args = [*TMUX, 'new-window', '-d', '-t', '=' + SESSION, '-n', WINDOW, '-c', ROOT]
    else:
        args = [*TMUX, '-f', '/dev/null', 'new-session', '-d', '-s', SESSION, '-n', WINDOW, '-c', ROOT]
    run([*args, 'umask 077; exec ' + command], check=True)
    for _ in range(50):
        if SOCKET.exists():
            print('用户态 Tailscale 已在本项目独立 tmux 中启动。')
            return
        time.sleep(0.1)
    raise SystemExit('Socket 尚未就绪，请查看本项目 tailscale 窗口。')


def status():
    result = cli('status', '--json', capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise SystemExit('项目 Tailscale 尚未启动；先运行 start。') from None
    own = data.get('Self') or {}
    report = {'state': data.get('BackendState'), 'online': own.get('Online'),
              'dns_name': own.get('DNSName'), 'ips': own.get('TailscaleIPs'),
              'login_url': data.get('AuthURL'), 'health': data.get('Health')}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if data.get('BackendState') == 'Running':
        cli('serve', 'status', check=True)


def stop():
    result = run([*TMUX, 'display-message', '-p', '-t', SESSION + ':' + WINDOW, '#{pane_pid}'],
                 capture_output=True, text=True)
    if result.returncode != 0:
        print('本项目 tailscale 窗口未运行。')
        return
    pid = int(result.stdout.strip())
    if Path(f'/proc/{pid}/exe').resolve() != (BIN / 'tailscaled').resolve():
        raise SystemExit('窗口进程不是本项目 tailscaled，未停止任何进程。')
    run([*TMUX, 'send-keys', '-t', SESSION + ':' + WINDOW, 'C-c'], check=True)
    print('已向本项目 tailscaled 发送退出信号；配置和登录状态保留。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install', 'start', 'login', 'serve', 'status', 'stop'])
    parser.add_argument('--archive', type=Path, help='install: use an already downloaded official .tgz')
    args = parser.parse_args()
    if platform.system() != 'Linux':
        raise SystemExit('此脚本在 Linux 服务器运行；手机/Windows 请使用官方 Tailscale 客户端。')
    os.umask(0o077)
    if args.command == 'install':
        install(args.archive)
    elif args.command == 'start':
        start()
    elif args.command == 'login':
        start()
        raise SystemExit(cli('up', '--hostname=autocanvas-webui', '--accept-dns=false',
                             '--accept-routes=false', '--timeout=30s').returncode)
    elif args.command == 'serve':
        with urlopen(TARGET, timeout=5) as response:
            if response.status != 200:
                raise SystemExit('请先启动 4173 密码网关。')
        # Serve is private to the tailnet. Never enable Funnel here.
        raise SystemExit(cli('serve', '--bg', '--https=443', TARGET).returncode)
    elif args.command == 'status':
        status()
    else:
        stop()


if __name__ == '__main__':
    main()
