# 运维与回滚

## 首次更新检查清单

更新前：

- 确认可以在物理控制台、IPMI、云厂商 VNC 或其他救援入口选择启动项
- 至少保留一个已验证可启动的旧内核
- 确认 `/boot` 有足够空间
- 备份关键数据
- 运行 `sudo kau doctor`
- 运行 `sudo kau update --dry-run`

更新后：

```bash
uname -r
kau kernels
kau status
```

建议重启后观察一段时间，再清理旧内核。

## 回滚

`kau` 不会自动删除旧内核。新内核启动失败时：

1. 重启并进入引导菜单
2. 在 GRUB 中选择 “Advanced options for …”
3. 选择上一个可启动内核
4. 进入系统后修复问题，或把 `channel` / `--to` 指向稳定版本

使用 systemd-boot 时，在启动菜单中直接选择旧内核条目。

`kau kernels` 会列出 `/lib/modules` 下的内核目录，`*` 表示当前运行内核，`[kau]` 表示由本工具记录的安装项。

如果新内核已经安装但启动失败，状态可能显示 “pending reboot”。`kau auto` 默认不会重复安装。需要重新构建同一版本时：

```bash
sudo kau update --to 7.3.1 --force
```

## 校验与 Secure Boot

### `package` 模式

apt / dnf 负责校验仓库元数据和软件包签名。使用发行版签名内核时，UEFI Secure Boot 通常可以直接工作，前提是发行版签名密钥已受信任。

### `build` 模式

默认校验 `sha256sums.asc` 中的 SHA-256，但不验证 GPG 签名。该文件的签名密钥不对外公开，因此不能作为用户可独立验证的信任链；真正的发布签名位于源码包的 `linux-*.tar.sign`。

完整校验需要导入 kernel.org 发布密钥并开启 `verify_pgp`：

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

开启后，工具会下载对应的 `.tar.sign`，在内存流中解压源码并交给 `gpg --verify` 验证。这会增加 CPU 和更新时间。

mainline / RC 通常没有 `.tar.sign`，必须关闭 `verify_pgp` 并显式允许未校验源码。该做法不建议用于生产。

### Secure Boot

- `package`：使用发行版已签名内核
- `build`：安装未签名内核，检测到 Secure Boot 时默认拒绝
- `--force`：仅跳过检测，不会生成 Secure Boot 签名

如果必须使用自编译内核，应自行配置密钥、`sign-file` 和登记流程。不要在不了解后果时关闭 Secure Boot 或使用 `--force`。

## 日志与状态

```bash
sudo kau status
sudo kau kernels
sudo kau doctor
sudo journalctl -u kernel-autoupdater.service -n 100 --no-pager
sudo tail -n 100 /var/log/kernel-autoupdater.log
```

日志轮转配置：

```text
/etc/logrotate.d/kernel-autoupdater
```

状态文件：

```text
/var/lib/kernel-autoupdater/state.json
```

## 磁盘清理

`build` 模式可能占用较多空间：

- 成功安装后默认删除编译目录
- 失败时会保留编译目录便于排查
- `work_dir/downloads/` 中的源码包不会自动删除
- `--keep-build` 或 `keep_build_dir = true` 会保留编译目录

检查占用：

```bash
du -sh /var/lib/kernel-autoupdater/*
du -sh /boot
```

清理旧内核时，务必保留至少一个已确认可启动的版本，并同步更新引导项。

## 常见问题

### `package` 模式找不到候选内核

先刷新软件源：

```bash
sudo apt-get update
# 或
sudo dnf makecache --refresh
```

然后检查包名和仓库：

```bash
apt-cache policy linux-image-generic
# 或
dnf repoquery kernel
```

如果发行版使用特殊内核包，设置 `packages.kernel_packages`。

### 不支持 apt / dnf

`package` 模式目前只支持 apt 和 dnf。Arch 等系统可继续使用 `build` 模式，或使用发行版自己的升级命令。

### 磁盘空间不足

```bash
sudo kau doctor
```

根据模式调整 `[packages] min_free_gb` 或 `[updater] min_free_gb`，或清理 `/var/lib/kernel-autoupdater` 和 `/boot` 中不再需要的旧内容。

### 编译失败或内存不足

- 降低 `[build] jobs`
- 使用 `--keep-build` 保留源码目录
- 加 `-v` 查看详细日志
- 确认内核配置来源可用
- 检查编译依赖是否齐全

```bash
sudo kau update --method build -y -v
```

### 找不到当前内核配置

指定配置文件：

```ini
[build]
config_source = /root/config-6.8.0
```

或临时使用 defconfig 测试（可能缺少当前硬件驱动）：

```bash
sudo kau update --method build --defconfig -y
```

### Secure Boot 拒绝安装

优先改用 `package`：

```bash
sudo kau update --method package -y
```

如果必须安装自编译内核，应先完成签名。仅使用 `--force` 会让内核安装成功，但 Secure Boot 仍可能拒绝启动。

### TUI 乱码或无法打开

```bash
export LANG=C.UTF-8
sudo kau
```

TUI 需要真实 TTY；在脚本、CI 或管道中请使用普通子命令。

### Systemd timer 没有运行

```bash
systemctl status kernel-autoupdater.timer
systemctl list-timers kernel-autoupdater.timer
sudo kau enable --now
```

如果单元不存在，请重新运行安装脚本。

## 已知限制

- `build` 模式不合并发行版内核补丁、ABI 或 DKMS 改动
- `package` 模式无法指定任意上游版本，也不应用本地配置和补丁
- 工具不修改 `GRUB_DEFAULT`
- 不自动删除旧内核
- 容器、WSL 或无 `/boot` 环境不能完成引导更新
