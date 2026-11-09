param([string]$Tag = 'research-group-frappe-base:v16.55.2')
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$contextPath = Join-Path $projectRoot 'runtime/base-context'
$revision = '74421fd01047290635742d3fb85f7fd7eebbde86'
if (!(Test-Path -LiteralPath $contextPath)) {
    New-Item -ItemType Directory -Path $contextPath | Out-Null
    git -C $contextPath init --quiet
    git -C $contextPath remote add origin https://github.com/frappe/frappe_docker.git
    git -C $contextPath fetch --depth 1 origin $revision
    if ($LASTEXITCODE -ne 0) { throw '获取官方构建资源失败' }
    git -C $contextPath checkout --detach --quiet FETCH_HEAD
    if ($LASTEXITCODE -ne 0) { throw '固定官方构建版本失败' }
}
$actualRevision = git -C $contextPath rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $actualRevision -ne $revision) {
    throw '构建上下文不是要求的版本；请保留现有目录并选择新的独立上下文。'
}
$contextChanges = git -C $contextPath status --porcelain=v1 --untracked-files=all --ignored=matching --ignore-submodules=none
if ($LASTEXITCODE -ne 0) { throw '无法核验构建上下文工作树；停止构建。' }
if ($contextChanges) {
    throw '构建上下文工作树有修改或额外文件；保留现有目录并选择新的独立上下文。'
}
docker build --progress plain --file (Join-Path $PSScriptRoot 'Containerfile') `
    --build-arg FRAPPE_BRANCH=v16.55.2 --build-arg PYTHON_VERSION=3.14 `
    --build-arg NODE_VERSION=24 --build-arg INSTALL_CHROMIUM=false --tag $Tag $contextPath
if ($LASTEXITCODE -ne 0) { throw '基础镜像构建失败；保留日志后诊断。' }
