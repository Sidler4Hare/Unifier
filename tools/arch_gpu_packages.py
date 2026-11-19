"""Input contract for the Arch GPU fixtures' offline package transactions."""

from pathlib import Path
import re


ARCHIVE = 'https://archive.archlinux.org/repos/2026/10/13'
MIB = 1024**2
VARIANTS = {
    'egl': ['mesa', 'libglvnd', 'python'],
    'graphics': ['mesa', 'libglvnd', 'python', 'vulkan-intel', 'vulkan-icd-loader'],
    'all': ['mesa', 'libglvnd', 'python', 'vulkan-intel', 'vulkan-icd-loader',
            'intel-media-driver', 'ffmpeg'],
    'amd': ['mesa', 'libglvnd', 'python', 'libva'],
}


def validate_plan(plan, cid):
    if (plan.get('cid') != cid or plan.get('archive') != ARCHIVE
            or plan.get('mode') not in VARIANTS or plan.get('requests') != VARIANTS[plan['mode']]):
        raise RuntimeError('Arch package plan differs from the approved fixture or requests')
    repositories = plan['repositories']
    packages = plan['packages']
    if set(repositories) != {'core', 'extra'} or not 1 <= len(packages) <= 256:
        raise RuntimeError('unexpected Arch repository or package count')
    transferred = 0
    installed = 0
    for repo, row in repositories.items():
        if (row['url'] != f'{ARCHIVE}/{repo}/os/x86_64/{repo}.db'
                or type(row['download_bytes']) is not int or not 0 < row['download_bytes'] <= 16*MIB
                or not re.fullmatch('[a-f0-9]{64}', row['sha256'])):
            raise RuntimeError('unexpected Arch repository input')
        transferred += row['download_bytes']
    for name, row in packages.items():
        if (row['name'] != name or row['repo'] not in repositories
                or not row['filename'].startswith(name+'-')
                or not re.fullmatch(r'[A-Za-z0-9_.+:-]+\.pkg\.tar\.(?:zst|xz)', row['filename'])
                or not re.fullmatch('[a-f0-9]{64}', row['sha256'])
                or not row['pgp_signature']
                or type(row['download_bytes']) is not int or row['download_bytes'] <= 0
                or type(row['installed_bytes']) is not int or row['installed_bytes'] < 0):
            raise RuntimeError('unexpected Arch package input')
        transferred += row['download_bytes']
        installed += row['installed_bytes']
    if (transferred != plan['transfer_bytes'] or installed != plan['installed_bytes']
            or transferred > 200*MIB or 2*transferred+installed+16*MIB > 1024*MIB):
        raise RuntimeError('Arch package inputs exceed the approved budgets')
    return plan


def guest_repository(unit):
    return Path('/tmp') / (unit+'-repo')
