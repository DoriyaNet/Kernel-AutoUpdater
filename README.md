# Kernel AutoUpdater

`kau` 是一个面向 Linux 内核的 CLI 自动更新器，提供两种更新方式：

- **package（默认）**：直接使用发行版仓库（apt / dnf）里的预编译内核包，不下载内核源码、
  不占编译所需的 CPU / 内存，由发行版包管理器完成安装、initramfs 和引导项更新。
- **build（可选）**：从 kernel.org 检查 stable / longterm 内核，下载并校验源码，
  沿用当前内核的 `.config` 在本机编译，再安装并刷新 initramfs / GRUB。

两种方式都会保留旧内核用于回滚。

> **重要：** `build` 模式直接执行 `make install`，不经过发行版包管理器，默认也不
> 签名内核；Secure Boot 开启时工具会拒绝安装未签名内核。`package` 模式调用
> `apt-get` / `dnf` 安装发行版签名内核，不执行本地编译。首次使用建议先执行
> `kau update --dry-run`，并确保你有控制台 / 救援模式可以选择旧内核。

## 功能

- 检查 kernel.org 的 stable、longterm、指定 stable 系列或 mainline 版本
- 检查 apt / dnf 仓库中发行版内核元包的候选版本
- 下载 `.tar.xz`，使用目录级 `sha256sums.asc` 做 SHA-256 校验
- 可选 GPG 校验官方 `linux-*.tar.sign`（使用本地 kernel.org 公钥环）
- 从当前运行内核同步 `.config`，执行 `make olddefconfig` 后构建
- 支持本地补丁目录，按文件名顺序应用 `*.patch` / `*.diff`
- `make modules_install && make install`，自动刷新 initramfs 和 GRUB
- 可选从 apt / dnf 仓库安装发行版预编译内核，避免本机编译的资源开销
- 保留所有旧内核；记录由 `kau` 安装的版本，便于确认
- systemd timer 每日检查；`auto_update` 控制是否无人值守安装
- 文件锁防止并发更新；失败时写入状态与日志
- 一键安装；`kau` / `kmenu` 打开交互菜单，常用配置可直接在 TUI 中修改
- 菜单内置程序自更新，可从 GitHub 升级 `kau` 本身
- `--dry-run`、`doctor`、`check --json`，方便先验证再上线

## 更新方式

在 `/etc/kernel-autoupdater/config.ini` 中设置：

```ini
[updater]
update_method = package   # package | build
```

也可以只对单次命令覆盖：

```bash
sudo kau check
sudo kau update --dry-run
sudo kau update -y

# 临时改用源码编译
sudo kau update --method build -y
```

| 方式 | 内核来源 | 本机资源 | 适合场景 |
| --- | --- | --- | --- |
| `build` | kernel.org 原版源码 | 需要编译工具链，通常 10–60 分钟 CPU 时间 | 需要自定义 `.config`、补丁、`-kau` 后缀或最新主线 |
| `package` | apt / dnf 发行版仓库 | 只下载二进制包，通常几十秒到几分钟 | 日常安全更新、低配 VPS / 笔记本、希望 Secure Boot 继续使用发行版签名内核 |

`package` 现在是默认值。已有配置如果显式写了 `update_method = build`，仍会继续使用
源码编译；把它改成 `package`（或在 TUI 主菜单选择“更新方式”）即可切换。
旧配置中完全没有 `update_method` 时，升级后会按新的默认值 `package` 处理；如果
需要继续源码编译，请显式补上 `update_method = build`。

`package` 模式使用发行版自己的内核包，因此：

- 内核带发行版后缀（如 `6.8.0-80-generic`），不会追加 `build.localversion`
- 不应用 `build.config_source` 和 `source.patches_dir`
- initramfs、GRUB / systemd-boot 由发行版包的 post-install 脚本维护
- `[install]` 中的 `update_initramfs` / `update_bootloader` 仅对 `build` 模式生效
- 仓库元数据和软件包签名由 apt / dnf 校验；UEFI Secure Boot 可直接使用发行版签名内核
- `--to`、`--defconfig`、`--keep-build`、`--jobs` 仅适用于 `build` 模式

