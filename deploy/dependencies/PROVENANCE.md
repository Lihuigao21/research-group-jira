# 依赖来源与本地适配

记录日期：2026-11-06。

| 项目 | 固定来源 |
| --- | --- |
| 基础镜像 | `python:3.14-slim-trixie@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d`（本机缓存） |
| MariaDB Debian 包 | [trixie mariadb-server](https://packages.debian.org/trixie/mariadb-server)，`1:11.8.6-0+deb13u1` |
| Redis Debian 包 | [trixie redis-server](https://packages.debian.org/trixie/redis-server)，`5:8.0.2-3+deb13u3`（security） |
| MariaDB 官方脚本仓库 | [MariaDB/mariadb-docker](https://github.com/MariaDB/mariadb-docker/tree/1a4c8e99f7816c8d97df11eee52098522dc770cf)，提交 `1a4c8e99f7816c8d97df11eee52098522dc770cf` |

`mariadb/vendor/` 保存官方提交的原始字节，均未改动：

| 文件 | 上游路径 | SHA-256 |
| --- | --- | --- |
| docker-entrypoint.sh | `11.8/docker-entrypoint.sh` | `d98f4b3febace529b7042bf2880777bfcebdd691d1a122c50dd59ee40963a7d2` |
| healthcheck.sh | `11.8/healthcheck.sh` | `592f89cb39308f8425693221b0476932918e2f3e451cad39b822e23ea0103ef0` |
| LICENSE | `LICENSE` | `ab15fd526bd8dd18a9e77ebc139656bf4d33e97fc7238cd11bf60e2b9b8666c6` |

许可为随仓库分发的 GPL-2.0，全文保存在 `mariadb/vendor/LICENSE`；镜像也保留原源码及许可。Debian 包自身的许可和版权信息保存在镜像 `/usr/share/doc/`。

官方 11.8 分支脚本的 `_mariadb_version()` 固定返回 `11.8.9-MariaDB`。`prepare-entrypoint.py` 在校验全部 vendor SHA-256 后，只把该函数的常量改为实际固定包的 `11.8.6-MariaDB`，并在生成脚本开头写明修改日期、内容和原始源码位置。没有更改初始化 SQL、密码处理、datadir 判断或健康检查。包版本变更会被拒绝，需重新审核。

其余适配均在独立文件：Debian HTTPS deb822 源（继续使用 Debian archive keyring）、MariaDB 容器网络/socket 配置、抑制构建时服务启动的 policy-rc.d、Redis 最小降权入口。未引入 registry 前端镜像、未使用 `--allow-unauthenticated`、未关闭 TLS 校验。
