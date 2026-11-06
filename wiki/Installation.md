# 安装与升级

## 环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Linux |
| Python | 3.9 或更高版本 |
| 权限 | root |
| 下载工具 | `curl` 或 `wget` |
| 软件包 | `package` 模式需要 apt 或 dnf |
| 磁盘 | `package` 模式约 2 GiB；`build` 模式默认预检 12 GiB |

安装器本身不需要第三方 Python 包。`build` 模式还需要内核编译工具链，详见 [更新方式](Update-Methods)。

## 一键安装

从 GitHub 的 `master` 分支安装：

```bash
curl -fsSL https://raw.githubusercontent.com/DoriyaNet/Kernel-AutoUpdater/master/install.sh | sudo bash
```

使用 `wget`：

```bash
wget -qO- https://raw.githubusercontent.com/DoriyaNet/Kernel-AutoUpdater/master/install.sh | sudo bash
```

### 安装其他分支或版本

```bash
curl -fsSL https://raw.githubusercontent.com/DoriyaNet/Kernel-AutoUpdater/master/install.sh \
  | sudo bash -s -- --ref master
```

把 `master` 换成 tag 可以安装固定版本，例如：

```bash
curl -fsSL https://raw.githubusercontent.com/DoriyaNet/Kernel-AutoUpdater/master/install.sh \
  | sudo bash -s -- --ref v0.4.2
```

### 不启用定时器

```bash
curl -fsSL https://raw.githubusercontent.com/DoriyaNet/Kernel-AutoUpdater/master/install.sh \
  | sudo bash -s -- --no-timer
```

定时器会被安装但不会启用。之后可用 `sudo kau enable --now` 手动启用。

### 安装器环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `KAU_REF` | `master` | 默认安装的分支或 tag |
| `KAU_REPO_URL` | 官方 GitHub 仓库 | 用于测试 fork 或镜像 |
| `ENABLE_TIMER` | `1` | 设为 `0` 时安装但不启用 timer |
| `PYTHON` | `python3` | 构建单文件程序时使用的解释器 |

## 从本地仓库安装

```bash
git clone https://github.com/DoriyaNet/Kernel-AutoUpdater.git
cd Kernel-AutoUpdater
sudo bash install.sh
```

本地安装同样支持 `--no-timer`：

```bash
sudo bash install.sh --no-timer
```

## 安装内容

| 路径 | 内容 |
| --- | --- |
| `/usr/local/bin/kau` | 单文件可执行程序 |
| `/usr/local/bin/kmenu` | 指向 `kau` 的快捷命令 |
| `/etc/systemd/system/kernel-autoupdater.service` | 更新服务 |
| `/etc/systemd/system/kernel-autoupdater.timer` | 每日检查定时器 |
| `/etc/logrotate.d/kernel-autoupdater` | 日志轮转规则 |
| `/etc/kernel-autoupdater/config.ini` | 配置文件，首次安装时写入 |
| `/etc/kernel-autoupdater/patches/` | 本地补丁目录 |
| `/var/lib/kernel-autoupdater/` | 状态、缓存、下载和编译目录 |
| `/usr/local/share/doc/kernel-autoupdater/README.md` | 随程序安装的说明 |
| `/var/log/kernel-autoupdater.log` | 默认日志文件，运行时创建 |

安装器不会自动安装 apt、dnf 或内核编译依赖，也不会修改已有配置文件。

## 安装后检查

```bash
sudo kau doctor
sudo kau check
sudo kau update --dry-run
```

确认预检和预览没有错误后，可以先打开菜单：

```bash
sudo kau
```

首次安装默认启用 timer，但 `[updater] auto_update` 为 `false`，因此定时任务只会检查，不会自动安装。

## 更新 `kau` 自身

查看是否有新版本：

```bash
sudo kau self-update --check
```

安装新版本：

```bash
sudo kau self-update
```

其他用法：

```bash
sudo kau self-update --yes
sudo kau self-update --force
sudo kau self-update --ref master
```

`self-update` 需要 root，并会沿用现有配置和 timer 状态。若通过代理访问 GitHub，可设置 `HTTPS_PROXY` 或 `HTTP_PROXY`。

注意：

- 自更新只支持通过 `install.sh` 安装的布局
- 如果使用 `pip install`，应使用 `pip` 升级
- 自更新下载 GitHub 的 HTTPS 源码包，目前不额外验证发布签名
- 新版本可用时，`--check` 的退出码为 `10`

## 卸载

在仓库目录中运行：

```bash
sudo bash scripts/uninstall.sh
```

卸载脚本会移除：

- `kau` 和 `kmenu`
- systemd service / timer
- logrotate 规则
- `/usr/local/share/doc/kernel-autoupdater`

以下内容会被保留：

- `/etc/kernel-autoupdater`
- `/var/lib/kernel-autoupdater`
- 所有已安装内核和引导项

确认不再需要配置、状态或日志后，可自行删除保留目录。不要在未确认旧内核可用前清理 `/boot`。

## 安装脚本安全说明

`curl | sudo bash` 会直接以 root 执行远程脚本。对安全敏感的环境，建议先下载并审查 `install.sh`，或克隆仓库后本地安装。