`kau check` 使用本机已有的软件源索引，不会主动联网刷新；`kau update` 和
`kau auto` 在安装前会按 `packages.refresh_package_index` 执行 `apt-get update`
或 `dnf makecache --refresh`。

`kernel_packages = auto` 在 Ubuntu 上选择 `linux-image-generic` /
`linux-headers-generic`，在 Debian 上选择 `linux-image-<arch>` /
`linux-headers-<arch>`。如果你运行的是 `lowlatency`、`aws`、`rt`、`linux-lts`
等 flavor，请显式设置对应的内核包名。

## 支持范围

已适配常见 initramfs / 引导链：

- Debian / Ubuntu：`update-initramfs` + `update-grub`
- Fedora / RHEL：`kernel-install` / `dracut` + `grub2-mkconfig`
- systemd-boot：依赖 `kernel-install` 生成 BLS 条目
- Arch 等使用 `mkinitcpio` 的系统：会安装内核，但自定义内核需要你自己创建
  mkinitcpio preset；工具会给出警告

`build` 模式的内核源码来自 kernel.org 原版，**不会**合并发行版的内核补丁或 ABI
变更；需要发行版补丁时改用 `package` 模式。

## 一键安装

要求：Linux、Python 3.9+、root、`curl` 或 `wget`。默认 `package` 模式只需 2 GiB
空闲磁盘；切换到 `build` 模式后至少需要 12 GiB（可配置）。

```bash
curl -fsSL https://raw.githubusercontent.com/DoriyaNet/Kernel-AutoUpdater/master/install.sh | sudo bash
# 或
wget -qO- https://raw.githubusercontent.com/DoriyaNet/Kernel-AutoUpdater/master/install.sh | sudo bash
```

这条命令会下载 GitHub 上的最新 `master` 源码包并安装。可选参数：

```bash
# 不自动启用 systemd timer
curl -fsSL https://raw.githubusercontent.com/DoriyaNet/Kernel-AutoUpdater/master/install.sh \
  | sudo bash -s -- --no-timer

# 指定分支或 tag（把 master 换成实际名称）
curl -fsSL https://raw.githubusercontent.com/DoriyaNet/Kernel-AutoUpdater/master/install.sh \
  | sudo bash -s -- --ref master
```

已经克隆仓库时，也可以本地安装：

```bash
git clone https://github.com/DoriyaNet/Kernel-AutoUpdater.git
cd Kernel-AutoUpdater
sudo bash install.sh
```

安装脚本会：

- 把程序打包为单文件 `/usr/local/bin/kau`（无第三方 Python 依赖）
- 创建快捷命令 `/usr/local/bin/kmenu`，等价于直接运行 `kau`
- 安装 systemd service / timer 和 logrotate 规则
- 首次安装时写入 `/etc/kernel-autoupdater/config.ini`
- 默认启用每日 timer，但 `auto_update = false`，所以只会检查、不会自动安装

如果暂时不想启用 timer：

```bash
sudo bash install.sh --no-timer
```

然后执行自检和试运行：

```bash
sudo kau doctor
sudo kau check
sudo kau update --dry-run
```

> `curl | sudo bash` 会以 root 执行远程脚本。请确认 URL 和仓库归属；对安全敏感的环境，
> 建议先下载并审查 `install.sh`，或使用 git clone 后本地安装。

## 交互菜单（TUI）

安装后直接运行 `kau`，或使用快捷命令 `kmenu`，即可打开全屏交互菜单：

```bash
sudo kau        # 无参数：TTY 下打开菜单
sudo kmenu      # 快捷方式
sudo kau menu   # 显式打开
sudo kau tui    # 别名
```

菜单内置：

- 检查更新、预览更新（dry-run）、立即安装
- 主菜单直接切换更新方式：`package`（发行版预编译包）或 `build`（源码编译）
- 程序自更新（更新 `kau` 本身）
- 状态、已安装内核、doctor 自检、日志查看
- 启用 / 停用 systemd timer
- 编辑常用配置：update_method、channel、auto_update、auto_reboot、超时、工作目录、
  日志路径、PGP 校验、`config_source`、`localversion`、jobs、包管理器 / 内核包、
  initramfs / bootloader 等

