# 第三方材料

- Frappe和Bench依赖由固定构建配方安装，分别遵守上游项目许可证；其完整源代码不在本仓库中复制。`deploy/base/Containerfile`改编自固定提交的frappe_docker配方，原MIT版权和许可全文保存在 [FRAPPE-DOCKER-LICENSE](../deploy/base/FRAPPE-DOCKER-LICENSE)。
- MariaDB容器辅助脚本保留在 `deploy/dependencies/mariadb/vendor/`，同时保留该目录的LICENSE及 `deploy/dependencies/PROVENANCE.md`。
- Apache POI的PPT/PPTX测试夹具保留许可证、NOTICE及来源摘要，见 `src/research_group/research_group/tests/fixtures/`。
- 工作餐保留7份主餐、7种加料及菜单数据。原7张餐品照片无再分发许可记录，因此不包含在公开仓库中。部署者可将有权使用的照片放在 `src/research_group/research_group/public/menu/2026-11-07/meal-1.jpg` 至 `meal-7.jpg`；无照片时菜单和点餐逻辑仍存在，但图片不可用，完整餐品图片测试须先提供这些文件。照片路径默认被Git忽略，防止把本地业务照片误提交。
- 应用既有MIT许可证保留于 `src/research_group/license.txt`，根目录LICENSE是同文副本。各第三方材料的许可证不被本项目MIT声明取代。
