# 运维说明

## 固定版本与滚动版本

固定版本 tag 不应被改写，例如 `vaultwarden/server:1.34.3`。`latest`、`main`、`release` 等滚动 tag 可以定时同步，但应先在测试环境验证，再用于客户安装。

## 增量同步

推送 `images.yml` 到 `main` 时，脚本会读取上一个提交的清单，以 `id` 为主键，只选择新增或发生变化的项目。同步脚本或工作流自身变更时会执行一次完整同步，以验证新逻辑。

手动运行工作流时，可在 `image_ids` 输入一个或多个逗号分隔的清单 ID。该输入非空时覆盖 `scope`，适合只重试失败镜像，避免重新复制整个清单。

## 删除与垃圾回收

从清单删除镜像不会删除 Registry 中的 manifest 或 blob。长期清理建议分成三步：

1. 在 Registry API 或管理界面删除明确的旧 tag/manifest。
2. 检查仍被保留的版本。
3. 在维护窗口执行 Registry garbage collection。

不要让 GitHub 清单删除自动触发远端删除。

## 目标路径约定

- `common/`：PostgreSQL、Valkey、Tika 等通用组件。
- `<project>/server`：项目主服务。
- `<project>/<component>`：项目专用数据库、机器学习或辅助服务。
- `media/`、`network/`、`storage/`：跨项目但用途明确的服务。

阿里云 ACR 的目标可以使用扁平名称，避免个人版实例的路径限制。