操作：方向键或 `j/k` 移动，`Enter` 选择，`Esc` / `q` 返回，主菜单按 `q` 退出。
设置修改会立即写入 `/etc/kernel-autoupdater/config.ini`，并保留原有注释；写入后会
重新加载配置校验，非法值会提示并恢复原文件。执行安装、检查等长任务时会临时退出
curses，任务结束后按 Enter 返回菜单。

菜单需要真实 TTY（Linux 终端 / SSH）；非 TTY 或没有 `curses` 时会返回明确错误，
仍可使用普通子命令。中文界面需要 UTF-8 locale；如出现乱码可先执行
`export LANG=C.UTF-8`。

## 首次更新

建议先手动跑一次完整流程，确认新内核可以启动：

```bash
sudo kau update --dry-run
sudo kau update -y
sudo reboot
```

重启后确认：

```bash
uname -r
kau kernels
kau status
```

默认 `package` 模式会从 apt / dnf 下载并安装发行版内核，耗时主要取决于软件源
速度。需要源码编译时使用：

```bash
sudo kau update --method build --dry-run
sudo kau update --method build -y
```

首次编译通常需要 10–60 分钟，取决于 CPU 和配置。`jobs = 0` 表示按 CPU 核心数
并行编译；内存较小的机器请手动降低 `jobs`，避免 OOM。


## 配置

配置文件：`/etc/kernel-autoupdater/config.ini`

```ini
[updater]
# package | build
update_method = package
# stable | longterm | longterm:6.12 | mainline
channel = stable
auto_update = false
auto_reboot = false
reboot_delay_minutes = 5
min_free_gb = 12

[source]
verify_pgp = false
allow_unverified = false
patches_dir = /etc/kernel-autoupdater/patches

[build]
config_source = auto
localversion = -kau
jobs = 0
keep_build_dir = false
clear_distro_keys = true

[packages]
# auto | apt | dnf
manager = auto
# auto 会选择 linux-image-generic / linux-image-<arch> 或 kernel
kernel_packages = auto
refresh_package_index = true
min_free_gb = 2

[install]
update_initramfs = true
update_bootloader = true
```

### 无人值守更新

确认手动更新成功后，再开启：

```ini
[updater]
auto_update = true
auto_reboot = false
```

`auto_reboot = true` 会在安装成功后按 `reboot_delay_minutes` 延迟重启。生产环境
建议保持 `auto_reboot = false`，在维护窗口内手动重启。

检查 timer：

```bash
systemctl list-timers kernel-autoupdater.timer
journalctl -u kernel-autoupdater.service -n 100 --no-pager
```

### 内核配置来源

`config_source` 支持：

- `auto` / `running`：依次查找 `/boot/config-$(uname -r)`、`/proc/config.gz`、
  `/lib/modules/$(uname -r)/config`、`/usr/lib/modules/$(uname -r)/config`
- `defconfig`：`make defconfig`，可能缺少当前机器所需驱动，不推荐生产使用
- 绝对路径：例如 `/root/config-6.8.0`

找到配置后会执行 `make olddefconfig`，自动补齐新内核新增的选项。`localversion`
默认追加 `-kau`，例如 `7.3.1-kau`，便于区分自编译内核。

Debian / Ubuntu 的内核配置会引用发行版签名密钥路径，而原版 kernel.org 源码中没有
这些文件。默认会清空 `SYSTEM_TRUSTED_KEYS` 和 `SYSTEM_REVOCATION_KEYS`；如果你
确实需要用自己的密钥，设置 `clear_distro_keys = false` 并自行准备密钥文件。

### 本地补丁

把补丁放进 `/etc/kernel-autoupdater/patches/`，扩展名为 `.patch` 或 `.diff`，
按文件名排序后以 `patch -p1` 应用。任何一个补丁失败都会在编译前中止更新。

## 命令

