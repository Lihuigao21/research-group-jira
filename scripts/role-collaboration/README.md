# 角色协作 HTTP 验收

从仓库统一入口运行，不直接执行辅助文件：

```powershell
python scripts/verify-role-http.py all --base http://localhost:8089 --credentials tasks/local-acceptance/work/credentials.json --container rg-role-candidate-backend-1 --task-dir tasks/local-acceptance --attempt review-01
```

`--check-paths` 只验证输入及输出路径边界，不创建文件、不连接 HTTP，也不调用 Docker。
每个 attempt 只能使用一次。回执写入 `<task-dir>/records/role-http/<attempt>/`，
临时文件写入同一任务的 `work/role-http/<attempt>/`。失败后保留原 attempt，另取新名称。

目标必须使用显式的 loopback HTTP 端口，且不能是 8088。运行前会核实所选容器和站点
使用独立的 `rg_candidate_` 数据库，外部 origin 匹配，已启用 `allow_tests`，
应用邮件仅捕获、原生邮件静音，AI 处于离线 mock 模式。入口不会开启生产配置。
容器和源码检查不能替代真实 HTTP 断言。

`all` 按顺序执行 `fixtures`、`accounts`、`portals`、`notifications`、`meals`、
`manuscripts` 和 `realtime`。这些套件会创建标明虚构的账号与记录；`fixtures` 只为
四个既有演示身份准备已验证的虚构联系邮箱。账号套件通过私密管道检查捕获的认证链接
及原生会话竞态，不打印或保存这些秘密。账号夹具仅接受本次随机生成的 `rgah...`
前缀。其他 HTTP 套件保留新建记录 ID 供审阅。门户套件会恢复初始首页介绍和导航，
并将本次虚构公告下线。

将示例中的 `all` 换成单个套件名称即可独立运行。`legacy-core`、`legacy-surveys`
和 `legacy-ai` 通过包装器保留已有断言。原有 `scripts/verify-http.py` 与
`scripts/verify-surveys-http.py` 保持不变。复制的 `legacy-ai-source.py` 是只读历史
输入；包装器只在内存中重新绑定凭据、目标、临时目录及错误 origin 测试，不调用该历史
源码原有的输出入口。

`native-envelope` 是独立可选套件，不在上述 `all` 七套件中。它创建合法 JSON 文档
超过 100 KB 的虚构问卷，验证原生 insert、Desk 保存、发布、关闭，以及完整选项的
保存重读；随后验证藏在大文档或数组末项中的内部表 `doc`／`docs`／`data` 请求均返回
HTTP 403。运行时将示例中的 `all` 换成 `native-envelope`，并使用新的 attempt 名称。

显式指定的私密 JSON 可采用既有“邮箱到密码”的对象，也可将该对象放在 `passwords`
键下。它必须包含四个 `.invalid` 演示账号。`legacy-core` 还需要同一私密文件中的
`Administrator` 键；其余套件不要求该键。不要为此修改原演示凭据文件。入口不接受命令行
明文密码，不隐式读取 `deploy/.env`，也不把凭据写入回执。账号测试的新密码每次启动
随机生成。私密 JSON 不得提交到 Git。账号套件还为本次虚构组员和非组员生成临时
API token，通过真实 Authorization 请求验证原生身份端点；token 只经私有管道传递，
并在 finally 中撤销。临时停用仅更改本次成员的 active，清理时恢复，User 保持启用。

回执只记录检查名称、状态码、布尔值、记录 ID 和哈希，不记录凭据、会话或 cookie 值、
CSRF、一次性键、邮件或选段正文、原始 HTTP 响应。捕获成功不代表 SMTP 已接受或实际
送达。离线 mock 验收不能证明真实模型质量，也不授权付费调用。这些脚本不能替代
桌面及 320px／390px 的实际浏览器验收。

`source-map.json` 记录每份晋升副本对应的原执行源码原字节 SHA-256 及适配内容。
work 源码和此前回执保持不变。入口静态检查通过，不代表晋升后的入口已经完成真实运行。

公开仓库不带内部 tasks 目录。请先为自己的测试建立 `tasks/local-acceptance/TASK.md`，凭据与输出均留在被忽略的 tasks/ 内。source-map.json 中的历史 source 路径仅用于记录晋升来源，当前执行使用 scripts/ 下的副本。
