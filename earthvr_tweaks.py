#!/usr/bin/env python3
"""
Earth VR quality tweaker (Google Earth VR 1.x Steam build, Earth.exe sha256 b07109d7...).

Starts Earth.exe (or attaches to a running one) and changes rendering-quality values in the
process's memory. Nothing on disk is modified: close Earth VR and start it normally to revert.

Launch mode (all tweaks):   python earthvr_tweaks.py [--config tweaks.json] [--exe PATH]
Attach mode (live tweaks):  python earthvr_tweaks.py --attach
Steam launch option:        "C:\\path\\to\\python.exe" "C:\\path\\to\\earthvr_tweaks.py" %command%

Global hotkeys while running (work while Earth VR has focus; a beep confirms each change):
  Ctrl+Alt+PageUp / PageDown   LOD bias +/- 0.25 (new detail streams in over a few seconds)
  Ctrl+Alt+F9                  A/B toggle: your tweaks <-> game defaults (live values only)
  Ctrl+Alt+F10                 measurement sweep over several LOD biases (hold still, ~90 s)
  Ctrl+Alt+F11                 print current values and metrics
Console keys (this window focused): ] [ LOD, d A/B, w sweep, s status, = - anisotropy, q stop tweaking.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import math
import msvcrt
import os
import struct
import sys
import time
import traceback

KNOWN_SHA256 = 'b07109d7a394ba011bd7aa5d27bf05f41b9954346e11ac8de2073fbd326a1da7'
STEAM_APP_ID = '348250'

# ---- Addresses (RVAs) found by reverse engineering this exact build ------------------------------
# gflags defined in vr/geo/earth/app/main/earth_app.cc. Constant-initialized in .data, so they are
# written while the process is still suspended, before any code runs.
FLAGS = {
    # 3D photogrammetry/terrain LOD bias. Mirth refines a tile while
    # log2(pixels covered by one texel) + bias > -0.2, so +1.0 = one extra octree level
    # (2x texel density) at the same distance. Read once at startup.
    'lod_bias':             (0x7eff0f0, '<d', 0.5),
    # MSAA sample count of the eye render targets. Read once when the targets are created.
    'msaa_samples':         (0x7eff0b8, '<i', 4),
    # Passed to Mirth's SetPerformanceBoost(). Read once at startup.
    'performance_boost':    (0x7eff0e8, '<B', 1),
    # Per-frame time budget for background jobs (tile decode/upload):
    #   budget = max(min_budget, time_until_vsync - margin). Read every frame (live-tweakable).
    'stream_min_budget_ms': (0x7eff120, '<d', 0.5),
    'stream_margin_ms':     (0x7eff110, '<d', 3.0),
    # Index into Earth VR's built-in start locations (-1 = normal start). Useful for repeatable tests.
    'start_location_index': (0x7eff0c0, '<i', -1),
}
# Mirth render settings compiled to plain globals; set by static initializers during CRT startup,
# so they are written after the process resumes. Read every frame (live-tweakable).
GLOBALS = {
    # Max anisotropic filtering for 3D mesh textures. 1.0 = off. The terrain textures are atlased
    # without mipmaps (the shader does its own 4-tap supersampling), so this changes very little.
    'anisotropy':           (0x7f80a6c, '<f', 1.0),
}
# Mirth memory cache target = clamp(available RAM MB - 2000, 1000, 8000) - 20.
# Raising the 8000 MB cap needs two in-memory patches in Earth.exe's code/rdata.
MEMCAP_CONST_RVA = 0xf29e60          # int 8000 used as std::min() operand (only user: 0x140071df0)
MEMCAP_CMP_IMM_RVA = 0x71e19         # imm32 of 'cmp ecx, 0x1f40' in the same function
MEMCAP_DEFAULT = 8000

# Object layout used for live LOD changes and diagnostics.
EFH_VTBLS = ((0x0, 0x10be5f8), (0x20, 0x10be658), (0x58, 0x10be670))   # mirth::earth::EarthFrameHandler
LODINFO_VTBL = 0x10be370                                                # mirth::tree::LodInfo
EFH_LODINFO_PTR = 0x2570             # shared_ptr<LodInfo>
EFH_LOD_BIAS = 0xa8                  # EarthSettings(+0x60).lod_bias(+0x48); copied to LodInfo every frame
EFH_TRAVERSER_PTR = 0x1e0            # mirth::earth::RockTraverser*
EFH_MESHES_DRAWN = 0x2ea8            # rock meshes drawn last frame
EFH_GEOMETRY_DRAWN = 0x2eac          # sum of per-mesh geometry counts drawn last frame
EFH_TEX_SHARP = 0x46db               # 1 = SHARP 4-tap texture kernel, else SOFT 5-tap (blurrier)
EFH_RENDER_MODE = 0x46e0             # 2 = reduced-detail mode (lod_bias - 2)
LODINFO_LOD_BIAS = 0x38
TRAV_LOD_BIAS = 0xd80                # float, bias actually used by the traversal this frame
TRAV_FOVY = 0xdc8                    # double, vertical field of view (radians)
TRAV_PIX_Y = 0xdd8                   # double, tan(fovy/2) / (viewport_height/2)

GAME_DEFAULTS = {'lod_bias': 0.5, 'anisotropy': 1.0}
SWEEP_BIASES = (0.0, 0.5, 1.0, 1.5, 2.0, 2.5)
SWEEP_SETTLE_S, SWEEP_SAMPLE_S = 9.0, 5.0

# ---- Win32 plumbing --------------------------------------------------------------------------------
k32 = ctypes.WinDLL('kernel32', use_last_error=True)
ntdll = ctypes.WinDLL('ntdll')
psapi = ctypes.WinDLL('psapi')
user32 = ctypes.WinDLL('user32')

CREATE_SUSPENDED = 0x4
CREATE_UNICODE_ENVIRONMENT = 0x400
PROCESS_ALL_ACCESS = 0x1FFFFF
MEM_COMMIT, MEM_PRIVATE = 0x1000, 0x20000
PAGE_READWRITE, PAGE_EXECUTE_READWRITE = 0x04, 0x40
STILL_ACTIVE = 259


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [('cb', wt.DWORD), ('lpReserved', wt.LPWSTR), ('lpDesktop', wt.LPWSTR),
                ('lpTitle', wt.LPWSTR), ('dwX', wt.DWORD), ('dwY', wt.DWORD), ('dwXSize', wt.DWORD),
                ('dwYSize', wt.DWORD), ('dwXCountChars', wt.DWORD), ('dwYCountChars', wt.DWORD),
                ('dwFillAttribute', wt.DWORD), ('dwFlags', wt.DWORD), ('wShowWindow', wt.WORD),
                ('cbReserved2', wt.WORD), ('lpReserved2', ctypes.c_void_p), ('hStdInput', wt.HANDLE),
                ('hStdOutput', wt.HANDLE), ('hStdError', wt.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [('hProcess', wt.HANDLE), ('hThread', wt.HANDLE),
                ('dwProcessId', wt.DWORD), ('dwThreadId', wt.DWORD)]


class PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [('ExitStatus', ctypes.c_void_p), ('PebBaseAddress', ctypes.c_void_p),
                ('AffinityMask', ctypes.c_void_p), ('BasePriority', ctypes.c_void_p),
                ('UniqueProcessId', ctypes.c_void_p), ('InheritedFromUniqueProcessId', ctypes.c_void_p)]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [('BaseAddress', ctypes.c_void_p), ('AllocationBase', ctypes.c_void_p),
                ('AllocationProtect', wt.DWORD), ('PartitionId', wt.WORD), ('RegionSize', ctypes.c_size_t),
                ('State', wt.DWORD), ('Protect', wt.DWORD), ('Type', wt.DWORD)]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [('cb', wt.DWORD), ('PageFaultCount', wt.DWORD), ('PeakWorkingSetSize', ctypes.c_size_t),
                ('WorkingSetSize', ctypes.c_size_t), ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPagedPoolUsage', ctypes.c_size_t), ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                ('QuotaNonPagedPoolUsage', ctypes.c_size_t), ('PagefileUsage', ctypes.c_size_t),
                ('PeakPagefileUsage', ctypes.c_size_t), ('PrivateUsage', ctypes.c_size_t)]


k32.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD,
                               ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
                               ctypes.POINTER(PROCESS_INFORMATION)]
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                  ctypes.POINTER(ctypes.c_size_t)]
k32.WriteProcessMemory.argtypes = k32.ReadProcessMemory.argtypes
k32.VirtualProtectEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, ctypes.POINTER(wt.DWORD)]
k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.ResumeThread.argtypes = [wt.HANDLE]
k32.ResumeThread.restype = wt.DWORD
k32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcess.restype = wt.HANDLE
k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
k32.FlushInstructionCache.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t]
ntdll.NtQueryInformationProcess.argtypes = [wt.HANDLE, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                            ctypes.POINTER(ctypes.c_ulong)]
psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short


def win_err(what):
    return OSError(f'{what} failed: {ctypes.WinError(ctypes.get_last_error())}')


class Proc:
    def __init__(self, h, pid):
        self.h, self.pid = h, pid
        self.base = self._image_base()

    def _image_base(self):
        pbi = PROCESS_BASIC_INFORMATION()
        st = ntdll.NtQueryInformationProcess(self.h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None)
        if st != 0:
            raise OSError(f'NtQueryInformationProcess status {st:#x}')
        return struct.unpack('<Q', self.read(pbi.PebBaseAddress + 0x10, 8))[0]

    def read(self, addr, n):
        buf = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t()
        if not k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)) or got.value != n:
            raise win_err(f'ReadProcessMemory({addr:#x})')
        return buf.raw

    def write(self, addr, data, protect=None):
        old = wt.DWORD()
        if protect is not None and not k32.VirtualProtectEx(self.h, ctypes.c_void_p(addr), len(data), protect,
                                                            ctypes.byref(old)):
            raise win_err('VirtualProtectEx')
        got = ctypes.c_size_t()
        ok = k32.WriteProcessMemory(self.h, ctypes.c_void_p(addr), data, len(data), ctypes.byref(got))
        if protect is not None:
            k32.VirtualProtectEx(self.h, ctypes.c_void_p(addr), len(data), old.value, ctypes.byref(wt.DWORD()))
            k32.FlushInstructionCache(self.h, ctypes.c_void_p(addr), len(data))
        if not ok or got.value != len(data):
            raise win_err(f'WriteProcessMemory({addr:#x})')

    def get(self, rva, fmt):
        return struct.unpack(fmt, self.read(self.base + rva, struct.calcsize(fmt)))[0]

    def put(self, rva, fmt, value, protect=None):
        self.write(self.base + rva, struct.pack(fmt, value), protect)

    def at(self, addr, fmt):
        return struct.unpack(fmt, self.read(addr, struct.calcsize(fmt)))[0]

    def alive(self):
        code = wt.DWORD()
        return k32.GetExitCodeProcess(self.h, ctypes.byref(code)) and code.value == STILL_ACTIVE

    def private_mb(self):
        pmc = PROCESS_MEMORY_COUNTERS_EX()
        pmc.cb = ctypes.sizeof(pmc)
        if psapi.GetProcessMemoryInfo(self.h, ctypes.byref(pmc), pmc.cb):
            return pmc.PrivateUsage / (1 << 20)
        return float('nan')

    def regions(self):
        mbi = MEMORY_BASIC_INFORMATION()
        addr = 0
        while k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            size = mbi.RegionSize
            if mbi.State == MEM_COMMIT and mbi.Type == MEM_PRIVATE and mbi.Protect == PAGE_READWRITE:
                yield (mbi.BaseAddress or 0), size
            addr = (mbi.BaseAddress or 0) + size
            if addr >= 0x7FFFFFFF0000:
                break


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 22), b''):
            h.update(chunk)
    return h.hexdigest()


def close_enough(a, b):
    return abs(a - b) < 1e-6 if isinstance(b, float) else a == b


# ---- SteamVR compositor statistics (optional; needs 'pip install openvr') ---------------------------
class VRStats:
    FIELDS = ('m_nNumFramePresents', 'm_nNumDroppedFrames', 'm_nNumReprojectedFrames', 'm_nNumFrameSubmits',
              'm_flSumApplicationGPUTimeMS', 'm_flSumApplicationCPUTimeMS')

    def __init__(self, log):
        self.ok = False
        try:
            import openvr
            self.openvr = openvr
            openvr.init(openvr.VRApplication_Background)
            self.comp = openvr.VRCompositor()
            self.size = ctypes.sizeof(openvr.Compositor_CumulativeStats)
            self.ok = True
            self.prev = self._snap()
        except Exception as e:  # SteamVR not running, package missing, ...
            log(f'  (SteamVR frame statistics unavailable: {e!r})')

    def _snap(self):
        s = self.comp.getCumulativeStats(self.size)
        return {f: getattr(s, f) for f in ('m_nPid',) + self.FIELDS}

    def window(self):
        """Stats accumulated since the previous call, or None."""
        if not self.ok:
            return None
        cur = self._snap()
        prev, self.prev = self.prev, cur
        if cur['m_nPid'] != prev['m_nPid']:
            return None
        d = {f: cur[f] - prev[f] for f in self.FIELDS}
        n = d['m_nNumFrameSubmits']
        presents = d['m_nNumFramePresents']
        if n <= 0 or presents <= 0:
            return None
        return {'app_gpu_ms': d['m_flSumApplicationGPUTimeMS'] / n,
                'app_cpu_ms': d['m_flSumApplicationCPUTimeMS'] / n,
                'reproj_pct': 100.0 * d['m_nNumReprojectedFrames'] / presents,
                'dropped': d['m_nNumDroppedFrames'], 'frames': presents}


# ---- Tweaks ------------------------------------------------------------------------------------------
def apply_flags(p, cfg, log):
    for key, (rva, fmt, default) in FLAGS.items():
        if key not in cfg or cfg[key] is None:
            continue
        cur = p.get(rva, fmt)
        if not close_enough(cur, default) and not close_enough(cur, cfg[key]):
            raise RuntimeError(f'{key}: expected default {default}, found {cur} - wrong build? aborting')
        val = int(bool(cfg[key])) if fmt == '<B' else cfg[key]
        p.put(rva, fmt, val)
        log(f'  {key:22} {default} -> {val}')


def apply_memcap(p, mb, log):
    if not mb:
        return
    if p.get(MEMCAP_CONST_RVA, '<i') != MEMCAP_DEFAULT or p.get(MEMCAP_CMP_IMM_RVA, '<i') != MEMCAP_DEFAULT:
        raise RuntimeError('memory cap patch site does not match - aborting')
    p.put(MEMCAP_CONST_RVA, '<i', int(mb), PAGE_READWRITE)
    p.put(MEMCAP_CMP_IMM_RVA, '<i', int(mb), PAGE_EXECUTE_READWRITE)
    log(f'  {"mirth_memory_cap_mb":22} {MEMCAP_DEFAULT} -> {int(mb)}  (actual = min(this, available RAM - 2000))')


def wait_and_apply_globals(p, cfg, log, timeout=30.0):
    """Globals are zero until their static initializer runs; wait for the default, then override."""
    pending = {k: v for k, v in GLOBALS.items() if cfg.get(k) is not None}
    t0 = time.time()
    while pending and time.time() - t0 < timeout and p.alive():
        for key, (rva, fmt, default) in list(pending.items()):
            if close_enough(p.get(rva, fmt), default):
                p.put(rva, fmt, cfg[key])
                log(f'  {key:22} {default} -> {cfg[key]}')
                del pending[key]
        time.sleep(0.002)
    if pending:
        log(f'  WARNING: globals never initialized: {list(pending)}')


def find_efhs(p):
    """Locate live EarthFrameHandler objects by their three vtable pointers + LodInfo link."""
    vt0 = struct.pack('<Q', p.base + EFH_VTBLS[0][1])
    found = []
    for base, size in p.regions():
        off = 0
        while off < size:
            n = min(size - off, 16 << 20)
            try:
                blob = p.read(base + off, n)
            except OSError:
                off += n
                continue
            i = blob.find(vt0)
            while i != -1:
                if i % 8 == 0:
                    a = base + off + i
                    try:
                        if all(p.at(a + o, '<Q') == p.base + v for o, v in EFH_VTBLS):
                            li = p.at(a + EFH_LODINFO_PTR, '<Q')
                            if li and p.at(li, '<Q') == p.base + LODINFO_VTBL:
                                found.append((a, li))
                    except OSError:
                        pass
                i = blob.find(vt0, i + 1)
            off += n
    return found


class Live:
    """Keeps live-tweakable values applied, reports metrics and handles hotkeys.

    lod_bias at startup comes from the patched gflag; live changes are written into every
    EarthFrameHandler's EarthSettings copy, which Mirth copies into the traverser every frame.
    """

    def __init__(self, p, cfg, log, attached, csv_path):
        self.p, self.log = p, log
        cfg_lod = cfg.get('lod_bias')
        self.tweaked = {
            'lod_bias': cfg_lod if cfg_lod is not None else GAME_DEFAULTS['lod_bias'],
            'anisotropy': cfg.get('anisotropy'),
            'stream_min_budget_ms': cfg.get('stream_min_budget_ms'),
            'stream_margin_ms': cfg.get('stream_margin_ms'),
        }
        self.cur = dict(self.tweaked)
        # In launch mode the startup LOD comes from the patched flag, so nothing needs writing until
        # the first live change. From then on the live value is always enforced (including a return
        # to the startup value). When attached, the configured value must be written live right away.
        self.live_lod = attached and cfg_lod is not None
        self.showing_defaults = False
        self.efhs = []
        self.last_scan = 0.0
        self.vr = VRStats(log)
        self.csv_path = csv_path
        self.sweep = None            # list of remaining (bias) steps while sweeping
        self.sweep_t0 = 0.0
        self.sweep_rows = []
        self.sweep_restore = None
        self.sweep_biases = SWEEP_BIASES
        self.sweeps_done = 0
        self.shot_dir = None

    # -- object discovery / enforcement --
    def rescan(self, quiet=False):
        t0 = self.last_scan = time.time()
        self.efhs = find_efhs(self.p)
        if not quiet or self.efhs:
            self.log(f'  found {len(self.efhs)} EarthFrameHandler(s) in {time.time() - t0:.1f}s' +
                     (': ' + ', '.join(f'{a:#x}' for a, _ in self.efhs) if self.efhs else ''))

    def enforce(self):
        """Re-assert live values (Earth VR may push its settings again, e.g. after menu changes)."""
        p = self.p
        if self.cur['anisotropy'] is not None:
            rva, fmt, _ = GLOBALS['anisotropy']
            if not close_enough(p.get(rva, fmt), self.cur['anisotropy']):
                p.put(rva, fmt, self.cur['anisotropy'])
        for key in ('stream_min_budget_ms', 'stream_margin_ms'):
            if self.cur[key] is not None:
                rva, fmt, _ = FLAGS[key]
                if not close_enough(p.get(rva, fmt), self.cur[key]):
                    p.put(rva, fmt, self.cur[key])
        lod = self.cur['lod_bias']
        if self.live_lod and lod is not None:
            if not self.efhs and time.time() - self.last_scan > 5:
                self.rescan()
            for a, li in list(self.efhs):
                try:
                    if abs(p.at(a + EFH_LOD_BIAS, '<d') - lod) > 1e-9:
                        p.write(a + EFH_LOD_BIAS, struct.pack('<d', lod))
                except OSError:
                    self.efhs.remove((a, li))

    # -- metrics --
    def metrics(self):
        p = self.p
        m = {'t': time.strftime('%H:%M:%S'), 'lod_setting': self.cur['lod_bias'], 'private_mb': p.private_mb()}
        if not self.efhs and time.time() - self.last_scan > 15:
            self.rescan(quiet=True)
        if self.efhs:
            a, _ = self.efhs[0]
            try:
                m['meshes'] = p.at(a + EFH_MESHES_DRAWN, '<i')
                m['geometry'] = p.at(a + EFH_GEOMETRY_DRAWN, '<i')
                m['tex_kernel'] = 'sharp' if p.at(a + EFH_TEX_SHARP, '<B') else 'soft'
                m['render_mode'] = p.at(a + EFH_RENDER_MODE, '<i')
                t = p.at(a + EFH_TRAVERSER_PTR, '<Q')
                if t:
                    m['lod_used'] = p.at(t + TRAV_LOD_BIAS, '<f')
                    fovy, pix_y = p.at(t + TRAV_FOVY, '<d'), p.at(t + TRAV_PIX_Y, '<d')
                    if pix_y > 0:
                        m['lod_view_height_px'] = round(2 * math.tan(fovy / 2) / pix_y)
                        m['fovy_deg'] = round(math.degrees(fovy), 1)
            except OSError:
                self.efhs = []
        w = self.vr.window()
        if w:
            m.update({k: (round(v, 2) if isinstance(v, float) else v) for k, v in w.items()})
        return m

    def fmt(self, m):
        parts = [f"lod={m.get('lod_used', float('nan')):.2f}",
                 f"meshes={m.get('meshes', '?')}", f"geom={m.get('geometry', '?')}",
                 f"mem={m['private_mb'] / 1024:.1f}GB"]
        if 'app_gpu_ms' in m:
            parts += [f"gpu={m['app_gpu_ms']:.1f}ms", f"cpu={m['app_cpu_ms']:.1f}ms",
                      f"reproj={m['reproj_pct']:.1f}%"]
        return '  ' + ' '.join(parts)

    def record(self, m):
        new = not os.path.exists(self.csv_path)
        cols = ['t', 'lod_setting', 'lod_used', 'meshes', 'geometry', 'private_mb', 'app_gpu_ms', 'app_cpu_ms',
                'reproj_pct', 'dropped', 'frames', 'tex_kernel', 'render_mode', 'lod_view_height_px', 'fovy_deg']
        with open(self.csv_path, 'a', encoding='utf-8') as f:
            if new:
                f.write(','.join(cols) + '\n')
            f.write(','.join('' if m.get(c) is None else str(m.get(c)) for c in cols) + '\n')

    def status(self):
        p = self.p
        m = self.metrics()
        extra = [f'anisotropy={p.get(GLOBALS["anisotropy"][0], "<f"):g}',
                 f'stream_min_budget_ms={p.get(FLAGS["stream_min_budget_ms"][0], "<d"):g}',
                 f'stream_margin_ms={p.get(FLAGS["stream_margin_ms"][0], "<d"):g}',
                 f"texture_kernel={m.get('tex_kernel', '?')}", f"render_mode={m.get('render_mode', '?')}",
                 f"LOD computed for {m.get('lod_view_height_px', '?')} px tall view (fovy {m.get('fovy_deg', '?')} deg)"]
        self.log('  ' + '  '.join(extra) + ('   [A/B: showing GAME DEFAULTS]' if self.showing_defaults else ''))
        self.log(self.fmt(m))

    # -- actions --
    def beep(self, value, lo, hi, ms=120):
        try:
            import winsound
            frac = 0.0 if hi == lo else max(0.0, min(1.0, (value - lo) / (hi - lo)))
            winsound.Beep(int(400 + 1200 * frac), ms)
        except Exception:
            pass

    def action(self, name):
        if self.sweep is not None and name != 'status':
            self.log('  sweep running - ignoring key (Ctrl+Alt+F10 again to abort)' if name != 'sweep' else '')
            if name == 'sweep':
                self.finish_sweep(aborted=True)
            return
        if name in ('lod_up', 'lod_down', 'ab', 'sweep'):
            self.live_lod = True
        if name in ('lod_up', 'lod_down'):
            base = self.cur['lod_bias'] if self.cur['lod_bias'] is not None else GAME_DEFAULTS['lod_bias']
            self.cur['lod_bias'] = round(base + (0.25 if name == 'lod_up' else -0.25), 3)
            self.log(f'  lod_bias -> {self.cur["lod_bias"]:g}')
            self.beep(self.cur['lod_bias'], -0.5, 3.0)
        elif name in ('aniso_up', 'aniso_down'):
            c = self.cur['anisotropy'] or GAME_DEFAULTS['anisotropy']
            self.cur['anisotropy'] = max(1.0, min(16.0, c * 2 if name == 'aniso_up' else c / 2))
            self.log(f'  anisotropy -> {self.cur["anisotropy"]:g}')
            self.beep(self.cur['anisotropy'], 1.0, 16.0)
        elif name == 'ab':
            self.showing_defaults = not self.showing_defaults
            if self.showing_defaults:
                self.cur = {'lod_bias': GAME_DEFAULTS['lod_bias'], 'anisotropy': GAME_DEFAULTS['anisotropy'],
                            'stream_min_budget_ms': FLAGS['stream_min_budget_ms'][2],
                            'stream_margin_ms': FLAGS['stream_margin_ms'][2]}
                self.log('  A/B -> GAME DEFAULTS (lod_bias 0.5, anisotropy 1, default streaming budget)')
                self.beep(0, 0, 1)
            else:
                self.cur = dict(self.tweaked)
                self.log(f'  A/B -> TWEAKED {self.cur}')
                self.beep(1, 0, 1)
        elif name == 'sweep':
            self.sweep_restore = self.cur['lod_bias']
            self.sweep = list(self.sweep_biases)
            self.sweep_rows = []
            self.log(f'  SWEEP start: biases {self.sweep_biases}, {SWEEP_SETTLE_S:g}s settle + {SWEEP_SAMPLE_S:g}s '
                     f'measure each. Hold still and keep looking at the same view.')
            self.next_sweep_step()
        elif name == 'status':
            self.status()

    def next_sweep_step(self):
        bias = self.sweep.pop(0)
        self.cur['lod_bias'] = bias
        self.sweep_t0 = time.time()
        self.sweep_sampled = False
        self.log(f'  sweep: lod_bias {bias:g}')
        self.beep(bias, -0.5, 3.0, ms=200)

    def tick_sweep(self):
        if self.sweep is None:
            return
        dt = time.time() - self.sweep_t0
        if not self.sweep_sampled and dt >= SWEEP_SETTLE_S:
            self.vr.window()                 # start a clean measurement window
            self.sweep_sampled = True
        elif self.sweep_sampled and dt >= SWEEP_SETTLE_S + SWEEP_SAMPLE_S:
            m = self.metrics()
            m['phase'] = 'sweep'
            if self.shot_dir:
                try:
                    shot = capture_window(self.p.pid, os.path.join(
                        self.shot_dir, f"{time.strftime('%H%M%S')}_lod{self.cur['lod_bias']:+.2f}.png"))
                    self.log(f'  screenshot {shot}')
                except Exception as e:
                    self.log(f'  screenshot failed: {e!r}')
            self.sweep_rows.append(m)
            self.record(m)
            self.log(self.fmt(m))
            if self.sweep:
                self.next_sweep_step()
            else:
                self.finish_sweep()

    def finish_sweep(self, aborted=False):
        self.cur['lod_bias'] = self.sweep_restore
        self.sweep = None
        self.sweeps_done += 1
        self.log('  SWEEP ' + ('aborted' if aborted else 'done') + f'; lod_bias restored to {self.sweep_restore:g}')
        for m in self.sweep_rows:
            self.log(f"    bias {m['lod_setting']:>4}: meshes {m.get('meshes', '?'):>5}  geometry {m.get('geometry', '?'):>8}"
                     f"  mem {m['private_mb'] / 1024:5.1f}GB  gpu {m.get('app_gpu_ms', float('nan')):5.1f}ms"
                     f"  cpu {m.get('app_cpu_ms', float('nan')):5.1f}ms  reproj {m.get('reproj_pct', float('nan')):5.1f}%")
        self.beep(1, 0, 1, ms=400)


def capture_window(pid, path):
    """Screenshot Earth VR's desktop mirror window (largest visible top-level window of pid)."""
    import win32gui, win32ui, win32process
    from PIL import Image
    wins = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == pid:
            l, t, r, b = win32gui.GetClientRect(hwnd)
            wins.append(((r - l) * (b - t), hwnd, r - l, b - t))
        return True
    win32gui.EnumWindows(cb, None)
    if not wins:
        return None
    _, hwnd, w, h = max(wins)
    hdc = win32gui.GetWindowDC(hwnd)
    src = win32ui.CreateDCFromHandle(hdc)
    mem = src.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(src, w, h)
    mem.SelectObject(bmp)
    ctypes.windll.user32.PrintWindow(hwnd, mem.GetSafeHdc(), 3)   # PW_CLIENTONLY | PW_RENDERFULLCONTENT
    info = bmp.GetInfo()
    img = Image.frombuffer('RGB', (info['bmWidth'], info['bmHeight']), bmp.GetBitmapBits(True), 'raw', 'BGRX', 0, 1)
    img.save(path)
    win32gui.DeleteObject(bmp.GetHandle())
    mem.DeleteDC()
    src.DeleteDC()
    win32gui.ReleaseDC(hwnd, hdc)
    return path


