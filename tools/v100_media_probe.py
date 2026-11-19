"""Exercise H.264 NVDEC and NVENC in a single-device, already guarded V100 fixture."""
import base64
import hashlib
import math
import os
import stat

from tools import reuse_test_guard as guard


FILES = ('ffmpeg', 'libnvcuvid.so.1', 'libnvidia-encode.so.1', 'input.h264', 'reference.nv12')
FRAME_BYTES = 128 * 96 * 3 // 2
FRAMES = 8


def verify_file(path, digest):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 32 * 1024**2
                or info.st_uid != os.getuid() or info.st_mode & 0o022
                or hashlib.file_digest(stream, 'sha256').hexdigest() != digest):
            raise RuntimeError(f'unverified NVIDIA media probe input: {path.name}')


def psnr(payload, reference):
    values = []
    for offset in range(0, len(reference), FRAME_BYTES):
        end = offset + FRAME_BYTES
        error = sum((a-b)**2 for a, b in zip(payload[offset:end], reference[offset:end], strict=True))
        values.append(10 * math.log10(255**2 * FRAME_BYTES / error) if error else None)
    return values


def run(attempt):
    source = attempt.cuda_payload('video')
    reference = (source/'reference.nv12').read_bytes()
    if len(reference) != FRAMES * FRAME_BYTES:
        raise RuntimeError('unexpected NVIDIA media reference length')
    fixture, config = attempt.fixture, attempt.active_config()
    service = attempt.service()
    runtime = service.runtime
    cid = config['cid']
    scratch = '/var/tmp/'+attempt.spec['unit']+'-video'
    paths = {name: scratch+'/'+name for name in FILES}
    manifest = attempt.spec['video_sha256']
    report = {'cid': cid, 'gpu': fixture.cuda.identity(), 'status': 'INVALID',
              'probe_sha256': manifest, 'cases': {}}
    started = cleanup = False

    def guest(argv, env=None):
        return runtime.exec(cid, argv, project=str(fixture.root), env_id=fixture.env_id,
                            workdir=None, env=env or {}, capture=True, timeout=10)

    def ffmpeg(label, args, *, library=None):
        env = {'LD_LIBRARY_PATH': '/run/unifier-cuda:'+scratch, 'LC_ALL': 'C'}
        if library:
            attempt.cuda_telemetry(active=True)
            env['LD_DEBUG'] = 'libs'
        result = guest([paths['ffmpeg'], '-nostdin', '-hide_banner', '-loglevel', 'verbose',
                        '-xerror', '-filter_threads', '1', *args], env)
        row = {'exit': result.returncode, 'stderr': result.stderr[-20000:]}
        report['cases'][label] = row
        if result.returncode != 0:
            raise RuntimeError(f'{label} failed with native exit {result.returncode}')
        if library and ('calling init: '+scratch+'/'+library not in result.stderr
                        or 'calling init: /run/unifier-cuda/libcuda.so.1' not in result.stderr):
            raise RuntimeError(f'{label} did not load the expected GPU runtime')
        return row

    def readback(name):
        result = guest(['/usr/bin/base64', '-w0', '--', scratch+'/'+name])
        size = FRAMES * FRAME_BYTES
        if result.returncode or len(result.stdout) != 4*((size+2)//3):
            raise RuntimeError('NVIDIA media readback has the wrong length')
        payload = base64.b64decode(result.stdout, validate=True)
        if len(payload) != size:
            raise RuntimeError('NVIDIA media readback has the wrong frame count')
        (attempt.out/name).write_bytes(payload)
        return payload

    decode_output = ['-pix_fmt', 'nv12', '-threads', '1', '-fps_mode', 'passthrough',
                     '-c:v', 'rawvideo', '-f', 'rawvideo']
    try:
        service.start(fixture.env_id, expected_binding={
            'environment_id': fixture.env_id, 'environment_uuid': config['environment_uuid'],
            'generation': config['generation'], 'container_id': cid})
        started = True
        absent = guest(['/bin/sh', '-c', 'test ! -e "$1" && test ! -L "$1"', 'probe', scratch])
        if absent.returncode:
            raise RuntimeError('NVIDIA media guest path was already occupied')
        guard.verify_permission(config, attempt.out)
        cleanup = True
        runtime._check_copy(source, cid, scratch)
        check = guest(['/usr/bin/sha256sum', '--', *paths.values()])
        if check.returncode or check.stdout.splitlines() != [
                manifest[name]+'  '+path for name, path in paths.items()]:
            raise RuntimeError('NVIDIA media guest payload differs from its manifest')
        decode = ffmpeg('NVDEC', [
            '-threads', '1', '-hwaccel', 'cuda', '-hwaccel_device', '0', '-hwaccel_output_format', 'cuda',
            '-f', 'h264', '-i', paths['input.h264'], '-vf', 'hwdownload,format=nv12',
            *decode_output, scratch+'/nvdec.nv12'], library='libnvcuvid.so.1')
        if 'pixfmt:cuda' not in decode['stderr'] or readback('nvdec.nv12') != reference:
            raise RuntimeError('NVDEC hardware frames differ from the independent software reference')
        decode.update(frames=FRAMES, exact=True)
        encode = ffmpeg('NVENC', [
            '-threads', '1', '-f', 'rawvideo', '-pixel_format', 'nv12', '-video_size', '128x96',
            '-framerate', '25', '-i', paths['reference.nv12'], '-frames:v', str(FRAMES),
            '-c:v', 'h264_nvenc', '-gpu', '0', '-preset', 'p1', '-rc', 'constqp', '-qp', '18',
            '-bf', '0', '-f', 'h264', scratch+'/nvenc.h264'], library='libnvidia-encode.so.1')
        ffmpeg('software-readback', [
            '-threads', '1', '-hwaccel', 'none', '-f', 'h264', '-i', scratch+'/nvenc.h264',
            *decode_output, scratch+'/nvenc.nv12'])
        scores = psnr(readback('nvenc.nv12'), reference)
        if any(score is not None and score < 30 for score in scores):
            raise RuntimeError('NVENC software readback fell below 30 dB on a frame')
        encode.update(frames=FRAMES, psnr_db=scores)
        report['status'] = 'PASS'
        return 0
    except Exception as exc:
        report['error'] = str(exc)
        raise
    finally:
        try:
            try:
                if cleanup and guest(['/usr/bin/rm', '-rf', '--', scratch]).returncode:
                    raise RuntimeError('NVIDIA media guest probe cleanup failed')
            finally:
                if started:
                    service.stop(fixture.env_id)
                    report['formal_stop'] = 'STOPPED'
        except Exception as exc:
            report.update(status='INVALID', cleanup_error=str(exc))
            raise
        finally:
            guard.put(attempt.out/'result.json', report)
