# Research Group Jira · 课题组协作平台

应用源码位于 `src/research_group/`，使用 Frappe **v16.55.2** 的原生登录和 Desk。界面语言为中文，时区为 Asia/Shanghai。功能包含教师、秘书、学生工作台，课题进展、预约与组会材料，实名投票/问卷/工作餐，复旦邮箱申请与账号管理，重要邮件及私有论文版本协作。AI采用 LangChain＋LangGraph，默认离线模拟；真实模型、外网邮件收件与公网发布须单独配置和实测。

源码在本机开发，Docker 提供 Python 3.14、Node 24、MariaDB 和 Redis 运行环境。Docker 可替换；标准 Bench 原生开发方法也列在下方。本仓库发布应用、部署配置和测试源码；已执行验证与尚未联调的范围见 [验证说明](docs/validation.md)。

## 本机启动

先检查 Docker 引擎和实际镜像数据盘容量。本配置绑定 `127.0.0.1:8088`；不会自动部署公网。

1. 从项目根目录执行 `python scripts/create-local-config.py`。它生成被 Git 忽略的 `deploy/.env`、`runtime/secrets/demo-credentials.json`，已有凭据不会被覆盖，口令不会打印到终端。写入失败会回收本次新建的未完成文件，重试不会把它误认为已就绪。
2. 按 [基础镜像说明](deploy/base/README.md) 构建固定 Frappe 基础镜像，Windows 入口为 `deploy/base/build.ps1`，Linux 入口为 `deploy/base/build.sh`。
   若 MariaDB/Redis 镜像拉取受网络限制，可按 [依赖镜像替代路径](deploy/dependencies/README.md) 使用 Debian 签名包构建明确版本的本机镜像。
3. 构建应用并初始化站点：

```sh
docker build -f deploy/Containerfile -t research-group-jira:0.1.0 .
docker compose --env-file deploy/.env -f deploy/compose.yaml config --quiet
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d db redis-cache redis-queue
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm configurator
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm initializer
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d backend websocket worker scheduler frontend
docker compose --env-file deploy/.env -f deploy/compose.yaml exec backend bench --site lab.localhost enable-scheduler
```

Linux 首次使用 bind 目录时，应先创建 `runtime/sites`、`runtime/logs` 并赋予镜像内 UID 1000、GID 0 写权限。MariaDB 与 Redis 由各自官方入口处理数据目录权限。Windows Docker Desktop 的 bind 权限语义不同，勿直接套用 Linux 的 chown。

