# Frappe 基础运行镜像

本地源码可直接编辑。此镜像提供 Linux 运行依赖；原生 WSL/Ubuntu + Bench 开发也可使用同一 app 源码。

构建配方来自 [frappe/frappe_docker](https://github.com/frappe/frappe_docker/tree/74421fd01047290635742d3fb85f7fd7eebbde86)，固定资源提交 `74421fd01047290635742d3fb85f7fd7eebbde86`，Frappe `v16.55.2`、Python3.14、Node24。自定义包括 Debian/PyPI 清华 HTTPS 镜像、npm 镜像、拆分下载层和下载失败检查；保留 apt 签名和包校验。为减少体积关闭 Chromium，第一版不提供浏览器生成 PDF 的功能。

Windows PowerShell：`./deploy/base/build.ps1`。Linux：`bash deploy/base/build.sh`。需要已就绪的 Docker 和 Git。上下文写入本项目 `runtime/base-context`；已有目录的版本不符时停止，不自动覆盖。

Debian/Python/Node 的上游依赖按锁定配方解析，具体镜像 ID、解析版本及构建日志见本次验收记录；此配方不保证跨日期逐字节相同。

改编配方的上游MIT版权与许可全文见 [FRAPPE-DOCKER-LICENSE](FRAPPE-DOCKER-LICENSE)；来源为上文固定提交的LICENSE原字节。
