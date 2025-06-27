# bpsync

同步 buildpack 依赖并上传到 registry 的工具。

## 功能简介

`bpsync` 是一个用于自动化同步 [Cloud Native Buildpacks](https://buildpacks.io/) 依赖到自有 Docker Registry 的工具。它支持多线程下载、上传、断点续传、任务日志记录，并可自动重写 `buildpack.toml` 依赖地址，便于企业内网环境或私有云环境下的依赖管理。

## 安装

```sh
pip install .
```

## 快速开始

安装后可直接在任意目录使用：

```sh
bpsync --help
```

## 命令行参数

| 参数 | 说明 | 示例 |
|------|------|------|
| `--buildpack-toml` | 指定 buildpack.toml 路径，默认为 `buildpack.toml` | `--buildpack-toml cache/gradle/buildpack.toml` |
| `--registry` | 目标 Docker registry（必填），如 `artifactory.example.com/repo1/pkgs` | `--registry my-registry.local/repo` |
| `--username` | registry 用户名（可选） | `--username admin` |
| `--password` | registry 密码（可选） | `--password secret` |
| `--temp-dir` | 下载临时目录，默认为 `tmp_downloads` | `--temp-dir cache/gradle` |
| `--rewrite-toml` | 上传完成后自动重写 buildpack.toml，生成 `buildpack-modified.toml` | `--rewrite-toml` |
| `--download-only` | 只下载依赖，不上传 | `--download-only` |
| `--upload-only` | 只上传依赖（需先下载），可配合 `--rewrite-toml` | `--upload-only` |

**注意：** `--download-only` 和 `--upload-only` 不能同时使用。

## 工作原理

1. 解析 `buildpack.toml`，获取 `metadata.dependencies` 下的所有依赖 URI。
2. 多线程下载所有依赖到本地指定目录，并记录任务日志（断点续传）。
3. 下载完成后，自动上传依赖到指定 registry（支持用户名密码认证）。
4. 上传完成后，可自动重写 `buildpack.toml`，将依赖地址替换为 registry 地址，生成 `buildpack-modified.toml`。
5. 所有任务状态、错误信息均记录在 `task_log.json`，支持中断恢复。

## 典型用法

### 全流程同步并重写 TOML

```sh
bpsync --buildpack-toml cache/gradle/buildpack.toml \
  --registry my-registry.local/repo \
  --username admin --password secret \
  --temp-dir cache/gradle \
  --rewrite-toml
```

### 仅下载依赖

```sh
bpsync --buildpack-toml cache/gradle/buildpack.toml \
  --temp-dir cache/gradle \
  --download-only
```

### 仅上传依赖并重写 TOML

```sh
bpsync --buildpack-toml cache/gradle/buildpack.toml \
  --registry my-registry.local/repo \
  --username admin --password secret \
  --temp-dir cache/gradle \
  --upload-only --rewrite-toml
```

## 任务日志与断点续传

- 所有任务状态保存在 `task_log.json`，支持断点续传。
- 状态包括：`pending`、`downloading`、`downloaded`、`uploading`、`uploaded`、`failed`。
- 失败任务可重试，已完成任务自动跳过。

## 依赖与环境

- Python 3.6+
- 依赖包：`requests`, `toml`
- 需本地安装 `curl` 命令行工具

## 注意事项

- 上传时默认使用 HTTPS，若 registry 不支持请加前缀 `http://`。
- 支持多线程并发（默认 4 线程），可根据需要修改源码 `DEFAULT_WORKERS`。
- 仅支持 `metadata.dependencies` 下的 `uri` 字段同步。
- 上传后生成的 `buildpack-modified.toml` 可直接用于 buildpack 构建。

---

如需进一步定制或集成，请参考源码 `sync_dependencies.py`。