def resize_mirror(pid, w, h):
    import win32gui, win32process, win32con
    wins = []
    win32gui.EnumWindows(lambda hw, _: wins.append(hw) if win32gui.IsWindowVisible(hw) and
                         win32process.GetWindowThreadProcessId(hw)[1] == pid else None, None)
    for hw in wins:
        if win32gui.GetWindowText(hw):
            win32gui.SetWindowPos(hw, 0, 0, 0, w, h, win32con.SWP_NOZORDER)
            return True
    return False


# Global hotkeys (polled with GetAsyncKeyState so they work while Earth VR has focus).
VK_CONTROL, VK_MENU = 0x11, 0x12
# Ctrl+Alt + PageUp/PageDown/F9/F10/F11 (avoids Ctrl+Alt+letter hotkeys used by Lossless Scaling etc.)
HOTKEYS = {0x21: 'lod_up', 0x22: 'lod_down', 0x78: 'ab', 0x79: 'sweep', 0x7A: 'status'}
CONSOLE_KEYS = {']': 'lod_up', '[': 'lod_down', '=': 'aniso_up', '-': 'aniso_down', 'd': 'ab', 's': 'status',
                'w': 'sweep'}


def poll_hotkeys(prev):
    down = lambda vk: bool(user32.GetAsyncKeyState(vk) & 0x8000)
    fired = []
    mods = down(VK_CONTROL) and down(VK_MENU)
    for vk, name in HOTKEYS.items():
        now = mods and down(vk)
        if now and not prev.get(vk):
            fired.append(name)
        prev[vk] = now
    return fired


