# Kernel AutoUpdater

`kau` 是一个面向 Linux 的内核自动更新工具。它既可以安装发行版仓库中的预编译内核，也可以从 kernel.org 下载源码并沿用当前内核配置在本机编译。

项目同时提供 `kau` 命令行和 `kmenu` 全屏交互菜单。两种更新方式都会保留旧内核，便于故障时回滚。

## 核心特性

- `package` 模式通过 apt / dnf 安装发行版签名内核，资源占用低
- `build` 模式支持 kernel.org stable、longterm、指定系列和 mainline
- 支持 SHA-256 校验和可选的 GPG 签名验证
- 自动同步当前内核配置、应用本地补丁、编译并安装
- 自动刷新 initramfs 和 GRUB / systemd-boot
- systemd timer 每日检查，可配置自动安装和自动重启
- 提供 `doctor`、`--dry-run`、JSON 输出和日志
- TUI 中可检查更新、安装、切换模式、改配置和更新 `kau` 自身

## 两种更新方式

| 方式 | 内核来源 | 典型耗时 | 适合场景 |
| --- | --- | --- | --- |
| `package`（默认） | apt / dnf 仓库 | 几十秒到几分钟 | 日常安全更新、低配置机器、Secure Boot |
| `build` | kernel.org 源码 | 10–60 分钟 | 自定义 `.config`、补丁、上游新内核 |

`package` 模式不需要编译工具链，也不会向 `/boot` 写入未签名内核。`build` 模式直接使用 `make install`，不经过发行版包管理器，也不负责 Secure Boot 签名。

## 快速开始

```bash
curl -fsSL https://raw.githubusercontent.com/DoriyaNet/Kernel-AutoUpdater/master/install.sh | sudo bash
sudo kau doctor
sudo kau check
sudo kau update --dry-run
sudo kau
```

最后一条命令会打开交互菜单；也可以运行 `sudo kmenu`。

## 文档

- [安装与升级](Installation)
- [更新方式](Update-Methods)
- [配置说明](Configuration)
- [使用指南](Usage)
- [运维与回滚](Operations)
- [开发与测试](Development)

## 支持范围

- Debian / Ubuntu：`update-initramfs` + `update-grub`
- Fedora / RHEL：`kernel-install` / `dracut` + `grub2-mkconfig`
- systemd-boot：通过 `kernel-install` 生成 BLS 条目
- Arch 等使用 `mkinitcpio` 的系统：`build` 模式需要自行创建对应 preset

`package` 模式目前支持 apt 和 dnf。容器、WSL 或没有 `/boot` 的环境无法完成引导更新。

## 安全提醒

内核更新可能影响系统启动。首次使用前应确认：

- 当前有可用的控制台或救援模式
- 至少保留一个可启动的旧内核
- 已备份重要数据
- 已阅读 [运维与回滚](Operations)

开启 UEFI Secure Boot 时，优先使用 `package` 模式。`build` 模式产生的未签名内核默认会被拒绝安装；`--force` 只能跳过检查，不能解决签名和启动问题。
