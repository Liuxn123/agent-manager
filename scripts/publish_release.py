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
            'body': 'Hermes 改为顶部实例 / Profile 选择，常用操作直接展示；服务器两个 Profile 明确共用网关和现有整套备份。项目与 Obsidian 合并，保留 myself 备份入口，日志按日期查看并分项填写。\n\nAgent / myself 新建加密备份共用本次打开的密码，关闭后忘记。修复 Codex 运行锁、受保护沙箱与静止 SQLite 只读备份；实际本机 5869 文件已完成隔离加密、校验、恢复。\n\n换电脑显示逐项任务清单，新增工作总结、手动限量的 Codex Token 用量统计，更新离线分模块说明。原应用安装、登录和续聊仍需在原客户端验证。\n\nWindows、Linux、macOS Apple Silicon 和 Intel 完成自动测试及便携包解压启动与移动验证。升级保留 data、backups、restored。'})
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
