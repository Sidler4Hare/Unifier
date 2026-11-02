#!/usr/bin/env python3
"""Send one JSON command to the explicitly registered FreeCAD GUI fixture."""
import json
from pathlib import Path
import sys
import time

BASE = Path(__file__).resolve().parent
CONTROL = BASE / 'host-project/control'


def command(payload, timeout=15):
    token=str(time.time_ns())
    path=CONTROL/f'command-{token}.json'
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload,ensure_ascii=False))
    temporary.replace(path)
    reply=CONTROL/f'reply-{token}.json'
    deadline=time.monotonic()+timeout
    while not reply.exists() and time.monotonic()<deadline:time.sleep(.1)
    if not reply.exists():raise TimeoutError(str(reply))
    result=json.loads(reply.read_text())
    if not result['ok']:raise RuntimeError(result['error'])
    return result['result']


if __name__=='__main__':
    result=command(json.loads(sys.argv[1]))
    if isinstance(result,dict) and 'actions' in result:
        result['actions']=[a for a in result['actions'] if a['name'] in (
            'Std_Open','Std_Save','Std_SaveAs','Std_New','Std_Close','Part_Box','Std_Quit')]
    print(json.dumps(result,ensure_ascii=False,indent=2))