def build_env():
    env = dict(os.environ)
    env.setdefault('SteamAppId', STEAM_APP_ID)
    env.setdefault('SteamGameId', STEAM_APP_ID)
    block = ''.join(f'{k}={v}\0' for k, v in sorted(env.items(), key=lambda kv: kv[0].upper())) + '\0'
    return ctypes.create_unicode_buffer(block, len(block))


def launch(exe, extra_args, log):
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    pi = PROCESS_INFORMATION()
    cmdline = ctypes.create_unicode_buffer(' '.join([f'"{exe}"'] + extra_args))
    env = build_env()
    if not k32.CreateProcessW(exe, cmdline, None, None, False, CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT,
                              env, os.path.dirname(exe), ctypes.byref(si), ctypes.byref(pi)):
        raise win_err('CreateProcessW')
    log(f'started Earth.exe suspended (pid {pi.dwProcessId})')
    return pi


def find_running():
    import subprocess
    out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq Earth.exe', '/FO', 'CSV', '/NH'],
                         capture_output=True, text=True).stdout
    return [int(line.split('","')[1]) for line in out.splitlines() if line.startswith('"Earth.exe"')]


def image_path(h):
    buf = ctypes.create_unicode_buffer(1024)
    n = wt.DWORD(1024)
    if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
        raise win_err('QueryFullProcessImageNameW')
    return buf.value


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('exe', nargs='?', help='path to Earth.exe (Steam passes it via %%command%%)')
    ap.add_argument('extra', nargs=argparse.REMAINDER, help='extra arguments passed to Earth.exe')
    ap.add_argument('--config', default=os.path.join(here, 'tweaks.json'))
    ap.add_argument('--attach', action='store_true', help='attach to a running Earth.exe (live tweaks only)')
    ap.add_argument('--dry-run', action='store_true', help='verify addresses in a suspended process, then kill it')
    ap.add_argument('--no-live', action='store_true', help='apply startup tweaks and exit (no live keys)')
    ap.add_argument('--metrics-every', type=float, default=5.0, help='seconds between metrics log lines (0=off)')
    ap.add_argument('--auto-sweep', type=float, default=0, metavar='SEC',
                    help='start a sweep automatically SEC seconds after launch (unattended testing)')
    ap.add_argument('--sweep-biases', help='comma-separated LOD biases for the sweep, e.g. 0,0.5,1,1.5,2')
    ap.add_argument('--screenshots', metavar='DIR', help='save a mirror-window screenshot at each sweep step')
    ap.add_argument('--exit-when-done', action='store_true', help='close Earth VR after the auto sweep')
    ap.add_argument('--mirror-size', metavar='WxH', help='resize the desktop mirror window (for screenshots)')
    args = ap.parse_args()

    logf = open(os.path.join(here, 'earthvr_tweaks.log'), 'a', encoding='utf-8')

    def log(msg):
        if not msg:
            return
        line = time.strftime('%H:%M:%S ') + msg
        print(line, flush=True)
        logf.write(line + '\n')
        logf.flush()

    with open(args.config, encoding='utf-8') as f:
        cfg = {k: v for k, v in json.load(f).items() if not k.startswith('_')}
    log(f'config {args.config}: {cfg}')

    if args.attach:
        pids = find_running()
        if not pids:
            sys.exit('Earth.exe is not running')
        h = k32.OpenProcess(PROCESS_ALL_ACCESS, False, pids[0])
        if not h:
            raise win_err('OpenProcess')
        exe = image_path(h)
        if sha256(exe) != KNOWN_SHA256:
            sys.exit(f'{exe}: unknown Earth.exe build, refusing to patch')
        p = Proc(h, pids[0])
        log(f'attached to pid {pids[0]} ({exe}), image base {p.base:#x}')
        log('  (startup-only values msaa/performance_boost/memcap cannot change now; lod_bias is applied live)')
    else:
        exe = os.path.abspath(args.exe or cfg.get('exe') or os.path.join(here, '..', 'Earth.exe'))
        if sha256(exe) != KNOWN_SHA256:
            sys.exit(f'{exe}: unknown Earth.exe build (sha256 mismatch), refusing to patch')
        pi = launch(exe, args.extra or [], log)
        p = Proc(pi.hProcess, pi.dwProcessId)
        log(f'image base {p.base:#x}')
        try:
            apply_flags(p, cfg, log)
            apply_memcap(p, cfg.get('mirth_memory_cap_mb'), log)
        except Exception:
            k32.TerminateProcess(pi.hProcess, 1)
            raise
        if args.dry_run:
            for key, (rva, fmt, default) in GLOBALS.items():
                log(f'  {key}: pre-init value {p.get(rva, fmt)} (expected 0 before static init)')
            k32.TerminateProcess(pi.hProcess, 0)
            log('dry run OK, process terminated')
            return
        k32.ResumeThread(pi.hThread)
        log('resumed')
        wait_and_apply_globals(p, cfg, log)

    if args.no_live:
        log('startup tweaks applied; exiting (live values are not re-enforced)')
        return

    live = Live(p, cfg, log, attached=args.attach, csv_path=os.path.join(here, 'metrics.csv'))
    if args.sweep_biases:
        live.sweep_biases = tuple(float(x) for x in args.sweep_biases.split(','))
    if args.screenshots:
        os.makedirs(args.screenshots, exist_ok=True)
        live.shot_dir = args.screenshots
    auto_sweep_pending = args.auto_sweep > 0
    mirror_done = False
    log('live mode. Hotkeys: Ctrl+Alt+PageUp/PageDown LOD bias, Ctrl+Alt+F9 A/B vs defaults, '
        'Ctrl+Alt+F10 sweep, Ctrl+Alt+F11 status. Console: q = stop tweaking')
    t_start, last_metrics = time.time(), time.time()
    last, prev = 0.0, {}
    has_console = sys.stdin is not None and sys.stdin.isatty()
    while p.alive():
        try:
            actions = poll_hotkeys(prev)
            while has_console and msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch == 'q':
                    log('stopped tweaking; Earth VR keeps running with the current values')
                    return
                if ch in CONSOLE_KEYS:
                    actions.append(CONSOLE_KEYS[ch])
            for a in actions:
                live.action(a)
                last = 0.0
            now = time.time()
            if args.mirror_size and not mirror_done and now - t_start > 15:
                mirror_done = resize_mirror(p.pid, *map(int, args.mirror_size.lower().split('x')))
                if mirror_done:
                    log(f'  mirror window resized to {args.mirror_size}')
            if auto_sweep_pending and now - t_start >= args.auto_sweep:
                auto_sweep_pending = False
                live.action('sweep')
            if args.exit_when_done and args.auto_sweep > 0 and live.sweeps_done and live.sweep is None:
                log('auto sweep finished; closing Earth VR')
                k32.TerminateProcess(p.h, 0)
                break
            if now - last > 0.25:
                live.enforce()
                live.tick_sweep()
                last = now
            if (args.metrics_every > 0 and live.sweep is None and now - last_metrics >= args.metrics_every
                    and now - t_start > 20):
                m = live.metrics()
                live.record(m)
                log(live.fmt(m))
                last_metrics = now
        except OSError as e:
            if not p.alive():
                break
            log(f'  warning: {e}')
            time.sleep(0.5)
        except Exception:
            log('  ERROR in live loop:\n' + traceback.format_exc())
            time.sleep(1.0)
        time.sleep(0.02)
    log('Earth VR exited')


if __name__ == '__main__':
    main()
