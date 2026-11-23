"""Host administration of access policy and exact selected-file leases."""
import json
import os
from pathlib import Path

from unifier.file_access import AccessError
from unifier.file_access_store import ACTIVE, AccessStore, _read, reclaim_directory
from unifier.identity import binding_for
from unifier.operation_lock import management_lock
from unifier.store import EnvironmentStore


def add_parser(file_ops, callback):
    access = file_ops.add_parser('access', help='inspect and manage unified file access')
    commands = access.add_subparsers(dest='access_action', required=True)
    policy = commands.add_parser('policy', help='show policy or apply a revision-checked JSON policy')
    policy.add_argument('--set', dest='policy_file')
    listing = commands.add_parser('list', help='list durable file access leases and grants')
    listing.add_argument('--lease', help='inspect one lease within the listing budget')
    revoke = commands.add_parser('revoke', help='request one grant revocation; the bridge performs cleanup')
    revoke.add_argument('lease')
    revoke.add_argument('grant')
    recover = commands.add_parser('recover', help='reclaim the exact snapshot directory of a dead lease')
    recover.add_argument('lease')
    access.set_defaults(func=callback)


class AccessControl:
    def __init__(self, root):
        self.root = Path(root)
        self.store = AccessStore(root)
        self.environments = EnvironmentStore(root)

    def set_policy(self, value):
        with management_lock(self.root):
            return self.store.set_policy(value, environments=self.environments.all())

    def _bound_ledger(self, lease):
        ledger = self.store.ledger(lease)
        binding = ledger.header['environment_binding']
        if binding is None:
            raise AccessError('ACCESS_BINDING_MISSING', 'lease has no environment binding')
        record = self.environments.load(binding['environment_id'])
        if binding_for(record) != binding:
            raise AccessError('ACCESS_BINDING_CHANGED', 'environment instance changed; preserve its old lease')
        self.store.check_mounts(record.get('shared_mounts', []))
        settings = (record.get('desktop') or {}).get('portal_proxy') or {}
        directory = self.root / '.lab/desktop' / record['id']
        if settings.get('directory') != str(directory) or directory.resolve() != directory:
            raise AccessError('ACCESS_STATE_IDENTITY', 'lease parent is not the registered bridge directory')
        selected = Path(ledger.header['selection_directory'])
        if selected.parent != directory or not selected.name.startswith('selected-'):
            raise AccessError('ACCESS_STATE_IDENTITY', 'lease selection directory is outside its bridge')
        return ledger, selected

    def revoke(self, lease, grant_id):
        with management_lock(self.root):
            ledger, _ = self._bound_ledger(lease)
            grant = ledger.read()['grants'].get(grant_id)
            if grant is None:
                raise AccessError('ACCESS_GRANT_MISSING', 'grant does not exist in this lease')
            if grant['state'] not in ('released', 'failed'):
                ledger.transition(grant_id, 'revoking')
            return {'lease_id': lease, 'grant_id': grant_id,
                    'state': ledger.read()['grants'][grant_id]['state'],
                    'scope': 'environment',
                    'limitation': 'Existing static mounts, open file descriptors and already read data remain accessible.'}

    def recover(self, lease):
        with management_lock(self.root):
            ledger, selected = self._bound_ledger(lease)
            with ledger.locked():
                value = ledger.read()
                owner = value.get('owner')
                if (not isinstance(owner, dict) or type(owner.get('pid')) is not int
                        or owner['pid'] <= 0 or type(owner.get('starttime')) is not int
                        or owner['starttime'] <= 0
                        or not isinstance(owner.get('boot_id'), str) or not owner['boot_id']):
                    raise AccessError('ACCESS_OWNER_UNKNOWN', 'lease owner identity is incomplete')
                boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                if owner['boot_id'] == boot:
                    try:
                        fields = Path(f"/proc/{owner['pid']}/stat").read_text().rsplit(')', 1)[1].split()
                    except FileNotFoundError:
                        fields = None
                    if fields and int(fields[19]) == owner['starttime']:
                        raise AccessError('ACCESS_LEASE_ACTIVE', 'lease owner is still alive; revoke the grant or finish its applications')
                # No runtime calls, socket removal, process signals, or name-only cleanup.
                active = any(grant['state'] in ACTIVE for grant in value['grants'].values())
                try:
                    parent_fd = os.open(selected.parent, os.O_DIRECTORY | os.O_NOFOLLOW)
                except FileNotFoundError:
                    if active:
                        raise AccessError('ACCESS_OBJECT_MISSING', 'active snapshot parent disappeared; preserve its accounting')
                    parent_fd = None
                if parent_fd is not None:
                    try:
                        reclaim_directory(parent_fd, selected.parent, selected.name,
                                          value['selection_identity'], missing_ok=not active)
                    finally:
                        os.close(parent_fd)
            ledger.release_all()
            return {'lease_id': lease, 'state': 'released', 'selection_directory_removed': True}


def run(args, root):
    control = AccessControl(root)
    if args.access_action == 'policy':
        result = (control.set_policy(_read(Path(args.policy_file), limit=65536))
                  if args.policy_file else control.store.policy().as_dict())
    elif args.access_action == 'list':
        result = (control.store.ledger(args.lease).read() if args.lease else control.store.ledgers())
    elif args.access_action == 'revoke':
        result = control.revoke(args.lease, args.grant)
    else:
        result = control.recover(args.lease)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
