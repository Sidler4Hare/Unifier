"""One registry for stopped database dispatch and mount-shadow protection."""
from unifier.package_inventory import InventoryUnsupported, read_dpkg_database
from unifier.pacman_inventory import read_pacman_database
from unifier.rpm_inventory import DATABASE_PATHS, read_rpm_database, rpm_decoder
from unifier.packages import driver_for

BACKENDS = {
    'apt': ('dpkg', ('/var/lib/dpkg',), read_dpkg_database),
    'pacman': ('pacman', ('/var/lib/pacman',), read_pacman_database),
    'dnf': ('rpm-sqlite', DATABASE_PATHS, read_rpm_database),
}


def backend_for(record, *, require_available=False):
    name = driver_for(record).name
    if name not in BACKENDS:
        raise InventoryUnsupported('read-only database adapter is not implemented for this backend')
    if require_available and name == 'dnf':
        rpm_decoder()
    return BACKENDS[name]
