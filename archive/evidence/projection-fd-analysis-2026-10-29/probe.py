"""Isolated object-layer FD probe; no mounts or daily runtime changes."""
import errno, json, os, resource, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from test_projection_support import fixture, open_fd_count
from multilinux.projection import ViewError

def snap(objects):
    return {'fds': open_fd_count(), **objects.stats(),
            'lookup_refs': sum(n.nlookup for n in objects.nodes.values()),
            'internal_refs': sum(n.internal for n in objects.nodes.values())}

out = {'scope':'object layer with real process FD limit; no kernel FUSE mount',
       'original_limit':resource.getrlimit(resource.RLIMIT_NOFILE), 'cases':[]}
original = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (256, original[1]))
try:
    for count in (100, 300):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for i in range(count): (root / f'f{i:04d}').write_bytes(b'old')
            with fixture(root) as (_, backend, server):
                objects = backend.objects
                before = snap(objects)
                try:
                    fh = objects.opendir(1)
                except (ViewError, OSError) as exc:
                    out['cases'].append({'files':count, 'before':before,
                                         'error':exc.errno, 'after_failure':snap(objects)})
                else:
                    during = snap(objects)
                    objects.release(fh)
                    out['cases'].append({'files':count, 'before':before,
                                         'during':during,'after_release':snap(objects)})
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for i in range(300): (root / f'f{i:04d}').write_bytes(b'old')
        with fixture(root) as (_, backend, server):
            objects = backend.objects
            before = snap(objects)
            first = objects.lookup(1, 'f0000')
            fh = objects.open_file(first.nodeid, os.O_RDONLY)
            ids = [first.nodeid]
            error = None
            for i in range(1,300):
                try: ids.append(objects.lookup(1, f'f{i:04d}').nodeid)
                except (ViewError,OSError) as exc:
                    error = exc.errno
                    break
            held_read = objects.read_file(fh,0,3).decode()
            pressure = snap(objects)
            objects.release(fh)
            after_close = snap(objects)
            objects.batch_forget([(nodeid,1) for nodeid in ids])
            after_forget = snap(objects)
            recovered = objects.lookup(1,'f0299')
            objects.forget(recovered.nodeid,1)
            out['cases'].append({'kind':'lookup_pressure','lookups':len(ids),
                'error':error,'held_read':held_read,'before':before,
                'pressure':pressure,'after_close':after_close,
                'after_forget':after_forget,'new_lookup_recovered':True})
finally:
    resource.setrlimit(resource.RLIMIT_NOFILE,original)
print(json.dumps(out,indent=2))
