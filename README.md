# NAS Image Mirror

面向 NAS 代安装与长期维护的 OCI/Docker 镜像同步项目。一个源镜像 tag 会连同完整的 multi-platform manifest 一起复制，AMD64 和 ARM64 客户端使用同一个目标地址即可自动选择架构。

## 设计目标

- 自建 Registry 作为长期主仓库，阿里云 ACR 作为可选国内副本。
- 使用 `images.yml` 管理镜像来源、目标路径、版本和必须支持的平台。
- 使用 `skopeo copy --all` 流式复制，不占满 GitHub Runner 的 Docker 磁盘。
- 支持完整同步和仅同步发生变化的清单项。
- 支持按镜像 ID 精确重试一个或多个失败项。
- 登录凭据只保存在 GitHub Actions Secrets 中。
- 固定版本与滚动 tag 可以同时存在；生产安装优先使用固定版本。

## 镜像清单

```yaml
version: 1
images:
  - id: immich-server
    source: ghcr.io/immich-app/immich-server:v3.1.0
    targets:
      home: immich/server:v3.1.0
      aliyun: immich-server:v3.1.0
    required_platforms:
      - linux/amd64
      - linux/arm64
```

`home` 可以保留 `immich/server` 这样的分层路径。阿里云个人版通常按单个命名空间管理仓库，所以示例使用扁平名称 `immich-server`。

`required_platforms` 会在复制前验证。如果源 tag 缺少其中任一架构，本次镜像会失败，而不是悄悄发布不完整的 tag。未设置时会原样复制上游提供的所有平台。

## GitHub 配置

在 `Settings → Secrets and variables → Actions` 添加：

| Secret | 用途 | 必需 |
| --- | --- | --- |
| `HOME_REGISTRY` | 自建仓库域名，不含 `https://` | 使用 home 时 |
| `HOME_NAMESPACE` | 可选公共路径前缀 | 否 |
| `HOME_REGISTRY_USER` | 自建仓库用户名 | 使用 home 时 |
| `HOME_REGISTRY_PASSWORD` | 自建仓库密码 | 使用 home 时 |
| `ALIYUN_REGISTRY` | 阿里云 ACR 地址 | 使用 aliyun 时 |
| `ALIYUN_NAME_SPACE` | 阿里云命名空间 | 使用 aliyun 时 |
| `ALIYUN_REGISTRY_USER` | 阿里云用户名 | 使用 aliyun 时 |
| `ALIYUN_REGISTRY_PASSWORD` | 阿里云密码 | 使用 aliyun 时 |

默认情况下，推送和定时事件不会真正同步。完成 Secrets 配置和首次手动验证后，再添加 Repository variable：

```text
MIRROR_ENABLED=true
```

## 使用方式

1. 修改 `images.yml` 并提交。
2. 打开 `Actions → Mirror OCI images → Run workflow`。
3. 第一次选择 `dry_run=true` 检查计划。
4. 确认无误后关闭 dry run，选择 `home`、`aliyun` 或两者。

如果只需重试失败镜像，在 `image_ids` 中填写清单 ID；多个 ID 用逗号分隔，例如 `postgres-18,valkey-9`。该输入非空时会覆盖 `scope`。

自建仓库最终使用方式：

```bash
docker login registry.example.com
docker pull registry.example.com/immich/server:v3.1.0
docker pull registry.example.com/common/valkey:9
```

当前 home 仓库的全部完整拉取地址（包括 `latest` 或固定版本 tag）可直接查看
[`pulls/home/README.md`](pulls/home/README.md)，纯命令列表见
[`pulls/home/docker-pull-commands.txt`](pulls/home/docker-pull-commands.txt)。

## 本地校验

```bash
python3 -m pip install -r requirements.txt
python3 scripts/mirror.py validate
python3 scripts/generate_home_pulls.py --check
python3 scripts/mirror.py mirror --scope all --destinations home --dry-run
python3 scripts/mirror.py mirror --image-ids postgres-18 --destinations home --dry-run
python3 -m unittest discover -v
```

## 安全原则

- 不要把 Registry、ACR、VPS 或 GitHub 密码写入代码和清单。
- 公网 Registry 必须使用可信 TLS 证书。
- 外部 PR 不会执行带仓库 Secrets 的镜像同步。
- 删除清单项不会自动删除仓库数据，避免误删；清理应在 Registry 端单独审批执行。

## 来源与许可

本项目迁移并重写了 [`tech-shrimp/docker_image_pusher`](https://github.com/tech-shrimp/docker_image_pusher) 的部分设计思想，保留 Apache License 2.0 和相应署名，详见 `NOTICE`。