打开 [本机站点](http://localhost:8088)。管理员账号为 `Administrator`，口令见本机 `deploy/.env` 的 `ADMIN_PASSWORD`。用原生用户管理分配“课题组教师”“课题组学生”“课题组秘书”，并建立启用的“课题组成员”记录后才拥有业务权限。角色可兼任；秘书管理公开排期和实名问卷，不扩大私人研究数据权限。

## 虚构演示数据

演示导入必须显式启用，成员和课题名称均带“虚构”。四个账号包括教师、学生甲、学生乙及秘书兼学生；口令只在本地文件中保存。

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml cp runtime/secrets/demo-credentials.json backend:/home/frappe/frappe-bench/sites/lab.localhost/private/demo-credentials.json
docker compose --env-file deploy/.env -f deploy/compose.yaml exec backend bash /opt/research-group-scripts/seed-demo.sh
```

演示账号为 `teacher.demo@example.invalid`、`student.one.demo@example.invalid`、`student.two.demo@example.invalid`、`secretary.demo@example.invalid`。应用默认不创建邮件账号，关闭队列发信和公开注册；演示邮箱使用保留的 `.invalid` 域名。

## 核心流程与权限

- 教师维护课题方向、优先级与里程碑，在“教师总览”查看全组当前课题、最近进展、阻碍和最近讨论。学生从课题表单追加进展、申请讨论；进展不可覆盖。
- 只有指定教师能批准或拒绝申请。批准事务先锁定教师，再用当前读取检查半开区间是否重叠；结束时间等于另一申请开始时间可以相邻。申请人或指定教师可取消待审批/已批准的讨论。已批准的讨论才可归档，保存结论、教师意见与后续行动；教师可补充自己的意见。
- 教师或秘书保存组会草稿，指定报告学生，再发布。发布者可在草稿报告中更正报告学生并用原生保存，发布后报告人和提交字段不可普通编辑。发布者可调整截止时间，默认组会前一天 23:59。报告学生通过“提交或重传材料”提交主题与 PPT/PPTX/PDF；文件类型按内容验证，默认上限 50MB，重传产生新版本，服务端记录时间与迟交。
- 已发布组会与各版材料供启用的全组成员读取；私人讨论与附件仅本人及教师可读。所有受管附件物理存储为 Frappe 私有文件，列表、REST、直接下载及打包下载采用同一访问范围。普通附件接口不能换父记录、转公开或覆盖版本。

业务账号应使用记录中的专用材料入口；私人讨论材料由本人或指定教师提交。原生通用上传（包括分片）会在写入临时文件前拒绝业务账号，系统运维 Administrator 仍可使用原生上传。附件路径别名和缩略图引用遵循同一受管材料边界；新文件原子分配独立物理名称，原生同内容去重保留，并在接受材料版本前核对持久字节的大小和 SHA256。

上传上限由 `rg_max_file_size`（字节）控制，默认 `52428800`。若调整，需同步请求体上限 `max_file_size`、System Settings 的 MB 值、Nginx `CLIENT_MAX_BODY_SIZE` 及前端提示；请求体应额外留出 multipart 开销。PPTX 解压总量限制 250MB，单次打包下载总量限制 250MB，防止异常文件耗尽资源。

报告主题上限为 200 字，报告与历史版本字段保持相同长度。文件名含扩展名最多 134 个字符、249 个 UTF-8 字节，为原生存储名的 6 字符哈希后缀预留空间，超限会在写入前提示；文件名不能含 `%`、`?`、`#`、路径分隔符或控制字符。旧组会的截止时间不可清空，新报告的迟交状态由服务端提交时间派生。教师总览按每个课题取得最近进展和讨论，材料历史保留全部已存版本。学生时间线与秘书排期按当前账号的行权限查询全部记录；排期只汇总可见组会的报告。

PPT 最低结构验证沿 CurrentUserAtom、UserEditAtom 和持久化映射读取 Document 容器，检查记录类型、边界和偏移。PPTX 验证 OPC 入口、presentation 与其引用部件/幻灯片的关系和目标，并拒绝包外路径及 DTD/实体 XML。测试使用带来源、许可和 SHA256 的真实 Apache POI 文件；这属于最低文件结构验证，未完整复现 PowerPoint 渲染，也不保证识别所有损坏形式。

PPTX 内部资源边界为最多 5,000 个 ZIP 成员、单个 XML 部件 16 MiB、presentation XML 8 MiB、Content Types XML 1 MiB。大小在打开/解析部件前检查；全包共享最多 100,000 个保留条目额度，覆盖关系属性、引用、幻灯片和派生映射，在分配前扣减。解压总量继续限制为 250 MiB。

管理员属于 Frappe 系统运维特权，不是三类业务角色。为保留审计历史，业务账号不具有删除/分享权限。

## 秘书投票与问卷

从中文工作区的“投票与问卷”进入。秘书新建草稿，在题目表中添加单选、多选或简答，指定必填项和可选截止时间。选择题的选项每行一个；单题自动显示为投票，多题为问卷。草稿只对秘书可见，题目可暂未完成；发布时会拒绝空题、重复选项及已过期截止时间，并保留草稿。

秘书点击“发布问卷”后，所有启用的课题组成员均可实名填写，包括普通教师和兼任角色成员。发布后锁定标题、说明、题目与截止时间；需要调整题目时另建问卷。成员在问卷内“填写投票/填写问卷”，截止或关闭前可“修改本人答卷”。每个账号每份问卷只保留一份最新答卷；必填缺失、非法选项或整份验证失败不会保存半份内容。截止以服务器时间为准，等于截止时间也拒绝写入；秘书可“提前关闭”，已有答卷继续保留。

秘书在问卷内查看填写进度、未填名单、选项统计、简答和实名明细，或“导出 CSV”。人数和统计对应每人最新内容；当前启用成员的填写进度与全部已收历史答卷分开显示，停用成员的旧答卷仍保留。CSV 使用 UTF-8 BOM，并对可能被电子表格当作公式的单元格加前导单引号。“问卷答卷”入口同样使用服务端行权限：普通成员仅见本人，普通教师也不能读取他人答卷；只有启用的秘书可查看实名明细、统计与导出。

权限同时覆盖原生页面、REST、列表及专用接口。普通 CRUD 不能伪造填写人、服务器时间或状态；答卷写入与关闭共用问卷父行锁，并以 `(survey, respondent)` 数据库唯一约束兜底。账号被停用、成员取消启用或业务角色移除后，直接接口和列表权限也随之失效。问卷不含附件、匿名访客、条件跳题或第三方集成；本轮增加站内重要邮件和截止提醒，默认隔离捕获，外网实际收件仍须单独验证。

输入上限为每份 100 题、每题 100 个选项，标题 200 字、题目 500 字、单个选项 200 字、说明及每题简答 5,000 字。空截止表示由秘书手动关闭。升级现有站点时使用同版源码镜像、`bench --site lab.localhost migrate` 和前端构建；迁移只增加新表、索引和唯一约束，不覆盖旧业务数据。历史验证摘要见 [验证说明](docs/validation.md)，未执行的检查不算通过。

## 角色工作台、账号、通知与协作

入口为 `/desk/rg-teacher-portal`、`/desk/rg-secretary-portal` 和 `/desk/rg-student-portal`。兼任学生/秘书的账号可在同一登录中切换；切换只改变界面，服务端每次重新核验真实身份。教师可按学生查看正式进展、讨论和来源时间，维护方向目标/优先级及关联课题。秘书将投票、问卷和工作餐归入信息收集；首页维护只接收纯文本、固定批准栏目名称/顺序/显隐及站内导航，不提供代码、部署或系统权限。

自助入组入口 `/rg_join` 只允许精确邮箱域名 `m.fudan.edu.cn`、`fudan.edu.cn`，不收校园密码。邮箱验证后仍须秘书审批；批准后本人经一次性链接设置网站密码。原生公开注册保持关闭，既有 User 不会被自动接管。登录 ID 保持初始邮箱；本人资料可更改姓名、密码、经验证的联系邮箱，通知及找回密码使用已验证联系邮箱。换邮箱需先验证现密码、新邮箱确认，并告知旧邮箱；旧重置键及其他会话被撤销。教师、秘书、系统账号由管理员维护，秘书专用入口仅管理普通学生；旧邮箱失效又忘记密码需管理员核实。

账号管理当前显示前200位组员，尚无后续分页；通知成员选择最多支持500位启用组员，超过该规模时包括单人选择在内都会提示规模限制。本轮按单课题组使用验收，不承诺机构级名录管理。被拒申请保留审计，但释放学号与邮箱预留，允许重新核实申请；非课题组的原生账号继续使用Frappe原生密码恢复。

账号使用至少12字符且Frappe强度评分4的网站密码，8小时空闲会话和登录/注册/出信节流。一次性密钥只存摘要，链接GET不消费，显式POST确认。2FA要在真实邮箱初始化和应急恢复实测后另行启用；当前没有把2FA能力写成通过。

教师/秘书从启用组员选择收件人，先完整预览主题、正文及名单，再由本人确认；每位收件人独立投递，固定From，不暴露其他收件人。自动验证、审批、预约状态、组会/工作餐及截止提醒进入同一提交后队列。调度需运行`bench schedule`及worker并启用scheduler；测试期间可暂停调度。默认未配置发送服务时显示未配置；隔离模式`rg_mail_mode=capture`仅保留本地捕获，未向外发送。SMTP接受、明确失败和投递结果未知分别记录；未知结果禁止盲目重发。SMTP连接和凭据只由管理员在服务端设置，实际外网收件另验。

本人及有权管理普通学生的秘书可在账号页核对自动邮件状态，并对页面标明可恢复的已知失败或未配置事件进行有界恢复。未入组的申请者继续收到统一响应，其注册验证投递状态由已登录秘书核对，避免公开查询申请身份。验证和设密通知会签发新链接；过期、来源变化、撤权、已提交或结果未知的事件不能重发。单事件实际尝试达到3次后停止恢复，同一目标每小时最多请求恢复5次。排队截止提醒在实际投递前再次核验来源状态、截止时间和本人是否已经提交。

工作餐入口 `/desk/rg-meals` 保留7份主餐、7种加料、7图及完整配菜。秘书可编辑草稿并发布冻结菜单，按轮次设截止或关闭。一人一轮一单，截止前跨设备修改同单；金额由服务端按整数分计算，数量1–20、合法主食且至少一份主餐，严格超过50元须本人确认。旧版本/截止/关闭失败保留原单。秘书可作废留痕、查看逐人/菜品/主食统计、导出CSV、最多50轮ZIP及商家文本。原独立点餐服务和历史订单不导入、不修改。

当前工作餐页面列出最近50轮，群发历史列出最近20条（含预览），尚无历史翻页；已知ID仍可通过原有授权接口查询。ZIP最多50轮是批次数量限制，未做每轮数千订单的大规模压力验收；较大历史请分批导出。人工意见和回复每次提交都会追加，丢失成功回执后重试可能产生重复条目。

论文入口 `/desk/rg-manuscripts` 仅作者学生与指定教师可见，秘书身份本身无权访问。私有PDF/DOCX上传产生不可变版本，记录父版、时间、大小及原字节SHA256；教师意见、学生回复和处理状态保留历史。DOCX检查OPC结构，PDF结构/选段解析在受CPU、内存、墙钟和解码预算限制的固定子进程中执行。旧版下载、File/Version和直接链接沿同一权限，失败上传回滚记录并补偿本次新物理文件。首版不含Word修订合并或在线多人正文编辑。

论文页可查看并撤销本人在当前可读论文上的选区，包括已自然过期但仍绑定已保存建议的选区；有效选区最多20个，可在同页管理释放额度。撤销只撤回段落授权并屏蔽派生AI建议及回复，保留原稿和人工意见。人工意见的处理、关闭和学生回复引起的状态变化追加操作者、时间与前后状态；旧记录未保存的过去事件不会补造。

当前选段提取器为 `rg-excerpt-2`，保留Word制表符、换行和文本框段落。升级后旧提取器创建的选区及派生AI建议不再授权展示或调用，需重新选择文字；原稿和人工意见保留。

AI仅处理本人明确选择并预览的授权论文段落，绑定版本、原文摘要和选区；不自动发送整篇。建议以原文/建议/理由和来源展示，本人确认后保存为AI辅助Review，原稿不覆盖。限时选区过期阻止新模型读取和确认；已确认历史仍保留，但显式撤销、来源失权或文件失效会屏蔽派生内容。

源码快照绑定与完整后端、HTTP、浏览器验证摘要见 [验证说明](docs/validation.md)；原始内部运行回执不随公开仓库发布。源码存在和离线捕获不代表真实成员、真实模型质量或外网邮件已完成验收。

## 我的 AI 助手

中文工作区新增“我的 AI 助手”，也可访问 `/desk/rg-ai-assistant`。三种业务身份共用服务端模型配置，各自的聊天、记忆和工作计划只经本人专用接口读取；教师查询正式研究记录，不读取学生私人内容。运维仍有数据库和备份维护权限。

缺省 `rg_ai_mode=mock`，页面和每条答复均标为“离线模拟”。它不需要key、不创建模型网络客户端，不代表真实模型理解能力或科研建议质量。教师总览按成员分页，显示日期、总人数、已覆盖人数和未展开范围；教师反馈与学生整理的讨论结论/行动分别标注。学生先选择课题，再按正式反馈安排工作；多课题不明确时要求选择。秘书只取得获准排期、问卷数量和选项汇总，模型不读取PPT正文、实名简答或CSV。问卷单轮最多聚合400份答卷、20道题，超出时说明范围并转原页面。

保存/修改/删除记忆或计划、追加进展和预约申请都先产生操作卡。本人核对具体内容并点击“确认内容并提交”后才写入；进展须本人确认真实，个人计划完成不代表科研成功或正式进展。预约成功回执仍为“待审批”，助手无审批或发布工具。卡片15分钟过期；确认只提交操作ID与决策，不能重传参数。重复确认返回同一无正文记录ID/状态回执，不再创建第二条业务记录。

跨会话记忆可关闭。未确认候选7天过期；回答偏好到本人修改/删除前有效，背景与待跟进事项默认30天；计划保留并显示过期依据。模型不会从闲聊自动积累私人画像。本实现按有界最近历史回放，不自动生成SDK摘要。记忆、计划、工具结果及业务答复继承全部实际注入来源；来源变化、失权或删除后，整条派生内容屏蔽，不能靠复制为“个人记忆”清除依赖。

删除会话时列出派生记忆，默认一并删除，可逐条明确保留为独立私人记忆，保留项仍受原来源约束。专用删除清空正文和全部操作预览，不通过Frappe Deleted Document/Version留下副本。正式进展和预约不随私人会话删除。删除、关闭记忆及原生User/Member/Role撤权与结果发布共用短事务闸门；网络等待不持锁，在途请求返回后复验，旧回合不发布、不继续工具、不重新写回。已被人看见或已发送给供应商的内容无法追回。

迁移使用同版应用源码/镜像后执行：

```sh
bench --site lab.localhost migrate
bench build --app research_group
bench --site lab.localhost set-config rg_ai_mode mock
```

新增AI表、索引和标准Page；不重建原业务表。迁移前保留数据库、公开/私有文件和受保护站点配置为一组，保留已验收旧镜像独立标签。原生Bench路径继续适用。Docker开发仍编辑 `src/research_group/`，重建同版应用镜像后迁移并重启应用服务，保留 `runtime/`。

服务端可设置 `rg_ai_limits` 对象，初值为每用户每分钟5轮、每天50轮，输入12000字符，保守上下文上限24000个UTF-8字节（未知供应商分词器时的token上界），输出2000 tokens、4次模型请求、6次工具、90秒。单站点一次执行，每人至多一轮排队/执行，最多20个等待，队列60秒过期；取消或隐私失效后，在途请求仍占用执行名额直到worker退出。每轮预占 `4*(24000+2000)` tokens；已知usage包含供应商total_tokens，未知usage保留预占，失败不自动重放。模拟模式无供应商费用。每人AI正文20MiB、全站200MiB、有效长期记忆200条；正文、来源和待确认预览均按实际UTF-8字节计入，满额停止新增，仍可读取与删除。相同目标的有效删除预览复用，不延长原到期时间。

真实适配器使用固定版本的LangChain模型适配和LangGraph显式流程；精确依赖见 `src/research_group/pyproject.toml`。支持Responses与Chat Completions两协议；整个图和模型调用关闭外部追踪、服务端响应存储、自动重试和外部预览。流程依次验证身份、站内范围、授权上下文、模型工具白名单、读取或预览、最终来源；只辅导站内工作。保留既有12个工具并只新增固定站内指引/论文选段/建议预览工具，无SQL、Shell、URL、浏览器、通用CRUD、群发、授权或发布工具。图产生预览后结束，网页本人确认独立执行既有幂等事务，不在重放节点内确认写入。Frappe是历史、memory与运行唯一持久存储，不配LangGraph checkpointer/Store。

出站前按完整请求字节复查预算，包含工具定义和协议包装。两协议真实代码使用MockTransport离线验证；付费联调须另行授权，之后运维可配置 `rg_ai_mode=langchain`、`rg_ai_real_enabled=true`、`rg_ai_protocol`、`rg_ai_model`、`rg_ai_base_url` 和同值的 `rg_ai_allowed_base_urls`，并在受保护配置保存 `rg_ai_api_key`。旧`agents`配置值仅兼容映射到新引擎，不再加载旧Agents SDK。这些配置没有客户端入口，密钥不进入源码、浏览器、prompt、日志或运行状态。真实端点、模型质量、条款、费用和容量仍须单独验收。

长期worker在每次请求和最终发布前从服务端重新读取配置。端点、允许清单、真实启用状态、模型、协议或额度变化会使旧轮次失效；凭据不进入配置指纹或运行记录。刷新页面可从本人历史中的请求ID恢复生成状态与取消控件，入队回执丢失时先查状态，不自动重新发送。

AI内容备份建议单独30天轮转，长期业务备份排除上述AI内容表；已有不可覆盖的历史完整备份应在运维清单列为保留例外。删除清单保存在站点 `private/rg-ai-deletions.jsonl`，仅含site、本人、对象类型/ID和时间，须独立于内容备份保留最新副本。恢复站点保持维护模式，先应用最新删除清单，再开放：

```sh
# 独立恢复站点显式绑定源site，不能按用户名猜测
bench --site restore-ai.localhost set-config rg_ai_restore_source_site lab.localhost
bench --site restore-ai.localhost execute research_group.ai.storage.replay_deletions --kwargs '{"path":"/protected/latest-rg-ai-deletions.jsonl"}'
```

重放会清空对应正文并使旧运行和预览失效；恢复演练还须核验正式业务记录保留。清单不含正文，仍应按内部元数据保护。提供商留存遵循所选服务条款，网站删除不等于抹除其全部外部留存。

源码检查新增AI表隐私约束；`node scripts/check-ai-ui.cjs` 覆盖安全文字渲染、来源URL和乱序回执。`research_group.tests.test_ai` 使用真实Frappe/MariaDB，包括独立连接双确认、User/Member/Role撤权、排队与在途删除/关闭，以及固定SDK两协议的离线HTTP通路。历史通过数量与验证边界见 [验证说明](docs/validation.md)；尚未运行的验证不算通过。

## 验证命令

GitHub Actions（`.github/workflows/checks.yml`）配置为在推送到 `main` 和 Pull Request 时运行离线源码、前端与 CI 入口保护检查，再用 MariaDB 11.8 和 Redis 运行完整 Frappe 测试。Linux 本机可运行 `DB_ROOT_PASSWORD=… scripts/ci-frappe-tests.sh`；它只接受不存在的 `BENCH_DIR` 和 `ci.localhost` 或 `ci-<小写字母数字>.localhost` 站点，拒绝复用或覆盖已有 bench。无数据库的入口保护检查为 `python scripts/check-ci-guards.py`。配置存在不代表 GitHub Actions 已实际执行；补丁提供者记录与当前验收分别见 [验证说明](docs/validation.md)。

三角色、账号、通知、工作餐和论文的可复跑 HTTP 入口：

```powershell
python scripts/verify-role-http.py all --base http://localhost:8089 --credentials tasks/local-acceptance/work/credentials.json --container rg-role-candidate-backend-1 --task-dir tasks/local-acceptance --attempt review-01
```

先加 `--check-paths` 可只检查参数和输出路径。入口仅允许独立的 loopback 候选站点，运行前核实独立数据库、捕获模式邮件、原生邮件静音和离线 AI；不会开启真实 SMTP 或付费模型。每个 attempt 的回执/临时文件均落在显式任务内，不覆盖旧记录。凭据 JSON 保持私密；`legacy-core` 额外需要其中的 `Administrator` 键，其余套件只需四个虚构账号。具体范围和单套件命令见 [HTTP 验收入口](scripts/role-collaboration/README.md)。

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml exec backend bench --site lab.localhost set-config allow_tests 1 --parse
docker compose --env-file deploy/.env -f deploy/compose.yaml exec backend bench --site lab.localhost run-tests --app research_group
docker compose --env-file deploy/.env -f deploy/compose.yaml exec backend bench --site lab.localhost execute research_group.audit.material_integrity
```

测试使用真实 Frappe 权限、数据库和私有下载选择器。并发测试启动独立 Python 进程/数据库连接，覆盖重叠批准、同一预约批准/取消、同一报告重传、不同报告物理文件名分配及草稿更正/发布竞态。测试数据均使用独立虚构前缀；提交给子连接的夹具在完成后精确回收。`scripts/verify-http.py` 另通过实际 multipart/分片请求及精确临时文件路径检查验证原生上传边界；它用本机管理员创建独立虚构教师夹具，成功时仅回收本次无业务引用的账号，失败时在回执保留身份。请在演示/验收站点运行测试。

实际浏览器验收还应覆盖教师/学生/秘书三种账号的桌面与 390px 视口、私人文件直链的拒绝回执，并保存截图和操作结果。测试文件存在或静态编译通过不等于这些验收已完成。

问卷另有 `python scripts/verify-surveys-http.py`（实际登录、发布、修改、并发、服务器截止及CSV；截止观察次数随运行时序变化）及 `node scripts/check-survey-refresh.cjs`（15项实际JS回归，含三题型首填/修改序列化、保存失败和乱序刷新）。HTTP脚本仅用于本机虚构演示站点，会新增标注虚构的问卷和答卷；不会修改旧课题或组会。完整测试范围及历史运行结果见 [验证说明](docs/validation.md)。

## 备份与恢复

运行期间保留整个 `runtime/`。数据库在 `runtime/db`，记录与材料在 `runtime/sites`；`docker compose down` 不删除这些 bind 目录。不要使用清空目录或 `down -v` 代替备份。

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml exec backend bash /opt/research-group-scripts/backup.sh
```

备份位于 `runtime/sites/lab.localhost/private/backups/`。将同一时间戳的 database、files、private-files 及 site_config_backup 作为一组复制到 `runtime/backups/` 或受控异机存储。站点配置含数据库凭据和 encryption_key，不得进入 Git。备份前后运行 `research_group.audit.material_integrity`，比较 checked_versions、errors 与 ok。

在独立空站点演练恢复，保留原站点，使用同版镜像并先安装本应用。将备份组放入该站点可读的私有目录，然后使用原生恢复命令：

```sh
bench --site restore-review1.localhost restore /path/to/database.sql.gz --with-public-files /path/to/files.tgz --with-private-files /path/to/private-files.tgz --db-root-password "$DB_ROOT_PASSWORD" --force
bench --site restore-review1.localhost migrate
bench --site restore-review1.localhost execute research_group.audit.material_integrity
```

恢复站点沿用**新站点的数据库连接字段**，从备份配置恢复原 `encryption_key` 及实际使用的应用配置；不要直接用旧 `db_name`、`db_password` 覆盖新连接。重启后检查原记录、材料版本数、父记录关联、文件大小及 SHA256，最后用三类账号复测读取范围。实际验收回执须记录备份组、恢复站点和检查结果，不能仅以 restore 退出零宣称完整恢复。

本机验收脚本为 `scripts/verify-backup-restore.py`，容器侧辅助脚本为 `scripts/backup-restore-worker.py`。不带执行参数时只打印计划。**在本会话明确允许“重启/备份恢复”后**，才执行：

```sh
python scripts/verify-backup-restore.py --restore-site restore-review1.localhost --execute --allow-restart
```

`--restore-site` 默认仍为 `restore.localhost`，可显式选择新的 `restore-review1.localhost` 等名称；只允许 `restore(?:-[a-z0-9]+)*\.localhost`，且必须与源站点不同。计划输出显示实际选择的站点。宿主与容器侧均拒绝已有恢复站点，保留原站点、已有 KEEP 演练及所有 bind 目录。源站点缺省 `encryption_key` 时，先使用 Frappe 原生方法生成；已有键绝不替换，记录只保留是否生成的布尔值。随后以原生完整备份建立独立站点、独立数据库，逐项断言新数据库连接字段保持不变，再比较八类业务记录摘要、材料原字节摘要和父记录关联数。它用恢复站点的真实 HTTP 登录验证教师、学生、秘书兼学生及访客的读取范围，检查登录仅更新恢复库，随后重启本项目七个服务，复查两个站点和 Redis queue 的 AOF 持久性。真实备份（含敏感配置）保存在源站点私有备份目录；验收 JSON 使用该脚本声明的本地任务输出路径，不随公开源码提交。失败时保留现场，不自动删除或覆盖恢复站点。

## Linux 服务器配置

将仓库放在服务器已登记的独立项目目录，保留同版镜像，生成服务器自己的 `.env`。默认仍可保留回环监听，通过 SSH 端口转发访问。正式域名、TLS、反向代理、真实账户、外部备份和公网发布属于后续明确部署工作；本版交付服务器可运行配置，不自动执行公网部署。

## 标准 Bench 原生开发

可在本机 WSL 或 Linux 直接开发，不依赖 Docker。需要 Python **>=3.14,<3.15**、Node **>=24**、Yarn、Frappe Bench、兼容 MariaDB（此配置固定 11.8.3）、Redis 和 Frappe 官方系统依赖。现有 Python 3.12/Node 22 环境不满足固定标签要求。以下是原生路径，尚未代表本机已完成原生安装验收。

平台和版本前提参照 [Frappe 官方安装说明](https://docs.frappe.io/framework/user/en/installation)；本项目仍以固定 v16.55.2 源码和实际运行回执为准。

```sh
bench init --frappe-branch v16.55.2 --python /absolute/path/to/python3.14 /registered/workplace/research-group-jira/runtime/dev-bench
cd /registered/workplace/research-group-jira/runtime/dev-bench
ln -s /absolute/path/to/research-group-jira/src/research_group apps/research_group
bench pip install -e apps/research_group
printf '\nresearch_group\n' >> sites/apps.txt
bench build --app research_group
bench new-site lab.localhost
bench --site lab.localhost install-app research_group
bench --site lab.localhost migrate
bench --site lab.localhost set-config mute_emails 1 --parse
bench --site lab.localhost set-config max_file_size 53477376 --parse
bench --site lab.localhost set-config rg_max_file_size 52428800 --parse
bench use lab.localhost
bench start
```

应用位于本仓库的子目录，并非单独 Git 仓库，以上用符号链接和 editable install 注册到 Bench。MariaDB/Redis 凭据交给原生命令交互输入或本地受控配置，不写入源码。编辑本机 app 后通过原生 Bench 的热更新/构建流程检验；数据库结构变化需要 migrate，前端资源变化需要 bench build。容器路径修改源码后重新 build 应用镜像、migrate 并重启应用服务，持久数据不应删除。


## 公开仓库与本地配置

这个仓库提供可安装的 Frappe 应用、部署配方和测试，不包含运行数据库、上传材料、账号口令、API key、站点配置、备份或内部开发记录。初次启动时由 `scripts/create-local-config.py` 在本机生成新凭据；没有公开通用密码。

应用源码最初从已验收开发版本 `81ef88729dc00c24a882ffdf95993745ce7859f4` 导出，导出时保留的226个应用文件保持原字节，另省略7张许可待核实的照片；[源码清单](docs/source-manifest.json)只记录这次首次导出的快照，之后的修改以本仓库 Git 历史为准。公开仓库使用独立历史，公开版本说明与内部工作记录分开保存。新增的发布说明不代表重新完成了真实邮件、模型或公网部署验收。

HTTP 验收入口要求自行建立 `tasks/local-acceptance/TASK.md` 和被忽略的私密凭据文件，并选择独立测试站点；不会连接当前任何现有课题组站点。演示数据与测试账号全部为虚构。不要在真实业务站点运行写入型验收。

本项目应用沿用既有MIT许可证，见 [LICENSE](LICENSE)。第三方代码和测试夹具保留各自声明，见 [第三方材料](docs/third-party.md)。7张原餐品照片未随公开仓库分发；需要图片时请自行提供有权使用的文件，路径见 [第三方材料](docs/third-party.md)。
