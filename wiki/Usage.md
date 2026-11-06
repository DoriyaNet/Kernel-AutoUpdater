# 使用指南

## 交互菜单

以下命令都会打开 TUI：

```bash
sudo kau
sudo kmenu
sudo kau menu
sudo kau tui
```

菜单支持：

- 检查更新
- 预览更新，不执行安装
- 立即安装
- 安装并重启
- 切换 `package` / `build`
- 更新 `kau` 自身
- 查看状态、已安装内核、环境自检和日志
- 启用 / 停用 systemd timer
- 编辑常用配置

操作方式：

- 方向键或 `j` / `k`：移动
- `Enter`：选择
- `Esc` / `q`：返回
- 主菜单 `q`：退出
- 长输出可用方向键、`PgUp` / `PgDn` 滚动

TUI 需要真实 TTY。中文界面需要 UTF-8 locale，乱码时可先执行：

```bash
export LANG=C.UTF-8
```

## 命令

| 命令 | 说明 |
| --- | --- |
| `kau` / `kmenu` / `kau menu` | 打开交互菜单 |
| `kau check [--json] [--offline] [--method build\|package]` | 检查更新 |
| `kau update [选项]` | 安装或编译内核 |
| `kau auto [--dry-run] [--method build\|package]` | 供 systemd 调用 |
| `kau status [--json]` | 查看运行内核、配置、状态和 timer |
| `kau kernels [--json]` | 列出已安装内核 |
| `kau doctor [--method build\|package]` | 检查主机依赖和配置 |
| `kau self-update [选项]` | 更新 `kau` 自身 |
| `kau enable --now` | 启用并启动 timer |
| `kau disable` | 停用 timer |

全局参数：

| 参数 | 说明 |
| --- | --- |
| `--config PATH` | 使用指定配置文件 |
| `-v` / `--verbose` | 增加日志详细程度，可重复 |
| `-q` / `--quiet` | 只显示错误 |
| `--version` | 显示版本 |

## `kau update` 参数

| 参数 | 适用方式 | 说明 |
| --- | --- | --- |
| `--dry-run` | 两者 | 显示计划，不安装 |
| `-y` / `--yes` | 两者 | 不询问确认 |
| `--reboot` | 两者 | 成功后重启 |
| `--force` | 两者 | 目标不更新时仍安装；`build` 中可绕过 Secure Boot 检查 |
| `--method build\|package` | 两者 | 本次覆盖更新方式 |
| `--to VERSION` | `build` | 安装指定 kernel.org 版本 |
| `--defconfig` | `build` | 本次使用 `make defconfig` |
| `--jobs N` | `build` | 本次覆盖并行编译数 |
| `--keep-build` | `build` | 复用已有源码目录 |

`--force` 不会让未签名内核在 Secure Boot 下变得可启动，也不会修复编译或依赖问题。

## 首次更新

先预览：

```bash
sudo kau update --dry-run
```

执行更新：

```bash
sudo kau update -y
```

然后重启：

```bash
sudo reboot
```

重启后确认：

```bash
uname -r
kau kernels
kau status
```

`package` 模式的耗时主要取决于软件源速度。`build` 模式首次编译通常需要 10–60 分钟，具体取决于 CPU、内存和配置。

## 常用工作流

### 只检查，不安装

```bash
sudo kau check
```

有可用更新时退出码为 `10`，无更新时为 `0`。

### 输出 JSON

```bash
sudo kau check --json
sudo kau status --json
sudo kau kernels --json
```

适合脚本或监控系统解析。

### 使用缓存检查 `build` 更新

```bash
sudo kau check --offline
```

离线模式只适用于 `build`，需要本地已有发布信息缓存。

### 临时改用源码编译

```bash
sudo kau update --method build --dry-run
sudo kau update --method build -y
```

### 指定上游版本

```bash
sudo kau update --method build --to 7.3.1 --dry-run
sudo kau update --method build --to 7.3.1 -y
```

`--to` 只适用于 `build`。稳定版不在 release feed 中时，工具会按 kernel.org CDN 规则生成下载地址。

### 安装后自动重启

```bash
sudo kau update -y --reboot
```

重启会使用配置中的 `reboot_delay_minutes` 延迟。

## 定时自动更新

启用 timer：

```bash
sudo kau enable --now
```

停用 timer：

```bash
sudo kau disable
```

查看状态：

```bash
systemctl list-timers kernel-autoupdater.timer
systemctl status kernel-autoupdater.timer
journalctl -u kernel-autoupdater.service -n 100 --no-pager
```

Timer 每次调用：

```text
kau auto
```

行为取决于配置：

- `auto_update = false`：只检查并记录
- `auto_update = true`：发现更新后自动安装
- `auto_reboot = true`：安装成功后按延迟重启

如果已安装新内核但尚未重启，`kau auto` 不会反复安装。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功或无可用更新 |
| `1` | 更新、检查或运行失败 |
| `2` | 参数或配置错误 |
| `10` | `check` 或 `self-update --check` 发现更新 |
| `130` | 用户中断 |

## 内核列表标记

`kau kernels` 输出示例：

```text
* 6.8.0-80-generic
  6.8.0-79-generic [kau]
  6.5.0-45-generic
```

- `*`：当前运行内核
- `[kau]`：由本工具安装的内核

`package` 模式安装的发行版内核也可能被标记，具体取决于状态记录和内核包版本匹配结果。
