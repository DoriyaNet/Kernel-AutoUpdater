# Kernel AutoUpdater

`kau` 是一个面向 Linux 的内核自动更新工具，提供命令行和全屏交互菜单。

它支持两种更新方式：

- **package（默认）**：从发行版仓库安装预编译内核，资源占用低，适合日常安全更新。
- **build（可选）**：从 kernel.org 下载源码，沿用当前内核配置在本机编译，适合需要自定义配置或补丁的场景。

两种方式都会保留旧内核，便于回滚。

[最新版本](https://github.com/DoriyaNet/Kernel-AutoUpdater/releases/latest) ·
[Wiki 文档](https://github.com/DoriyaNet/Kernel-AutoUpdater/wiki) ·
[问题反馈](https://github.com/DoriyaNet/Kernel-AutoUpdater/issues)

> [!WARNING]
> 更新内核可能导致系统无法启动。首次使用请先运行 `sudo kau update --dry-run`，并在有控制台或救援模式的环境中保留至少一个可启动的旧内核。`build` 模式安装的是未签名内核，开启 UEFI Secure Boot 时默认会被拒绝。

## 功能

- 检查并安装 apt / dnf 仓库中的发行版内核
- 检查 kernel.org 的 stable、longterm、指定系列或 mainline 内核
- 校验源码 SHA-256，可选验证 GPG 签名
- 同步当前 `.config`，支持本地补丁和自定义 localversion
- 自动刷新 initramfs，并更新 GRUB / systemd-boot
- systemd timer 每日检查，可配置无人值守安装
- 交互式 TUI，常用配置和常用操作都可直接完成
- 内置环境自检、状态查询、日志查看和程序自更新

## 一键安装

要求：Linux、Python 3.9+、root，以及 `curl` 或 `wget`。

```bash
curl -fsSL https://raw.githubusercontent.com/DoriyaNet/Kernel-AutoUpdater/master/install.sh | sudo bash
```

不启用 systemd timer：

```bash
curl -fsSL https://raw.githubusercontent.com/DoriyaNet/Kernel-AutoUpdater/master/install.sh \
  | sudo bash -s -- --no-timer
```

安装完成后：

```bash
sudo kau doctor
sudo kau update --dry-run
sudo kau
```

最后一条命令会打开交互菜单。也可以使用快捷命令 `sudo kmenu`。

## 更新方式

| 方式 | 内核来源 | 耗时 | 适合场景 |
| --- | --- | --- | --- |
| `package` | apt / dnf 仓库 | 通常几十秒到几分钟 | 日常更新、低配置机器、Secure Boot |
| `build` | kernel.org 源码 | 通常 10–60 分钟 | 自定义配置、补丁、上游新内核 |

临时切换更新方式：

```bash
sudo kau update --method build -y
sudo kau update --method package -y
```

永久配置位于 `/etc/kernel-autoupdater/config.ini`：

```ini
[updater]
update_method = package
```

## 支持范围

- Debian / Ubuntu：`update-initramfs` + `update-grub`
- Fedora / RHEL：`kernel-install` / `dracut` + `grub2-mkconfig`
- systemd-boot：通过 `kernel-install` 生成 BLS 条目
- Arch 等使用 `mkinitcpio` 的系统：`build` 模式需要自行配置 mkinitcpio preset

容器、WSL 或没有 `/boot` 的环境无法完成引导更新。`package` 模式目前支持 apt 和 dnf。

## 文档

详细说明、配置项、故障排查和开发指南都在 Wiki：

- [安装与升级](https://github.com/DoriyaNet/Kernel-AutoUpdater/wiki/Installation)
- [更新方式](https://github.com/DoriyaNet/Kernel-AutoUpdater/wiki/Update-Methods)
- [配置说明](https://github.com/DoriyaNet/Kernel-AutoUpdater/wiki/Configuration)
- [使用指南](https://github.com/DoriyaNet/Kernel-AutoUpdater/wiki/Usage)
- [运维与回滚](https://github.com/DoriyaNet/Kernel-AutoUpdater/wiki/Operations)
- [开发与测试](https://github.com/DoriyaNet/Kernel-AutoUpdater/wiki/Development)

## 安全提示

`build` 模式直接执行 `make install`，不经过发行版包管理器，也不会为内核签名。Secure Boot 开启时应使用 `package` 模式，或自行完成签名后再使用 `--force`。

`curl | sudo bash` 会以 root 执行远程脚本。对安全敏感的环境，建议先审查 `install.sh`，或克隆仓库后本地安装。

## 开发

```bash
python -m unittest discover -s tests -t . -v
PYTHONPATH=src python -m kernel_autoupdater --help
```
