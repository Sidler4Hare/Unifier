"""Verify deployed identities, original data, menus and native package state."""
from pathlib import Path
import hashlib
import io
import json
import os
import subprocess
import tomllib
import tarfile
import zipfile

from backup import archive_container, files, save

RUN = Path(__file__).resolve().parent
CTX = json.loads((RUN / 'context.json').read_text())


def main():
    os.umask(0o077)
    assert not (RUN / 'audit.json').exists()
    results = {}
    for app, item in CTX['apps'].items():
        root, backup = Path(item['root']), Path(item['backup'])
        current = json.loads(subprocess.check_output(['podman', 'inspect', item['container_id']], text=True))[0]
        original = json.loads((backup / 'container-inspect.json').read_text())
        assert current['Id'] == original['Id']
        assert current['Image'] == original['Image'] and current['Mounts'] == original['Mounts']
        assert current['Config'] == original['Config']
        assert current['State']['Status'] == 'exited'
        lease = json.loads((root / '.lab/leases' / (item['environment'] + '.json')).read_text())
        assert not lease.get('leases') and not lease.get('bridge')
        assert files(Path(item['documents'])) == item['documents_sha256']
        assert hashlib.sha256(Path(item['desktop']).read_bytes()).hexdigest() == item['desktop_sha256']
        before_config = tomllib.loads((backup / 'deployment/config/targets.toml').read_text())
        after_config = tomllib.loads((root / 'config/targets.toml').read_text())
        assert after_config.pop('compatibility') == {'mode': 'whitelist'}
        assert before_config == after_config
        assert (backup / 'deployment/config/images.lock.json').read_bytes() == (root / 'config/images.lock.json').read_bytes()
        env_path = '.lab/state/environments/' + item['environment'] + '.json'
        before = json.loads((backup / 'deployment' / env_path).read_text())
        after = json.loads((root / env_path).read_text())
        for record in [before, after]:
            record.pop('updated_at', None)
            record.pop('runtime_state', None)
            record.get('observed', {}).pop('kernel', None)
        assert before == after, 'environment contract changed'
        unchanged_state = {}
        for relative, digest in item['state_sha256'].items():
            if relative != 'environments/' + item['environment'] + '.json':
                assert hashlib.sha256((root / '.lab/state' / relative).read_bytes()).hexdigest() == digest, relative
                unchanged_state[relative] = digest
        package_after = backup / 'package-state-after-audit2.tar.gz'
        package_receipt = archive_container(item['container_id'], item['package_database'], package_after)
        original_packages = json.loads((backup / 'package-state.tar.manifest.json').read_text())
        final_packages = json.loads(package_after.with_suffix('.manifest.json').read_text())
        assert original_packages == final_packages
        home_after = backup / 'private-home-after-audit2.tar.gz'
        home_receipt = archive_container(item['container_id'], '/home/multilinux-1000', home_after)
        original_home = json.loads((backup / 'private-home.tar.manifest.json').read_text())
        final_home = json.loads(home_after.with_suffix('.manifest.json').read_text())
        deleted = [name for name, entry in original_home.items() if 'sha256' in entry and name not in final_home]
        changed = [name for name, entry in original_home.items() if 'sha256' in entry and name in final_home
                   and entry['sha256'] != final_home[name].get('sha256')]
        added = [name for name in final_home if name not in original_home]
        # Tencent Meeting itself compresses historical logs and retires old
        # downloadable resource caches during an ordinary launch. Verify log
        # contents through the ZIPs and keep the original caches in backup.
        compressed_logs, retired_caches, unexpected = [], [], []
        with tarfile.open(home_after, 'r:gz') as archive:
            for name in deleted:
                if app == 'wemeet' and '/Saas/Logs/' in name and name + '.zip' in final_home:
                    data = archive.extractfile(name + '.zip').read()
                    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
                        matches = [entry for entry in zipped.infolist() if not entry.is_dir()
                                   and hashlib.sha256(zipped.read(entry)).hexdigest() == original_home[name]['sha256']]
                        assert matches, 'compressed log does not retain original content'
                    compressed_logs.append(name)
                elif app == 'wemeet' and any(prefix in name for prefix in (
                        '/Saas/Global/Data/DynamicResource/', '/Saas/Global/Data/DynamicResourcePackage/')):
                    retired_caches.append(name)
                else:
                    unexpected.append(name)
        save(backup / 'private-home-delta-audit2.json', {
            'deleted_live_paths': deleted, 'changed': changed, 'added': added,
            'compressed_logs_verified': compressed_logs, 'retired_dynamic_caches_backed_up': retired_caches,
            'unexpected_missing': unexpected})
        assert not unexpected, 'an original non-cache private HOME file disappeared'
        assert hashlib.sha256((backup / 'private-home.tar.gz').read_bytes()).hexdigest() == item['private_home']['sha256']
        manifest = json.loads((root / 'deployment-current.json').read_text())
        assert all(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
                   for name, digest in manifest['source_sha256'].items())
        assert all((root / name).read_bytes() == (Path(CTX['root']) / name).read_bytes()
                   for name in manifest['compatibility_deployment']['source_sha256'])
        results[app] = {
            'container_id': item['container_id'], 'container_id_image_mounts_config_preserved': True,
            'environment_identity_and_contract_preserved': True, 'state_after': 'exited', 'leases_empty': True,
            'unchanged_registry_files': len(unchanged_state), 'desktop_unchanged': True,
            'documents_unchanged': True, 'image_lock_preserved': True, 'whitelist_enabled': True,
            'native_package_database_unchanged': True, 'package_entries': package_receipt['entries'],
            'private_home_unexpected_original_files_missing': len(unexpected),
            'historical_logs_preserved_in_zip': len(compressed_logs),
            'retired_dynamic_cache_files_retained_in_backup': len(retired_caches),
            'private_home_before_backup_verified': True,
            'private_home_normal_runtime_files_changed': len(changed),
            'private_home_entries_added': len(added), 'private_home_entries_after': home_receipt['entries'],
            'deployment_manifest_hashes_verified': True,
        }
    final = json.loads(subprocess.check_output(['podman', 'ps', '-a', '--format', 'json'], text=True))
    assert {row['Id'] for row in final} == {item['container_id'] for item in CTX['apps'].values()}
    result = {'status': 'PASSED_DEPLOYMENT_AND_DATA_PRESERVATION', 'apps': results,
              'only_two_daily_containers_remain': True}
    save(RUN / 'audit.json', result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
