# 本机依赖镜像替代路径

当 DockerHub 或镜像代理的大文件域名无法连接时，可用本机已缓存的 Python 基础层和 Debian 签名软件包构建 MariaDB、Redis。这里不会修改业务源码或 `deploy/base/`，也不会把替代版本标成官方镜像版本。

这条路径首次仍需要访问清华 Debian HTTPS 镜像；它绕过数据库、Redis 及 Dockerfile frontend 的 registry 下载。两个 Containerfile 都没有外部 `# syntax=` 指令，且固定同一个已缓存基础镜像：

```
python:3.14-slim-trixie@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d
```

APT 使用 Debian 自带 keyring 校验 Release 签名和软件包哈希，没有关闭 HTTPS 或签名检查。直接包版本固定；传递依赖随签名源变化，实际全量版本保存在镜像 `/usr/local/share/research-group-dependencies/packages.txt`。上游若移除指定版本，构建会失败，不会自动升级包。

## 构建及选择

在仓库根目录执行。本机已构建并用下述明确版本完成运行验收；新机器仍需执行自己的构建和检查：

```powershell
docker build --pull=false -f deploy/dependencies/mariadb/Containerfile -t research-group-mariadb:11.8.6-debian13 .
docker build --pull=false -f deploy/dependencies/redis/Containerfile -t research-group-redis:8.0.2-debian13u3 .
docker compose --env-file deploy/.env --env-file deploy/dependencies/fallback.env.example -f deploy/compose.yaml config --quiet
docker compose --env-file deploy/.env --env-file deploy/dependencies/fallback.env.example -f deploy/compose.yaml up -d db redis-cache redis-queue
```

然后按主 README 继续初始化、启动应用。后续所有 Compose 命令都需带这两个 `--env-file`，或把下列设置加入私有 `deploy/.env`：

```dotenv
RG_DB_IMAGE=research-group-mariadb:11.8.6-debian13
RG_REDIS_IMAGE=research-group-redis:8.0.2-debian13u3
RG_DEPENDENCY_PULL_POLICY=never
```

没有这些设置时仍使用官方 `mariadb:11.8.3`、`redis:7.4.2-alpine`，默认缺失才拉取。`never` 防止已构建的替代镜像再次触发 registry 访问。

MariaDB 固定 Debian 包 `1:11.8.6-0+deb13u1`。Redis 固定 Debian security 包 `5:8.0.2-3+deb13u3`，服务版本 8.0.2，满足 Frappe 要求的 Redis >=6；它与默认 Redis 7.4.2 有版本差异，不能把这两个镜像重标为默认官方标签。

## 初始化、数据与健康检查

MariaDB 使用固定官方提交的入口和健康检查，来源及许可见 [PROVENANCE.md](PROVENANCE.md)。保留 `MARIADB_ROOT_PASSWORD`、`/var/lib/mysql` bind、`healthcheck.sh --connect --innodb_initialized` 语义。第一次启动在空 datadir 初始化；已有 `mysql/` 时沿用原数据，环境变量中的新密码不会重设现有密码。没有启用 `MARIADB_AUTO_UPGRADE`。不自动迁移既有 datadir，也不提供降级保证。

Debian 安装过程中生成的镜像内空库会被移除；此步骤发生在镜像构建层，不会接触主机 `runtime/db`。运行时官方入口创建目录、设置 mysql 所有权，再降权运行。容器配置显式使用 `/run/mysqld/mysqld.sock`，监听容器网络，沿用 Compose 的 utf8mb4 和隔离参数。

Redis 最小入口将 `/data` 的文件交给 Debian 的 redis 用户后降权运行，工作目录和 RDB/AOF 均在 `/data`；Compose 的 queue AOF 和 cache 内存策略保留。它显式允许容器间连接（`--protected-mode no`），因此此方案依赖 Compose 内网隔离；Compose 没有发布 MariaDB/Redis 主机端口。不要另行暴露它们的端口。镜像包含 `redis-cli ping` 健康检查。

已有数据切换前先按主 README 备份。尤其不要把 Redis 8 写出的 AOF/RDB 或 MariaDB 11.8.6 使用后的 datadir 直接交给较低版本；回退应使用验证过的备份和独立数据目录。

## 完全离线复用

成功构建并验收后，可在本项目目录保存镜像归档，例如：

```powershell
docker save -o tasks/20261106-frappe-v1/work/dependency-images.tar research-group-mariadb:11.8.6-debian13 research-group-redis:8.0.2-debian13u3
Get-FileHash tasks/20261106-frappe-v1/work/dependency-images.tar -Algorithm SHA256
```

目标机 `docker load -i ...` 后用 `RG_DEPENDENCY_PULL_POLICY=never` 启动，即不需要 registry 或 APT。基础镜像未缓存且没有镜像归档时，不能声称首次构建完全离线。只固定直接包和基础镜像摘要也不等于所有传递依赖逐字节可重建；归档及其 SHA-256 才固定这次实际镜像。

## 验收命令

```powershell
docker compose --env-file deploy/.env --env-file deploy/dependencies/fallback.env.example -f deploy/compose.yaml ps
docker compose --env-file deploy/.env --env-file deploy/dependencies/fallback.env.example -f deploy/compose.yaml exec db healthcheck.sh --connect --innodb_initialized
docker compose --env-file deploy/.env --env-file deploy/dependencies/fallback.env.example -f deploy/compose.yaml exec db mariadbd --version
docker compose --env-file deploy/.env --env-file deploy/dependencies/fallback.env.example -f deploy/compose.yaml exec redis-queue redis-cli ping
docker compose --env-file deploy/.env --env-file deploy/dependencies/fallback.env.example -f deploy/compose.yaml exec redis-queue redis-server --version
```

本机已通过真实 Frappe 安装、38项测试、55项HTTP及浏览器核心流程检查、独立站点备份恢复和七服务重启验证。MariaDB记录及文件关联重启后保持一致，Redis queue AOF探针跨重启保留；镜像ID、实时连接与完整证据见 [验证说明](../../docs/validation.md)。这不代表默认官方镜像组合或其它机器已通过验收。
