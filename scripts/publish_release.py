from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'scripts'))
sys.path.insert(0, str(root / 'src'))
from github_publish import api, credential
from agent_manager import __version__

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None

def artifact_bytes(token, route):
    request = urllib.request.Request('https://api.github.com' + route,
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'User-Agent': 'agent-manager-release'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code != 302:
            raise RuntimeError('Artifact HTTP ' + str(error.code)) from None
        # Never forward a GitHub credential to the signed storage redirect.
        with urllib.request.urlopen(error.headers['Location'], timeout=60) as response:
            return response.read()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--run-id', type=int, required=True)
    options = parser.parse_args()
    token = credential()
    route = '/repos/Liuxn123/agent-manager'
    repo = api(token, route)
    assert repo['private'] is True
    run = api(token, route + '/actions/runs/' + str(options.run_id))
    head = run['head_sha']
    assert run['head_repository']['full_name'] == 'Liuxn123/agent-manager'
    assert run['name'] == 'Desktop CI and packages' and run['head_branch'] == 'main'
    jobs = api(token, route + '/actions/runs/' + str(run['id']) + '/jobs')['jobs']
    print(json.dumps({'run': run['id'], 'commit': head, 'status': run['status'],
                      'conclusion': run['conclusion'], 'jobs': [{'name': job['name'], 'status': job['status'], 'conclusion': job['conclusion'],
                      'current_step': next((step['name'] for step in job['steps'] if step['status'] == 'in_progress'), None)} for job in jobs]}))
    if not options.publish:
        return
    assert run['conclusion'] == 'success'
    assert len(jobs) == 4 and all(job['conclusion'] == 'success' for job in jobs)
    artifacts = api(token, route + '/actions/runs/' + str(run['id']) + '/artifacts')['artifacts']
    assert {artifact['name'] for artifact in artifacts} == {'AgentManager-' + name for name in ['windows-latest', 'ubuntu-22.04', 'macos-15', 'macos-15-intel']}
    directory = root / 'dist/releases'
    directory.mkdir(parents=True, exist_ok=True)
    packages = []
    def download(artifact):
        assert not artifact['expired']
        payload = artifact_bytes(token, route + '/actions/artifacts/' + str(artifact['id']) + '/zip')
        digest = artifact.get('digest')
        if digest:
            assert digest == 'sha256:' + hashlib.sha256(payload).hexdigest()
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = [name for name in archive.namelist() if name.endswith(('.zip', '.tar.gz'))]
            assert len(names) == 1
            package = directory / Path(names[0]).name
            package.write_bytes(archive.read(names[0]))
        print('Downloaded verified artifact:', artifact['name'])
        return package
    with ThreadPoolExecutor(max_workers=4) as pool:
        packages = list(pool.map(download, artifacts))
    checksums = directory / 'SHA256SUMS.txt'
    checksums.write_text(''.join(hashlib.sha256(package.read_bytes()).hexdigest() + '  ' + package.name + '\n' for package in packages), encoding='utf-8')
    packages.append(checksums)
    try:
        release = api(token, route + '/releases/tags/v' + __version__)
        assert release['target_commitish'] == head, 'Existing release points to another commit'
    except RuntimeError as error:
        if str(error) != 'GitHub HTTP 404':
            raise
        release = api(token, route + '/releases', {'tag_name': 'v' + __version__, 'target_commitish': head, 'name': 'Agent 管家 v' + __version__,
            'draft': False, 'prerelease': True,
            'body': '设置默认只保留备份位置、打开与更换按钮；工作台移除全局记录搜索和七天活动图，改为需要处理的事项与最近备份。\n\n新增独立“技能与工具”：统一查看 Skills、MCP 与提示词，导入及复制本地技能，新建编辑通用提示词；MCP 清单仅查看，不自动改写原配置或启用工具。通用库随便携目录携带，可用现有加密流程备份。\n\n保留管理页长文字完整换行、Hermes 会话与技能浏览、服务器网关日志及按资源操作记录。备份页加入恢复范围说明；图形界面数据恢复测试核对中文文件、聊天、附件、SQLite 与自动登记。原软件安装、登录和会话接续仍需在原应用确认。\n\nWindows、Linux、macOS Apple Silicon 与 Intel 均完成测试、打包、解压启动和便携目录迁移验证。更新时保留原 data、backups 与恢复资料。'})
    base = release['upload_url'].split('{')[0]
    assert urllib.parse.urlparse(base).hostname == 'uploads.github.com'
    existing = {asset['name']: asset for asset in release['assets']}
    for package in packages:
        payload = package.read_bytes()
        if package.name in existing:
            assert existing[package.name]['size'] == len(payload)
            continue
        request = urllib.request.Request(base + '?name=' + urllib.parse.quote(package.name), data=payload,
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/zip' if package.suffix == '.zip' else ('application/gzip' if package.suffix == '.gz' else 'text/plain'),
                     'Accept': 'application/vnd.github+json', 'User-Agent': 'agent-manager-release'}, method='POST')
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
            asset = json.load(response)
        assert asset['state'] == 'uploaded' and asset['size'] == len(payload)
        if asset.get('digest'):
            assert asset['digest'] == 'sha256:' + hashlib.sha256(payload).hexdigest()
        print('Uploaded:', package.name)
    print('Private release:', release['html_url'])

if __name__ == '__main__':
    main()
