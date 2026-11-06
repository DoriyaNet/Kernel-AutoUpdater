# 配置说明

主配置文件：

```text
/etc/kernel-autoupdater/config.ini
```

安装器首次安装时写入默认配置，之后升级会保留现有文件。

也可以通过全局参数或环境变量使用其他配置：

```bash
sudo kau --config /path/to/config.ini status
sudo KAU_CONFIG=/path/to/config.ini kau status
```

## 完整示例

```ini
[updater]
update_method = package
channel = stable
feed_url = https://www.kernel.org/releases.json
feed_timeout = 30
download_timeout = 120
work_dir = /var/lib/kernel-autoupdater
log_file = /var/log/kernel-autoupdater.log

auto_update = false
auto_reboot = false
reboot_delay_minutes = 5
min_free_gb = 12

[source]
verify_pgp = false
pgp_keyring = /etc/kernel-autoupdater/kernel.org-keyring.gpg
allow_unverified = false
patches_dir = /etc/kernel-autoupdater/patches

[build]
config_source = auto
localversion = -kau
jobs = 0
keep_build_dir = false
clear_distro_keys = true

[packages]
manager = auto
kernel_packages = auto
refresh_package_index = true
min_free_gb = 2

[install]
update_initramfs = true
update_bootloader = true
```

## `[updater]`

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `update_method` | `package` | `package` 或 `build` |
| `channel` | `stable` | `stable`、`longterm`、`longterm:6.12`、`mainline` |
| `feed_url` | kernel.org releases.json | 发布信息地址 |
| `feed_timeout` | `30` | 获取发布信息的超时秒数 |
| `download_timeout` | `120` | 下载超时秒数 |
| `work_dir` | `/var/lib/kernel-autoupdater` | 状态、缓存、下载和编译目录 |
| `log_file` | `/var/log/kernel-autoupdater.log` | 日志路径；设为 `none` 或 `off` 可禁用 |
| `auto_update` | `false` | 定时检查发现更新时是否自动安装 |
| `auto_reboot` | `false` | 自动安装成功后是否重启 |
| `reboot_delay_minutes` | `5` | 自动重启延迟分钟数 |
| `min_free_gb` | `12` | `build` 模式最低空闲磁盘空间 |

`channel` 只影响 `build` 模式。

## `[source]`

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `verify_pgp` | `false` | 是否验证 `linux-*.tar.sign` |
| `pgp_keyring` | `/etc/kernel-autoupdater/kernel.org-keyring.gpg` | 本地 kernel.org 公钥环 |
| `allow_unverified` | `false` | 是否允许构建没有校验文件的源码 |
| `patches_dir` | `/etc/kernel-autoupdater/patches` | 本地补丁目录 |

默认始终校验 kernel.org 目录中的 SHA-256，但不验证签名。完整供应链校验的配置方法见 [运维与回滚](Operations#校验与-secure-boot)。

`mainline` / RC 通常没有 `sha256sums.asc` 或 `.tar.sign`，需要同时设置：

```ini
[source]
verify_pgp = false
allow_unverified = true
```

这仅建议用于测试，不建议用于生产系统。

## `[build]`

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `config_source` | `auto` | `auto`、`running`、`defconfig` 或绝对路径 |
| `localversion` | `-kau` | 追加到内核版本后的本地标识 |
| `jobs` | `0` | 并行编译数；`0` 表示按 CPU 核心数 |
| `keep_build_dir` | `false` | 成功后是否保留编译目录 |
| `clear_distro_keys` | `true` | 是否清空发行版签名密钥配置 |

`config_source` 的查找顺序和行为见 [更新方式](Update-Methods#内核配置)。

成功安装后默认删除编译目录以节省空间，失败时会保留。下载的源码包保存在 `work_dir/downloads/`，不会自动删除。

## `[packages]`

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `manager` | `auto` | `auto`、`apt` 或 `dnf` |
| `kernel_packages` | `auto` | 内核包名；多个包可用空格或逗号分隔 |
| `refresh_package_index` | `true` | 安装前是否刷新软件源索引 |
| `min_free_gb` | `2` | `package` 模式最低空闲磁盘空间 |

`kernel_packages = auto` 会沿用当前内核 flavor，并在不可用时回退到发行版默认内核包。需要固定包名时显式设置，例如：

```ini
[packages]
kernel_packages = linux-image-lts, linux-headers-lts
```

此节仅在 `package` 模式使用。

## `[install]`

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `update_initramfs` | `true` | `build` 安装后是否刷新 initramfs |
| `update_bootloader` | `true` | `build` 安装后是否更新 GRUB |

这两项对 `package` 模式无效。发行版内核包的安装后脚本会负责相应更新。

## 配置校验

- 未知配置节会产生警告并被忽略
- 布尔值支持 `true` / `false`、`yes` / `no`、`1` / `0`、`on` / `off`
- 无效枚举、整数或路径会导致命令中止
- TUI 修改配置时会保留原有注释，并在写入后重新加载校验

## 无人值守更新

确认手动更新成功后，可开启自动安装：

```ini
[updater]
auto_update = true
auto_reboot = false
```

生产环境建议保持 `auto_reboot = false`，在维护窗口内手动重启。

检查定时器：

```bash
systemctl list-timers kernel-autoupdater.timer
journalctl -u kernel-autoupdater.service -n 100 --no-pager
```

Timer 默认在开机 15 分钟后首次运行，之后每 24 小时运行一次，并带 0–2 小时随机延迟。
