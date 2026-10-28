"""Create three isolated stopped native fixtures using already accepted B code.

Only fixture setup executes guest shell commands. The production view is tested
later from the host without guest shell execution. Extra mounts exercise real
Podman named-volume and nested-bind coverage, independent of v2 shared projects.
"""
from dataclasses import replace
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys
import tomllib
import uuid

HERE = Path(__file__).resolve().parent
REPO = next(p for p in HERE.parents if (p / 'src/multilinux').is_dir())
PROJECT = HERE / 'live-project'
FROZEN = REPO / 'evidence/unifier-v2-stage-b-2026-10-24/live-freecad/project'
sys.path[:0] = [str(FROZEN / 'src'), str(FROZEN)]
from multilinux.runtime import PodmanRuntime, creation_argv
from multilinux.service import EnvironmentService


def save(name, value):
    with (HERE / name).open('x') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')


class FixtureRuntime(PodmanRuntime):
    def create(self, spec):
        # Explicitly disable labels only in this fixture; do not relabel any
        # host project. All additional bind sources are this fixture's files.
        args = creation_argv(replace(spec, selinux_label='disable'))
        args[-2:-2] = ['--volume', self.volume + ':/var/lib/unifier-volume',
                      ]
        result = subprocess.run(args, capture_output=True, text=True, check=True)
        return result.stdout.strip()


class FixtureService(EnvironmentService):
    def _shares(self, shares):
        assert not shares
        # Freeze the nested bind in CreateSpec before the initial env record
        # is written. This exercises an actual declared persistent mount.
        return [(str(self.runtime.bind), '/var/lib/unifier-volume/nested')]


def main():
    if PROJECT.exists():
        raise RuntimeError('refusing to replace a fixture project')
    PROJECT.mkdir(mode=0o700)
    (PROJECT / 'config').mkdir()
    for name in ('targets.toml', 'images.lock.json'):
        shutil.copy2(REPO / 'config' / name, PROJECT / 'config' / name)
    config = tomllib.loads((PROJECT / 'config/targets.toml').read_text())
    lock = json.loads((PROJECT / 'config/images.lock.json').read_text())
    nonce = uuid.uuid4().hex[:10]
    report = {'project': str(PROJECT), 'targets': {}, 'fixture_only_guest_exec': True}
    save('containers-before-fixture.json', json.loads(subprocess.check_output(
         ['podman', 'ps', '-a', '--format', 'json'], text=True)))
    try:
        for target in ('debian13', 'fedora44', 'arch'):
            bind = HERE / ('bind-' + target + '-' + nonce)
            bind.mkdir(mode=0o700)
            (bind / 'payload.txt').write_text('nested-bind-' + target + '\n')
            volume = 'unifier-c-' + target + '-' + nonce
            subprocess.run(['podman', 'volume', 'create', '--label',
                            'unifier.probe=' + nonce, volume], check=True, capture_output=True)
            runtime = FixtureRuntime()
            runtime.volume, runtime.bind = volume, bind
            service = FixtureService(PROJECT, runtime, config, lock)
            env = 'view-' + target
            record = service.create(target, env, [],
                        user_namespace='keep-id' if target == 'arch' else 'rootless-default')
            item = {'environment': record, 'volume': volume, 'bind': str(bind),
                    'image': lock['targets'][target]['locked_reference']}
            report['targets'][target] = item
            # Persist immediately so an interrupted fixture remains recoverable.
            (HERE / 'fixture-progress.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            service.start(env)
            cid = record['runtime']['container_id']
            script = r'''set -eu
mkdir -p /var/lib/unifier-view/a/b /var/lib/unifier-volume/nested
printf '%s\n' "$1" > /var/lib/unifier-view/payload.txt
printf 'volume-%s\n' "$1" > /var/lib/unifier-volume/payload.txt
ln -s /etc/os-release /var/lib/unifier-view/os-absolute
ln -s ../../../../../../../etc/os-release /var/lib/unifier-view/a/b/os-upward
ln -s payload.txt /var/lib/unifier-view/relative
ln -s /var/lib/unifier-volume/payload.txt /var/lib/unifier-view/volume-absolute
ln -s "$2" /var/lib/unifier-view/host-escape
for n in 0 1 1000 1001; do
  printf 'uid=%s\n' "$n" > /var/lib/unifier-view/uid-$n.txt
  chown "$n:$n" /var/lib/unifier-view/uid-$n.txt
done
chmod 600 /var/lib/unifier-view/uid-0.txt
'''
            sentry = HERE / ('host-only-' + target + '.txt')
            sentry.write_text('HOST-SENTRY-MUST-NOT-BE-READ\n')
            subprocess.run(['podman', 'exec', '--user', '0', cid, '/bin/sh', '-c', script,
                            'fixture', target, str(sentry)], check=True, capture_output=True)
            item['host_only_sentry'] = str(sentry)
            item['inspect'] = json.loads(subprocess.check_output(['podman', 'inspect', cid], text=True))[0]
            service.stop(env)
            assert service.inspect(env)['runtime_state'] == 'stopped'
            item['stopped'] = True
        report['prepared'] = True
        save('fixtures.json', report)
        print(json.dumps({'prepared': True, 'targets': list(report['targets']), 'all_stopped': True}))
    finally:
        (HERE / 'fixture-progress.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