| 命令 | 说明 |
| --- | --- |
| `kau` / `kmenu` / `kau menu` | 打开交互式 TUI 菜单 |
| `kau self-update [--check] [--force] [--yes] [--ref REF]` | 更新 `kau` 程序本身 |
| `kau check [--json] [--offline] [--method build\|package]` | 检查新版本；有更新时退出码为 `10` |
| `kau update [--to VERSION] [--dry-run] [-y] [--force] [--reboot] [--method build\|package]` | 编译或安装内核 |
| `kau auto [--dry-run] [--method build\|package]` | systemd 调用；受 `auto_update` 控制 |
| `kau status [--json]` | 显示运行内核、配置、状态和 timer |
| `kau kernels [--json]` | 列出 `/lib/modules` 下的内核，标记当前内核和 kau 安装项 |
| `kau doctor [--method build\|package]` | 按更新方式检查依赖、配置/仓库、磁盘、Secure Boot、PGP 和 timer |
| `kau enable --now` / `kau disable` | 启用 / 停用 systemd timer |

常用参数：

- `--to 7.3.1`：安装指定版本；不在 release feed 中的稳定版会按 CDN 规则拼出 URL
- `--force`：目标不新于当前内核时仍安装；也用于绕过 Secure Boot 拦截
- `--keep-build`：复用 `work_dir/build/linux-*`，便于失败后继续排查
- `--defconfig`：本次改用 `make defconfig`
- `--jobs N`：本次覆盖并行编译数
- `--method build`：本次切回源码编译；不带该参数时使用默认的 `package` 模式

成功安装后默认删除编译目录以回收磁盘；失败时会保留，便于排查。若需要保留，
设置 `keep_build_dir = true` 或使用 `--keep-build`。

下载的源码包保存在 `work_dir/downloads/`，不会自动删除；请按需清理。

`package` 模式下不会使用 `work_dir/downloads`、`build_dir`、补丁目录或
`min_free_gb`；请改用 `[packages] min_free_gb` 控制所需磁盘空间。apt 默认会
更新 `linux-image-*` 与 `linux-headers-*` 元包，dnf 默认更新 `kernel` 包。
如果发行版使用不同的包名（例如 `linux-lts`），设置 `kernel_packages`。

### 程序自更新

菜单中的“程序自更新”会读取 GitHub `master`（或指定 ref）的 `pyproject.toml`
版本号，下载源码包并运行安装脚本更新 `/usr/local/bin/kau`。它需要 root，并会保留
现有配置和 timer 的启用状态。命令行用法：

```bash
sudo kau self-update --check     # 只检查；有新版本时退出码 10
sudo kau self-update             # 检查并确认更新
sudo kau self-update --force     # 版本相同也重新安装
sudo kau self-update --ref master
```

自更新下载来自 GitHub 的 HTTPS 源码包，当前不额外校验发布签名；如果网络需要通过
代理访问 GitHub，可设置 `HTTPS_PROXY` / `HTTP_PROXY` 环境变量。

自更新按照 `install.sh` 的安装布局工作（`/usr/local/bin/kau`、systemd 单元等）。
如果你是通过 `pip install` 安装的，请使用 `pip` 升级。

退出码：

- `0`：成功 / 无可用更新
- `1`：更新或检查失败
- `2`：参数或配置错误
- `10`：`check` 或 `self-update --check` 发现可用更新

## 回滚

工具不会删除旧内核。若新内核启动失败：

1. 在 GRUB 菜单选择 “Advanced options for …”
2. 选择上一个可启动内核
3. 修复问题后重新构建，或把 `/etc/kernel-autoupdater/config.ini` 中的
   `channel` / `--to` 指向稳定版本

`kau kernels` 可以列出所有已安装模块目录，`*` 标记当前运行内核，`[kau]` 标记
由本工具安装的版本。

如果新内核已经安装但启动失败，状态会显示 “pending reboot”，`kau auto` 不会反复
重装。需要重新构建同一版本时使用：

```bash
sudo kau update --to 7.3.1 --force
```

## 校验与 Secure Boot

以下内容针对 `build` 模式；`package` 模式由 apt / dnf 校验仓库与软件包签名。

