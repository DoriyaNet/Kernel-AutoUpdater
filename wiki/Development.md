# 开发与测试

## 开发环境

- Python 3.9+
- 无需第三方运行时依赖
- 主要面向 Linux；部分主机探测功能依赖 Linux 路径和命令

## 运行测试

```bash
python -m unittest discover -s tests -t . -v
```

直接运行源码：

```bash
PYTHONPATH=src python -m kernel_autoupdater --help
PYTHONPATH=src python -m kernel_autoupdater check --json
```

## 目录结构

```text
src/kernel_autoupdater/
tests/
packaging/systemd/
packaging/kernel-autoupdater.logrotate
packaging/config.ini
scripts/install.sh
scripts/uninstall.sh
install.sh
```

## 主要模块

| 模块 | 职责 |
| --- | --- |
| `versioning.py` | 内核版本解析、排序和 channel |
| `kernelorg.py` | release feed、校验和、下载 |
| `packages.py` | apt / dnf 内核包与候选版本 |
| `builder.py` | 预检、构建、安装、状态和回滚 |
| `system.py` | 发行版、配置、initramfs、Secure Boot 探测 |
| `doctor.py` | 环境自检 |
| `config.py` | 配置加载与校验 |
| `state.py` | 状态持久化 |
| `config_editor.py` | 保留注释的配置编辑 |
| `menu.py` | curses TUI |
| `selfupdate.py` | 从 GitHub 更新 `kau` |
| `cli.py` | 子命令和参数解析 |

## 更新流程概览

```text
check
  -> 读取配置
  -> package: 查询 apt / dnf 候选包
  -> build:   获取 kernel.org release feed
  -> 比较当前版本并输出结果

update
  -> 预检环境
  -> package: 刷新索引、安装内核包
  -> build:   下载、校验、配置、编译、安装
  -> 更新 initramfs / 引导项
  -> 写入状态
```

## 安装产物

`scripts/install.sh` 使用 Python `zipapp` 将 `src/kernel_autoupdater` 打包为单文件 `/usr/local/bin/kau`，并安装 systemd、logrotate、配置和文档文件。

`install.sh` 是面向远程安装的入口脚本，会下载指定分支或 tag 的源码包，并调用其中的 `scripts/install.sh`。

## 版本发布前检查

- 同步 `pyproject.toml` 和 `src/kernel_autoupdater/__init__.py` 中的版本号
- 运行完整测试
- 检查安装脚本和 systemd 单元
- 在支持的发行版上验证 `package` 模式
- 在测试机或虚拟机中验证 `build` 模式
- 更新 Wiki 中过时的说明

## 文档

- [安装与升级](Installation)
- [更新方式](Update-Methods)
- [配置说明](Configuration)
- [使用指南](Usage)
- [运维与回滚](Operations)
