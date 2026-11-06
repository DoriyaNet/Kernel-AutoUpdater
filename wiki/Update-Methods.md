# 更新方式

`kau` 支持 `package` 和 `build` 两种更新方式。默认使用 `package`。

## 对比

| 项目 | `package` | `build` |
| --- | --- | --- |
| 内核来源 | 发行版 apt / dnf 仓库 | kernel.org 源码 |
| 是否需要编译 | 否 | 是 |
| 典型耗时 | 几十秒到几分钟 | 10–60 分钟 |
| 是否可定制 `.config` | 否 | 是 |
| 是否可应用本地补丁 | 否 | 是 |
| Secure Boot | 发行版签名内核 | 未签名，默认拒绝安装 |
| 初始化与引导项 | 由发行版包脚本维护 | 由 `kau` 调用系统工具刷新 |
| 磁盘需求 | 约 2 GiB | 默认预检 12 GiB |

## 切换更新方式

永久切换：

```ini
[updater]
update_method = package
```

或：

```ini
[updater]
update_method = build
```

只覆盖当前命令：

```bash
sudo kau check --method build
sudo kau update --method build -y
sudo kau update --method package -y
```

也可以在 TUI 主菜单中切换更新方式。

从旧版本升级时，如果配置里显式写了 `update_method = build`，会继续使用源码编译；如果旧配置完全没有这一项，则会采用新的默认值 `package`。需要保留源码编译时请显式补上 `update_method = build`。

## `package` 模式

`package` 模式调用发行版包管理器安装预编译内核：

- Debian / Ubuntu 使用 `apt-get`
- Fedora / RHEL / Rocky / AlmaLinux / CentOS 使用 `dnf`

默认包名：

- apt：`linux-image-<flavor>` 和 `linux-headers-<flavor>`
- dnf：`kernel`

### 内核 flavor

`packages.kernel_packages = auto` 会优先沿用当前运行内核的 flavor，例如：

- `generic`
- `lowlatency`
- `aws`
- `cloud`
- `rt-amd64`

如果 apt 仓库中没有该 flavor，会回退到发行版默认内核包：

- Ubuntu：`linux-image-generic` / `linux-headers-generic`
- Debian：`linux-image-<arch>` / `linux-headers-<arch>`

需要固定包名时可直接设置，例如：

```ini
[packages]
kernel_packages = linux-image-lts, linux-headers-lts
```

### 软件源刷新

- `kau check` 使用本机已有软件源索引，不会主动联网刷新
- `kau update` 和 `kau auto` 在安装前会刷新索引
- 可通过 `packages.refresh_package_index = false` 关闭刷新

### 特点和限制

- 只安装发行版仓库中已有的内核，不能通过 `--to` 指定任意上游版本
- 不使用 `build.config_source`、本地补丁或 `build.localversion`
- `[install]` 中的 initramfs / bootloader 选项对 `package` 模式无效
- 仓库元数据和软件包签名由 apt / dnf 校验
- 安装可能同时更新内核包依赖的其他系统包
- `--to`、`--defconfig`、`--keep-build`、`--jobs` 仅适用于 `build`
- `package` 模式使用 `[packages] min_free_gb`，而不是 `[updater] min_free_gb`

安装完成后通常需要重启才能运行新内核。如果新内核已安装但尚未重启，`check` 会显示等待重启，`auto` 不会重复安装。

## `build` 模式

`build` 模式从 kernel.org 获取源码并在本机编译。

### 更新通道

| 通道 | 说明 |
| --- | --- |
| `stable` | kernel.org 当前 stable 版本 |
| `longterm` | 当前 longterm 版本 |
| `longterm:6.12` | 指定 longterm 系列 |
| `mainline` | 主线版本，可能包含 RC |

配置示例：

```ini
[updater]
update_method = build
channel = longterm
```

### 构建流程

1. 从 kernel.org release feed 选择目标版本
2. 下载 `.tar.xz`，使用目录级 `sha256sums.asc` 校验 SHA-256
3. 可选下载 `linux-*.tar.sign` 并用本地 kernel.org 公钥环验证
4. 读取当前内核配置，执行 `make olddefconfig`
5. 按文件名顺序应用补丁
6. 编译内核和模块
7. 执行 `make modules_install` 和 `make install`
8. 刷新 initramfs 和引导项
9. 保留旧内核并记录状态

`jobs = 0` 表示按 CPU 核心数并行编译。内存较小的机器应手动降低 `jobs`，避免 OOM。

### 内核配置

`build.config_source` 支持：

- `auto` / `running`：查找当前运行内核的配置
- `defconfig`：执行 `make defconfig`
- 绝对路径：使用指定的配置文件

`auto` 会依次检查：

1. `/boot/config-$(uname -r)`
2. `/proc/config.gz`
3. `/lib/modules/$(uname -r)/config`
4. `/usr/lib/modules/$(uname -r)/config`

默认追加 `localversion = -kau`，例如 `7.3.1-kau`，便于识别本工具编译的内核。

Debian / Ubuntu 配置会引用发行版签名密钥路径。默认会清空 `SYSTEM_TRUSTED_KEYS` 和 `SYSTEM_REVOCATION_KEYS`；如果确实需要自定义密钥，可设置 `clear_distro_keys = false` 并准备相应文件。

### 本地补丁

将 `.patch` 或 `.diff` 文件放入：

```text
/etc/kernel-autoupdater/patches/
```

工具会按文件名排序，以 `patch -p1` 应用。任意补丁失败都会在编译前中止更新。

### 编译依赖

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

`kau doctor` 会检查缺失项，但不会自动安装软件包。

### 特点和限制

- 源码来自 kernel.org 原版，不合并发行版补丁、ABI 或 DKMS 改动
- 直接安装到 `/boot` 和 `/lib/modules`，不受 apt / dnf 管理
- 需要发行版补丁时改用 `package` 模式
- 不支持自动为内核做 Secure Boot 签名
- mainline / RC 通常没有校验文件或签名，需要显式允许未校验源码，不建议生产使用
- 成功安装后默认删除编译目录；失败时保留以便排查

## 相关文档

- 配置项：[配置说明](Configuration)
- 首次更新、回滚和 Secure Boot：[运维与回滚](Operations)
