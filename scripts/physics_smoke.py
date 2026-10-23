"""Read-only smoke test using AC's existing public shared-memory telemetry."""
import ctypes
from ctypes import wintypes
from pathlib import Path
import json
import math
import struct
import time

kernel=ctypes.WinDLL('kernel32',use_last_error=True)
kernel.OpenFileMappingW.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.LPCWSTR]
kernel.OpenFileMappingW.restype=wintypes.HANDLE
kernel.MapViewOfFile.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.c_size_t]
kernel.MapViewOfFile.restype=ctypes.c_void_p
kernel.UnmapViewOfFile.argtypes=[ctypes.c_void_p]
kernel.CloseHandle.argtypes=[wintypes.HANDLE]
handle=kernel.OpenFileMappingW(4,False,'Local\\acpmf_physics')
if not handle:
    raise SystemExit('Assetto Corsa physics telemetry is not open')
view=kernel.MapViewOfFile(handle,4,0,0,0)
if not view:
    kernel.CloseHandle(handle)
    raise ctypes.WinError(ctypes.get_last_error())
snapshots=[]
try:
    for _ in range(3):
        raw=ctypes.string_at(view,200)
        sample={'packet_id':struct.unpack_from('<i',raw,0)[0],
                'speed_kmh':struct.unpack_from('<f',raw,28)[0],
                'wheel_load_n':list(struct.unpack_from('<4f',raw,72)),
                'suspension_travel_m':list(struct.unpack_from('<4f',raw,184))}
        snapshots.append(sample)
        time.sleep(1)
finally:
    kernel.UnmapViewOfFile(view);kernel.CloseHandle(handle)
all_loaded=all(all(math.isfinite(v) and v>100 for v in s['wheel_load_n']) for s in snapshots)
report={'passed':all_loaded,'meaning':'All four wheels maintain physical ground contact at spawn',
        'scope':'Spawn smoke only; not a complete driven route or multiplayer test','samples':snapshots}
root=Path(__file__).resolve().parents[1]
cfg=json.loads((root/'tracks/altenhain.json').read_text(encoding='utf-8'))
report.update(track=cfg['id'],version=cfg['version'])
(root/'test-results/physics-smoke.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
raise SystemExit(0 if all_loaded else 1)