默认始终校验 `sha256sums.asc` 中的 SHA-256（HTTPS 传输），但不验证 GPG 签名。
注意：`sha256sums.asc` 本身由 kernel.org 内部的自动签名密钥签署，官方说明该密钥
不对外发布，因此它不是用户可独立验证的信任链；真正的发布签名在源码包的
`linux-*.tar.sign` 中。

如需完整供应链校验，导入 Greg KH 的 kernel.org 发布密钥并开启 `verify_pgp`：

```bash
gpg --locate-keys gregkh@kernel.org
fpr="$(gpg --list-keys --with-colons gregkh@kernel.org | awk -F: '/^fpr:/ {print $10; exit}')"
gpg --export "$fpr" | sudo tee /etc/kernel-autoupdater/kernel.org-keyring.gpg >/dev/null
```

然后设置：

```ini
[source]
verify_pgp = true
```

开启后，工具会下载对应的 `.tar.sign`，在内存流中解压源码包并交给 `gpg --verify`
验证，不额外保留解压文件。该步骤会增加 CPU 和更新时间。mainline / RC 通常没有
`.tar.sign`，此时必须关闭 `verify_pgp` 并显式允许未校验源码。

未签名内核在 UEFI Secure Boot 开启时无法启动。`kau` 会在检测到 Secure Boot
开启时拒绝更新；如果你自行签名内核，可以用 `--force` 绕过该检查。不要在不了解
后果时使用 `--force`。

## 编译依赖

`package` 模式不需要本节的内核编译工具链，只需要可用的 apt 或 dnf。

Debian / Ubuntu：

```bash
sudo apt install build-essential bc flex bison libssl-dev libelf-dev \
    dwarves cpio rsync
```

Fedora / RHEL：

```bash
sudo dnf groupinstall "Development Tools"
sudo dnf install bc flex bison openssl-devel elfutils-libelf-devel dwarves cpio rsync
```

`kau doctor` 会检查缺失项。工具不会自动安装软件包。

## 限制

- `build` 模式不合并发行版内核补丁、ABI、DKMS 或第三方模块；需要额外补丁时使用
  patches 目录
- `build` 模式直接安装到 `/boot` 和 `/lib/modules`，不由 `apt`/`dnf` 管理；升级
  发行版包时可能产生重复内核
- `package` 模式只能安装发行版仓库已有的内核，无法使用 `--to` 指定任意上游版本，
  也不应用本地配置和补丁；目前支持 apt 与 dnf，Arch 的 pacman 请继续使用 `build`
  模式或发行版自己的升级命令
- `package` 模式会刷新发行版软件源索引；如果仓库中的内核包依赖其他系统包更新，
  apt / dnf 可能会一并升级这些依赖
- 不自动删除旧内核；长期使用请自行清理 `/boot`，但要保留至少一个可启动旧内核
- 工具不修改 `GRUB_DEFAULT`；如果使用 `GRUB_DEFAULT=saved`，请自行把新内核设为默认
- `build` 模式不支持给内核做 Secure Boot 签名；需要你自己配置密钥和 `sign-file`。
  `package` 模式使用发行版的已签名内核
- mainline / RC 源码通常既没有 `sha256sums.asc` 也没有 `.tar.sign`，必须设置
  `allow_unverified = true` 和 `verify_pgp = false` 才能构建，不建议生产使用
- 容器、WSL 或没有 `/boot` 的环境无法完成引导更新

## 开发与测试

```bash
python -m unittest discover -s tests -t . -v
PYTHONPATH=src python -m kernel_autoupdater --help
```

主要模块：

- `versioning.py`：内核版本解析、排序、channel
- `kernelorg.py`：release feed、校验和、下载
- `packages.py`：apt / dnf 预编译内核包与候选版本检查
- `builder.py`：预检、配置同步、编译、安装、回滚状态
- `system.py`：发行版 / initramfs / Secure Boot 探测
- `config.py`、`state.py`：配置与持久化
- `menu.py`：curses TUI 菜单；`config_editor.py`：保留注释的配置编辑
- `cli.py`：`kau` 子命令

