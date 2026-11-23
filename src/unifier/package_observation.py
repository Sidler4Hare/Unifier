"""Non-target observations are audit evidence, not package commit authority."""


def unavailable_host(error):
    return {'packages': {'available': False, 'tool': None, 'sha256': None,
                         'count': None, 'error': str(error)[-500:]},
            'entries': {'available': False, 'directory': '', 'names': []}}


def summarize(txn):
    changed, unavailable = [], []
    peers = txn.get('other_environments') or {}
    if '_error' in peers:
        unavailable.append(str(peers['_error']))
    else:
        for env_id, item in peers.items():
            if item.get('error'):
                unavailable.append(f'{env_id}: {item["error"]}')
            elif not item.get('unchanged'):
                changed.append(f'{env_id}: peer inventory or identity changed')
    host = txn.get('host_packages') or {}
    before, after = host.get('before') or {}, host.get('after') or {}
    if not before.get('available') or not after.get('available'):
        unavailable.append('host package observer unavailable')
    elif before.get('sha256') != after.get('sha256'):
        changed.append('host package set changed')
    entries = txn.get('host_entries') or {}
    if entries.get('available') is False:
        unavailable.append('host desktop observer unavailable')
    elif entries.get('added') or entries.get('removed'):
        changed.append('host desktop entries changed')
    return {'schema_version': 1, 'scope': 'non-target-audit',
            'status': 'changed' if changed else 'unavailable' if unavailable else 'unchanged',
            'attribution': 'unattributed', 'problems': changed + unavailable}


def warning(txn):
    audit = txn.get('isolation_observation') or {}
    if audit.get('status') not in ('changed', 'unavailable'):
        return None
    return (f"unifier: non-target audit {audit['status']}; it does not establish "
            f"transaction causation or isolation. See 'unifier pkg show {txn['id']}'.")
