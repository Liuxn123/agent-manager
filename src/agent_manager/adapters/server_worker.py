"""Fixed remote program, sent via SSH. Kept as a string for frozen packaging."""

SERVER_PROGRAM = r'''
import base64
import hashlib
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
from pathlib import Path

def execute(args, timeout=600, env=None):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout, env=env)
    if result.returncode:
        raise RuntimeError('command_failed')
    if len(result.stdout) > 8000000:
        raise RuntimeError('output_too_large')
    return result.stdout

def token(repo):
    snapshot = repo / 'snapshot'
    if not snapshot.is_dir() or snapshot.is_symlink():
        raise RuntimeError('missing_snapshot')
    digest = hashlib.sha256()
    for path in sorted(snapshot.rglob('*')):
        if path.is_symlink():
            raise RuntimeError('unsafe_snapshot')
        if path.is_file():
            digest.update(path.relative_to(snapshot).as_posix().encode('utf-8'))
            with path.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1048576), b''):
                    digest.update(chunk)
    return digest.hexdigest()

def main(config):
    action = config['action']
    if action not in {'observe', 'backup', 'verify', 'plan_restore', 'restore', 'start', 'stop', 'restart'}:
        raise RuntimeError('unsupported_action')
    repo = Path(config['backup_repo'])
    home = Path(config['home'])
    knowledge = Path(config['knowledge_repo'])
    service = config.get('service', 'hermes-gateway.service')
    if not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', service) or service.startswith('-'):
        raise RuntimeError('invalid_service')
    scope = config.get('service_scope', 'system')
    if scope not in {'user', 'system'}:
        raise RuntimeError('invalid_scope')
    service_command = ['systemctl'] + (['--user'] if scope == 'user' else [])
    if action == 'observe':
        result = subprocess.run(service_command + ['is-active', service], capture_output=True, text=True, timeout=15)
        state = result.stdout.strip()
        usage = shutil.disk_usage(home if home.exists() else '/')
        latest = None
        metadata = repo / 'snapshot/backup.json'
        if metadata.is_file() and metadata.stat().st_size < 100000:
            latest = json.loads(metadata.read_text()).get('created_at')
        memory = {}
        meminfo = Path('/proc/meminfo')
        if meminfo.is_file():
            for line in meminfo.read_text().splitlines():
                key, _, value = line.partition(':')
                if key in {'MemTotal', 'MemAvailable'}:
                    memory[key] = int(value.strip().split()[0])
        key_candidates = [home / '.hermes-backup-passphrase', repo / '.hermes-backup-passphrase', Path('/home/hermes/.hermes-backup-passphrase')]
        key_file = next((str(path) for path in key_candidates if path.is_file()), '')
        backup_tool = repo / 'tools/hermes_server_backup.py'
        restore_tool = repo / 'tools/hermes_restore_server.py'
        return {'connected': True, 'service_state': state if state in {'active','inactive','failed','activating','deactivating','unknown'} else 'unknown',
                'service_scope': scope, 'load_average': list(os.getloadavg()), 'cpu_count': os.cpu_count(),
                'disk_used_percent': round(usage.used * 100 / usage.total, 1), 'backup_created_at': latest,
                'memory_used_percent': round((1 - memory['MemAvailable'] / memory['MemTotal']) * 100, 1) if memory.get('MemTotal') and 'MemAvailable' in memory else None,
                'profiles': sorted(p.name for p in (home / 'profiles').iterdir() if p.is_dir())[:50] if (home / 'profiles').is_dir() else [],
                'home_present': home.is_dir(), 'backup_repo_present': repo.is_dir(),
                'backup_tool_present': backup_tool.is_file(), 'restore_tool_present': restore_tool.is_file(),
                'backup_key_file': key_file, 'backup_key_available': bool(key_file),
                'backup_ready': home.is_dir() and backup_tool.is_file() and restore_tool.is_file() and bool(key_file)}
    if action in {'start','stop','restart'}:
        execute(service_command + [action, service], timeout=60)
        result = subprocess.run(service_command + ['is-active', service], capture_output=True, text=True, timeout=15)
        return {'operation': action, 'service_state': result.stdout.strip() if result.stdout.strip() in {'active','inactive','failed','activating','deactivating'} else 'unknown'}
    if not repo.is_dir() or not repo.is_absolute() or not home.is_absolute() or not knowledge.is_absolute():
        raise RuntimeError('invalid_directory')
    python = config.get('python', 'python3')
    run_user = config.get('run_user', 'hermes')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', run_user):
        raise RuntimeError('invalid_user')
    account = pwd.getpwnam(run_user)
    env = dict(os.environ, HOME=account.pw_dir, HERMES_HOME=str(home), HERMES_KNOWLEDGE_REPO=str(knowledge), PYTHONUTF8='1')
    def run(args):
        if os.geteuid() == 0 and account.pw_uid != 0:
            args = ['runuser', '-u', run_user, '--', 'env', 'HOME=' + account.pw_dir,
                    'HERMES_HOME=' + str(home), 'HERMES_KNOWLEDGE_REPO=' + str(knowledge), *args]
        elif os.geteuid() != account.pw_uid:
            raise RuntimeError('wrong_account')
        return json.loads(execute(args, timeout=1800, env=env))
    allowed = {'applied','changes','rescue_dir','created_at','counts','pushed','status','valid','mismatches','recoverability','homes','home','target','runtime','node'}
    def public(report):
        return {key: value for key, value in report.items() if key in allowed}
    backup = [python, str(repo / 'tools/hermes_server_backup.py'), 'backup', '--repo', str(repo), '--home', str(home), '--knowledge-repo', str(knowledge)]
    recovery = [python, str(repo / 'tools/hermes_restore_server.py')]
    if action == 'backup':
        if config.get('passphrase_file'):
            backup += ['--passphrase-file', config['passphrase_file']]
        return public(run(backup))
    verification = run(recovery + ['verify', '--repo', str(repo)])
    if verification.get('valid') is not True:
        raise RuntimeError('verification_failed')
    if action == 'verify':
        return public(verification)
    target = Path(config['target'])
    if not target.is_absolute() or target == Path('/') or target.is_symlink() or any(p.is_symlink() for p in target.parents):
        raise RuntimeError('unsafe_target')
    if target == home or target == knowledge or home.is_relative_to(target) or knowledge.is_relative_to(target) or target.is_relative_to(home) or target.is_relative_to(knowledge) or repo.is_relative_to(target) or target.is_relative_to(repo):
        raise RuntimeError('unsafe_target')
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise RuntimeError('target_not_empty')
    current_token = token(repo)
    restore = recovery + ['restore','--repo',str(repo),'--home',str(target),'--knowledge-repo',str(knowledge)]
    if config.get('passphrase_file'):
        restore += ['--passphrase-file',config['passphrase_file']]
    if action == 'plan_restore':
        report = public(run(restore))
        return {'plan_token': current_token, 'target': str(target), 'preview': report, 'service_started': False}
    if current_token != config.get('plan_token'):
        raise RuntimeError('snapshot_changed')
    if not config.get('passphrase_file'):
        raise RuntimeError('passphrase_file_required')
    report = public(run(restore + ['--apply']))
    return {**report, 'service_started': False, 'note': 'Data restored into an empty directory. Runtime installation, service installation and gateway activation remain separate steps.'}

try:
    config = json.loads(base64.b64decode(sys.argv[1]))
    print(json.dumps({'ok': True, 'result': main(config)}, ensure_ascii=False))
except Exception as exc:
    code = str(exc) if str(exc) in {'command_failed','output_too_large','missing_snapshot','unsafe_snapshot','invalid_service','invalid_scope','unsupported_action','invalid_directory','invalid_user','wrong_account','verification_failed','unsafe_target','target_not_empty','snapshot_changed','passphrase_file_required'} else type(exc).__name__
    print(json.dumps({'ok': False, 'error': code}))
'''
