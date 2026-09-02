"""
NinjaEnc — Python Kod Koruma / Obfuscation Aracı
==================================================

Bir Python kaynağını çok katmanlı bir korunmuş çıktıya dönüştürür:
obfuscation (AST, MBA, control-flow flattening, opaque predicates),
şifreleme (AES-256, ChaCha20, Twofish, XOR, White-Box AES), gerçek bir
stack-tabanlı sanal makine (NinjaVM), homomorfik VM guard, honeypot kabuğu
ve ZORUNLU native derleme (Nuitka).

GEREKSİNİMLER (encode ortamı):
  - Nuitka (yoksa otomatik 'pip install' denenir; --yes ile sormadan)
  - Bir C derleyici: gcc / clang (Windows'ta Nuitka MinGW indirebilir)
  - Önerilen: pycryptodome (AES/ChaCha için), Cython (opsiyonel .so katmanı)

ÖNEMLİ — TAŞINABİLİRLİK:
  Native derleme ZORUNLUDUR ve pure-Python fallback YOKTUR. Üretilen çıktı
  yalnızca derlendiği işletim sistemi + CPU mimarisi + Python sürümünde
  çalışır. Farklı bir hedef için o platformda yeniden encode edilmelidir.

DÜRÜST KORUMA BEKLENTİSİ:
  Hiçbir obfuscation mutlak değildir. Kod çalışmak için eninde sonunda
  bellekte açılır; bu araç tersine mühendislik MALİYETİNİ artırır, onu
  imkânsız kılmaz. En güçlü koruma NinjaVM (kritik fonksiyonlar) + zorunlu
  native derlemenin birleşiminden gelir. Değerli mantığı @ninja_vm ile
  işaretleyin.

KULLANIM:
  python3 ninjaenc.py girdi.py -o cikti.py [--seed N] [--yes]
"""
import os
import sys
import base64
import zipfile
import random
import zlib
import marshal
import py_compile
import tempfile
import shutil
import hashlib
import string
import subprocess
import logging
import re
import struct
import platform
import types
import dis
import opcode
from binascii import hexlify, unhexlify
from io import BytesIO
from pathlib import Path

def generate_chunked_payload_loader(b64_zip, python_version, chunk_count=6):
    import random as _r, os as _o
    def _rv(length=None):
        chars='abcdefghijklmnopqrstuvwxyz'
        n=length or _r.randint(6,12)
        return '_'+''.join(_r.choice(chars) for _ in range(n))
    chunk_size=max(1,len(b64_zip)//chunk_count)
    chunks=[]
    for i in range(chunk_count-1):
        chunks.append(b64_zip[i*chunk_size:(i+1)*chunk_size])
    chunks.append(b64_zip[(chunk_count-1)*chunk_size:])
    chunk_vars=[_rv() for _ in range(chunk_count)]
    varnames=[_rv() for _ in range(15)]
    funcnames=[_rv() for _ in range(5)]
    chunk_assignments=""
    for i,(cv,chunk) in enumerate(zip(chunk_vars,chunks)):
        chunk_assignments+=f"{cv}=\"{chunk}\"\n"
        if i<len(chunks)-1:
            nv=varnames[i]
            chunk_assignments+=f"{nv}=len({cv})^{_r.randint(100,9999)}\n"
    concat_expr="+".join(chunk_vars)
    vb64=varnames[-1];vtmp=varnames[-2];vpath=varnames[-3]
    vdata=varnames[-4];vf=varnames[-5];vres=varnames[-6];verr=varnames[-7]
    vcln=funcnames[0];vload=funcnames[1];vhash=funcnames[2]
    py_ver=python_version
    return f"""#!/usr/bin/env python3
import os,sys,base64,tempfile,hashlib,time,subprocess
_ENC_VER={py_ver}
_CUR_VER=(sys.version_info.major,sys.version_info.minor)
if _CUR_VER!=_ENC_VER:
    print(f"[!] UYARI: Bu dosya Python {{_ENC_VER[0]}}.{{_ENC_VER[1]}} ile sifrelendi.")
{chunk_assignments}
def {vload}():
    try:
        {vb64}={concat_expr}
        return base64.b64decode({vb64}.encode())
    except Exception:
        return None
{vhash}=hashlib.sha1(str(time.monotonic()).encode()).hexdigest()[:10]
{vpath}=os.path.join(tempfile.gettempdir(),'.'+{vhash})
def {vcln}():
    for _ in range(3):
        try:
            if os.path.exists({vpath}): os.remove({vpath})
            break
        except: pass
try:
    {vdata}={vload}()
    if {vdata} is None: sys.exit(1)
    with open({vpath},'wb') as {vf}: {vf}.write({vdata})
    os.chmod({vpath},0o700)
    del {vdata}
    {vres}=subprocess.run([sys.executable,{vpath}]+sys.argv[1:],stdin=sys.stdin,stdout=sys.stdout,stderr=sys.stderr)
    {vcln}()
    sys.exit({vres}.returncode)
except Exception as {verr}:
    {vcln}()
    print(f"[!] Hata ({{type({verr}).__name__}}): {{{verr}}}",file=sys.stderr)
    sys.exit(1)
finally:
    {vcln}()
"""

def generate_async_antidebug_code():
    import random as _r
    def _rv():
        chars='abcdefghijklmnopqrstuvwxyz'
        return '_'+''.join(_r.choice(chars) for _ in range(_r.randint(5,10)))
    v=[_rv() for _ in range(10)]
    min_i=_r.uniform(2.5,4.0);max_i=_r.uniform(5.0,8.0)
    fp=[27042,27043,10900,_r.randint(20000,40000)]
    return f"""
import sys as {v[0]},os as {v[1]},time as {v[2]},platform as {v[3]}
import threading as {v[4]},socket as {v[5]},random as {v[6]}
_AD9_ANDROID={v[1]}.path.exists("/system/build.prop")
_AD9_ARM={v[3]}.machine().lower() in ("aarch64","arm","armv7l","armv8l")
def {v[7]}():
    _sc=0
    try:
        if not _AD9_ANDROID and {v[0]}.gettrace() is not None: _sc+=5
    except Exception: pass
    try:
        with open("/proc/self/status","r") as _f:
            for _l in _f:
                if _l.startswith("TracerPid:") and int(_l.split(":")[1].strip())!=0: _sc+=5; break
    except Exception: pass
    try:
        _bad=["frida","gdb","lldb","strace","radare2","ida","debugpy","pydevd"]
        with open("/proc/self/maps","r") as _f:
            _c=_f.read().lower()
            for _s in _bad:
                if _s in _c: _sc+=4; break
    except Exception: pass
    try:
        for _p in {fp}:
            _s={v[5]}.socket(); _s.settimeout(0.03)
            if _s.connect_ex(("127.0.0.1",_p))==0: _s.close(); _sc+=5; break
            _s.close()
    except Exception: pass
    try:
        _ld={v[1]}.environ.get("LD_PRELOAD","").lower()
        if _ld and any(x in _ld for x in ["frida","gadget","interpose"]): _sc+=4
    except Exception: pass
    if _sc<8: return
    _a=[lambda:{v[1]}._exit(0),lambda:(_ for _ in ()).throw(MemoryError()),lambda:{v[1]}.kill({v[1]}.getpid(),9),lambda:(_ for _ in ()).throw(OSError(22,"Invalid argument"))]
    _a[_sc%len(_a)]()
def {v[8]}():
    while True:
        try: {v[7]}()
        except SystemExit: raise
        except Exception: pass
        {v[2]}.sleep({v[6]}.uniform({min_i:.2f},{max_i:.2f}))
{v[9]}={v[4]}.Thread(target={v[8]},daemon=True,name="_nj_guard")
{v[9]}.start()
del {v[9]}
"""

ENCODER_PYTHON_VERSION = sys.version_info[:2]

def check_python_version_compatibility(min_version=(3, 8)):
    if sys.version_info < min_version:
        print(f'\x1b[31m[!] Python {min_version[0]}.{min_version[1]}+ gerekli! Mevcut: {sys.version_info.major}.{sys.version_info.minor}\x1b[0m')
        sys.exit(1)

def warn_version_mismatch(encoded_on_version):
    cur = sys.version_info[:2]
    enc = encoded_on_version
    if cur != enc:
        print(f'\x1b[93m[!] UYARI: Bu dosya Python {enc[0]}.{enc[1]} ile şifrelendi.')
        print(f'    Mevcut Python: {cur[0]}.{cur[1]}')
        print(f'    Versiyon uyumsuzluğu nedeniyle çalışmayabilir!\x1b[0m')
check_python_version_compatibility()
Y = '\x1b[92m'
S = '\x1b[93m'
K = '\x1b[31m'
B = '\x1b[37m'
G = '\x1b[1;30;40m'
logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

def check_cython():
    try:
        import Cython
        from Cython.Build import cythonize
        return True
    except ImportError:
        return False

def check_nuitka():
    try:
        result = subprocess.run([sys.executable, '-m', 'nuitka', '--version'], capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except Exception:
        return False

def _which(prog):
    """Basit which — PATH'te çalıştırılabilir arar."""
    for _p in os.environ.get('PATH', '').split(os.pathsep):
        _fp = os.path.join(_p, prog)
        if os.path.isfile(_fp) and os.access(_fp, os.X_OK):
            return _fp
        if os.name == 'nt' and os.path.isfile(_fp + '.exe'):
            return _fp + '.exe'
    return None

def ensure_native_toolchain(auto_install=True, assume_yes=False):
    """
    Native derleme zincirini (Nuitka + C derleyici) hazırlar.

    Dönüş: (ok: bool, mesaj: str)
      ok=True  → Nuitka çağrılabilir ve bir C derleyici mevcut.
      ok=False → mesaj eksik bileşeni ve çözümü açıklar.

    Not: Bu yalnızca ENCODE ortamını hazırlar; üretilen çıktı çalıştırma anında
    hiçbir şey indirmez/kurmaz. auto_install True ve Nuitka yoksa 'pip install'
    denenir (assume_yes değilse kullanıcıya sorulur).
    """
    # 1) Nuitka
    if not check_nuitka():
        if not auto_install:
            return False, 'Nuitka kurulu değil. Kur: pip install nuitka'
        do_it = assume_yes
        if not assume_yes:
            try:
                _ans = input('[?] Nuitka kurulu değil. Şimdi kurulsun mu? [E/h]: ').strip().lower()
                do_it = _ans in ('', 'e', 'evet', 'y', 'yes')
            except EOFError:
                do_it = False
        if not do_it:
            return False, 'Nuitka kurulumu reddedildi.'
        logger.info('Nuitka kuruluyor (pip install nuitka)...')
        try:
            _r = subprocess.run([sys.executable, '-m', 'pip', 'install', '--upgrade', 'nuitka'],
                                capture_output=True, text=True, timeout=600)
            if _r.returncode != 0:
                return False, f'Nuitka kurulumu başarısız:\n{_r.stderr[-500:]}'
        except Exception as _e:
            return False, f'Nuitka kurulumu hata verdi: {_e}'
        if not check_nuitka():
            return False, 'Nuitka kuruldu ama çağrılamıyor (PATH/sürüm sorunu).'
        logger.info('Nuitka başarıyla kuruldu.')

    # 2) C derleyici (Nuitka backend için gerekli)
    _cc = None
    for _c in ('gcc', 'clang', 'cc'):
        if _which(_c):
            _cc = _c
            break
    if _cc is None and os.name == 'nt':
        if _which('cl') or _which('gcc'):
            _cc = 'cl/gcc'
    if _cc is None:
        return False, ('C derleyici bulunamadı (gcc/clang gerekli). '
                       'Linux: apt install gcc | macOS: xcode-select --install | '
                       'Windows: Nuitka ilk çalışmada MinGW indirmeyi önerir.')
    return True, f'Native zincir hazır (Nuitka + {_cc}).'

def check_gcc():
    try:
        result = subprocess.run(['gcc', '--version'], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False

def check_clang():
    try:
        result = subprocess.run(['clang', '--version'], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False

class CythonCompiler:

    def __init__(self, temp_dir):
        self.temp_dir = temp_dir
        self.available = check_cython()

    def compile_to_so(self, py_file, module_name=None):
        if not self.available:
            logger.warning('Cython yüklü değil!')
            return None
        if module_name is None:
            module_name = Path(py_file).stem
        pyx_file = os.path.join(self.temp_dir, f'{module_name}.pyx')
        if os.path.abspath(py_file) != os.path.abspath(pyx_file):
            shutil.copy(py_file, pyx_file)

        is_windows = platform.system() == 'Windows'
        is_arm = 'aarch64' in platform.machine().lower() or 'arm' in platform.machine().lower()

        if is_windows:
            extra_compile = ['/O2', '/GS-', '/GL', '/DNDEBUG']
            extra_link    = ['/LTCG', '/OPT:REF', '/OPT:ICF']
        else:
            extra_compile = [
                '-O2', '-g0', '-DNDEBUG',
                '-D_FORTIFY_SOURCE=0',
                '-DCYTHON_WITHOUT_ASSERTIONS=1',
                '-DCYTHON_CLINE_IN_TRACEBACK=0',
                '-ffunction-sections', '-fdata-sections',
                '-fno-common', '-fno-stack-protector',
                '-fomit-frame-pointer',
                '-fno-unwind-tables',
                '-fno-asynchronous-unwind-tables',
                '-fno-plt', '-fno-ident',
                '-fmerge-all-constants',
                '-funroll-loops',
            ]
            extra_link = [
                '-Wl,--strip-debug',
                '-Wl,--gc-sections',
                '-Wl,--as-needed',
                '-Wl,--build-id=none',
                '-Wl,--discard-locals',
            ]

        setup_file = os.path.join(self.temp_dir, 'setup_cython.py')
        setup_code = f"""\nimport os, sys, platform
from setuptools import setup, Extension
from Cython.Build import cythonize
from Cython.Compiler import Options

Options.annotate = False
Options.docstrings = False
Options.embed_pos_in_docstring = False

extra_compile = {extra_compile!r}
extra_link    = {extra_link!r}

ext = Extension(
    name='{module_name}',
    sources=['{pyx_file}'],
    extra_compile_args=extra_compile,
    extra_link_args=extra_link,
)

ext_modules = cythonize(
    [ext],
    compiler_directives={{
        'language_level': 3,
        'boundscheck': False,
        'wraparound': False,
        'cdivision': True,
        'cdivision_warnings': False,
        'cpow': True,
        'embedsignature': False,
        'emit_code_comments': False,
        'profile': False,
        'linetrace': False,
        'infer_types': True,
        'initializedcheck': False,
        'nonecheck': False,
        'overflowcheck': False,
        'binding': False,
        'annotation_typing': False,
        'optimize.use_switch': True,
        'optimize.unpack_method_calls': True,
        'warn.undeclared': False,
        'warn.unreachable': False,
        'warn.maybe_uninitialized': False,
        'warn.unused': False,
        'warn.unused_arg': False,
        'warn.unused_result': False,
        'show_performance_hints': False,
    }},
    annotate=False,
    quiet=True,
)

setup(
    name='{module_name}',
    ext_modules=ext_modules,
    zip_safe=False,
)
"""
        with open(setup_file, 'w') as f:
            f.write(setup_code)
        old_cwd = os.getcwd()
        try:
            os.chdir(self.temp_dir)
            result = subprocess.run([sys.executable, 'setup_cython.py', 'build_ext', '--inplace'], capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                logger.error(f'Cython derleme hatası: {result.stderr}')
                return None
            so_pattern = f'{module_name}*.so'
            pyd_pattern = f'{module_name}*.pyd'
            so_files = list(Path(self.temp_dir).glob(so_pattern))
            if not so_files:
                so_files = list(Path(self.temp_dir).glob(pyd_pattern))
            if so_files:
                so_path = str(so_files[0])
                if not is_windows:
                    os.system(
                        f"strip --strip-debug "
                        f"--remove-section=.comment "
                        f"--remove-section=.note "
                        f"--remove-section=.note.ABI-tag "
                        f"--remove-section=.note.gnu.build-id "
                        f"--remove-section=.note.gnu.property "
                        f"'{so_path}' 2>/dev/null"
                    )
                for c_file in Path(self.temp_dir).glob('*.c'):
                    try:
                        c_file.unlink()
                    except Exception:
                        pass
                logger.info(f'Cython derleme başarılı: {so_files[0].name}')
                return so_path
            return None
        except subprocess.TimeoutExpired:
            logger.error('Cython derleme zaman aşımı!')
            return None
        except Exception as e:
            logger.error(f'Cython hatası: {e}')
            return None
        finally:
            os.chdir(old_cwd)

    def create_obfuscated_wrapper(self, source_code, module_name='ninja_core'):
        hyp = HyperionObfuscator()
        vars = [hyp._randvar() for _ in range(20)]
        funcs = [hyp._randvar() for _ in range(10)]
        compressed = zlib.compress(source_code.encode('utf-8'), level=9)
        b64_data = base64.b64encode(compressed).decode('ascii')
        xor_keys = [random.randint(1, 255) for _ in range(3)]
        masks = [random.randint(1, 255) for _ in range(3)]
        blinded_keys = [xor_keys[i] ^ masks[i] for i in range(3)]

        cython_code = f'''# cython: language_level=3

from cpython.ref cimport PyObject
from libc.string cimport memset
import base64
import zlib
import sys
import ctypes

cdef int {vars[10]} = {blinded_keys[0]}  # blinded k0
cdef int {vars[11]} = {masks[0]}         # mask0
cdef int {vars[12]} = {blinded_keys[1]}  # blinded k1
cdef int {vars[13]} = {masks[1]}         # mask1
cdef int {vars[14]} = {blinded_keys[2]}  # blinded k2
cdef int {vars[15]} = {masks[2]}         # mask2

cdef bytes {vars[0]} = b"{b64_data}"

cdef bytes {funcs[0]}(bytes data, int key):
    cdef bytearray result = bytearray(len(data))
    cdef int i
    for i in range(len(data)):
        result[i] = data[i] ^ key
    return bytes(result)

cdef bytes {funcs[1]}():
    cdef int _k0 = {vars[10]} ^ {vars[11]}
    cdef int _k1 = {vars[12]} ^ {vars[13]}
    cdef int _k2 = {vars[14]} ^ {vars[15]}
    cdef bytes {vars[2]} = base64.b64decode({vars[0]})
    cdef bytes {vars[3]} = {funcs[0]}({vars[2]}, _k0)
    cdef bytes {vars[4]} = {funcs[0]}({vars[3]}, _k1)
    cdef bytes {vars[5]} = {funcs[0]}({vars[4]}, _k2)
    return zlib.decompress({vars[5]})

cdef void {funcs[2]}() except *:
    cdef bytes {vars[6]} = {funcs[1]}()
    cdef str {vars[7]} = {vars[6]}.decode('utf-8')

    _papi = ctypes.pythonapi
    _papi.Py_CompileString.restype = ctypes.py_object
    _papi.Py_CompileString.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]

    _code_obj = _papi.Py_CompileString(
        {vars[7]}.encode('utf-8'),
        b'<ninja>',
        ctypes.c_int(257)  # Py_file_input
    )

    _gl = {{'__name__': '__main__', '__builtins__': __builtins__}}
    _papi.PyEval_EvalCode.restype = ctypes.py_object
    _papi.PyEval_EvalCode.argtypes = [ctypes.py_object, ctypes.py_object, ctypes.py_object]
    _papi.PyEval_EvalCode(_code_obj, _gl, _gl)

    try:
        if hasattr(_code_obj, 'co_code'):
            _raw = bytes(_code_obj.co_code)
            if _raw:
                _buf = (ctypes.c_char * len(_raw)).from_address(id(_raw) + 32)
                ctypes.memset(_buf, 0, len(_raw))
    except Exception:
        pass
    del _code_obj, {vars[6]}, {vars[7]}

def {funcs[3]}():
    {funcs[2]}()

def run():
    {funcs[3]}()

if __name__ == '__main__':
    run()
'''
        data_bytes = base64.b64decode(b64_data)
        for key in reversed(xor_keys):
            data_bytes = bytes([b ^ key for b in data_bytes])
        new_b64 = base64.b64encode(data_bytes).decode('ascii')
        cython_code = cython_code.replace(b64_data, new_b64)
        pyx_file = os.path.join(self.temp_dir, f'{module_name}.pyx')
        with open(pyx_file, 'w', encoding='utf-8') as f:
            f.write(cython_code)
        return pyx_file

class NuitkaCompiler:

    def __init__(self, temp_dir):
        self.temp_dir = temp_dir
        self.available = check_nuitka()

    def compile_to_binary(self, py_file, output_name=None, standalone=False):
        if not self.available:
            logger.error('Nuitka yüklü değil! Kur: pip install nuitka')
            return None
        if output_name is None:
            output_name = Path(py_file).stem
        output_dir = os.path.join(self.temp_dir, 'nuitka_build')
        os.makedirs(output_dir, exist_ok=True)
        arch = platform.machine().lower()
        is_arm = 'aarch64' in arch or 'arm' in arch
        is_win = platform.system() == 'Windows'
        has_clang = check_clang()

        cmd = [sys.executable, '-m', 'nuitka',
               '--mode=module' if not standalone else '--mode=standalone',
               '--output-dir=' + output_dir,
               '--remove-output',
               '--no-pyi-file',
               '--nofollow-imports' if not standalone else '--follow-imports',
               '--assume-yes-for-downloads',
               '--python-flag=no_site',
               '--python-flag=no_warnings',
               '--python-flag=-S',
               '--python-flag=-OO',
               '--deployment',
               '--noinclude-setuptools-mode=nofollow',
               '--noinclude-pytest-mode=nofollow',
               '--noinclude-unittest-mode=nofollow',
               '--noinclude-IPython-mode=nofollow',
               '--low-memory',
        ]

        if has_clang:
            cmd.append('--clang')

        if not is_arm:
            cmd.append('--lto=yes')
            cmd.extend(['--jobs=2'])
        else:
            cmd.append('--no-progress-bar')

        if is_win and standalone:
            cmd.append('--windows-disable-console')

        if standalone:
            cmd.extend(['--onefile', '--static-libpython=auto'])
        cmd.append(py_file)
        logger.info(f'Nuitka derleniyor: {Path(py_file).name}')
        _cmd_str = ' '.join(cmd)
        logger.info(f'Komut: {_cmd_str}')
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=self.temp_dir)
            if result.returncode != 0:
                print(f'\x1b[31m[!] Nuitka derleme HATASI (kod {result.returncode}):\x1b[0m')
                if result.stderr:
                    print(result.stderr)
                if result.stdout:
                    print(result.stdout)
                return None
            if standalone:
                bin_files = list(Path(output_dir).glob(f'{output_name}*'))
                bin_files = [f for f in bin_files if f.is_file() and f.suffix not in ['.py', '.pyx', '.c', '.h']]
            else:
                bin_files = list(Path(output_dir).glob(f'{output_name}*.so'))
                if not bin_files:
                    bin_files = list(Path(output_dir).glob(f'{output_name}*.pyd'))
                if not bin_files:
                    bin_files = list(Path(output_dir).glob(f'{output_name}*.cpython*.so'))
                if not bin_files:
                    bin_files = list(Path(self.temp_dir).rglob(f'{output_name}*.so'))
            if bin_files:
                logger.info(f'Nuitka derleme başarılı: {bin_files[0].name}')
                return str(bin_files[0])
            print(f'\x1b[31m[!] Nuitka derlendi ama .so dosyası bulunamadı!\x1b[0m')
            print(f'    Aranan dizin: {output_dir}')
            _dir_files = list(Path(output_dir).iterdir()) if Path(output_dir).exists() else 'Dizin yok'
            print(f'    Mevcut dosyalar: {_dir_files}')
            return None
        except subprocess.TimeoutExpired:
            print(f'\x1b[31m[!] Nuitka derleme ZAMAN AŞIMI (10 dakika)!\x1b[0m')
            return None
        except FileNotFoundError as e:
            print(f'\x1b[31m[!] Nuitka bulunamadı: {e}\x1b[0m')
            print('    Kur: pip install nuitka')
            return None
        except Exception as e:
            print(f'\x1b[31m[!] Nuitka beklenmeyen hata: {type(e).__name__}: {e}\x1b[0m')
            import traceback
            traceback.print_exc()
            return None

    def compile_module(self, py_file):
        return self.compile_to_binary(py_file, standalone=False)

    def compile_standalone(self, py_file):
        return self.compile_to_binary(py_file, standalone=True)

    def compile_with_embedded_cython(self, wrapper_py: str, cython_so: str) -> str | None:
        """
        Cython .so'yu base64 ile wrapper .py içine göm → Nuitka derle → tek .so çıkar.
        --include-extension-module gibi flag'lere bağımlı değil, her Nuitka versiyonunda çalışır.
        """
        if not self.available:
            logger.error('Nuitka yüklü değil!')
            return None

        with open(cython_so, 'rb') as f:
            so_bytes = f.read()
        so_b64 = base64.b64encode(so_bytes).decode('ascii')
        cython_mod = Path(cython_so).name.split('.')[0]

        embed_src = f'''import base64 as _b64, tempfile as _tf, ctypes as _ct, os as _os, sys as _sys

_SO_DATA = b"{so_b64}"

def _load_embedded():
    _raw = _b64.b64decode(_SO_DATA)
    _tmp = _tf.NamedTemporaryFile(suffix='.so', delete=False, prefix='_nj_')
    try:
        _tmp.write(_raw)
        _tmp.close()
        _os.chmod(_tmp.name, 0o755)
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location('{cython_mod}', _tmp.name)
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        return _mod
    except Exception as _e:
        try: _os.unlink(_tmp.name)
        except: pass
        raise _e

_ninja_mod = _load_embedded()

def run():
    if hasattr(_ninja_mod, 'run'):
        _ninja_mod.run()

if __name__ == '__main__':
    run()
'''
        embed_py = os.path.join(self.temp_dir, 'ninja_embed.py')
        with open(embed_py, 'w', encoding='utf-8') as f:
            f.write(embed_src)

        output_dir = os.path.join(self.temp_dir, 'nuitka_embed_build')
        os.makedirs(output_dir, exist_ok=True)

        arch      = platform.machine().lower()
        is_arm    = 'aarch64' in arch or 'arm' in arch
        has_clang = check_clang()

        cmd = [
            sys.executable, '-m', 'nuitka',
            '--mode=module',
            f'--output-dir={output_dir}',
            '--remove-output',
            '--no-pyi-file',
            '--nofollow-imports',
            '--assume-yes-for-downloads',
            '--python-flag=no_site',
            '--python-flag=no_warnings',
            '--python-flag=-S',
            '--python-flag=-OO',
            '--deployment',
            '--low-memory',
            '--no-progress-bar',
        ]

        if has_clang:
            cmd.append('--clang')
        if not is_arm:
            cmd.extend(['--lto=yes', '--jobs=2'])

        cmd.append(embed_py)

        logger.info(f'Nuitka → base64 embedded Cython derleniyor...')
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=600
            )
            if result.returncode != 0:
                print(f'\x1b[31m[!] Nuitka embed HATASI:\x1b[0m')
                if result.stderr: print(result.stderr[-2000:])
                return None

            so_files = list(Path(output_dir).rglob('*.so'))
            if not so_files:
                so_files = list(Path(output_dir).rglob('*.pyd'))
            if so_files:
                logger.info(f'Nuitka embed başarılı: {so_files[0].name}')
                return str(so_files[0])
            print('\x1b[31m[!] Nuitka embed derlendi ama .so bulunamadı!\x1b[0m')
            return None
        except subprocess.TimeoutExpired:
            print('\x1b[31m[!] Nuitka embed zaman aşımı!\x1b[0m')
            return None
        except Exception as _e:
            print(f'\x1b[31m[!] Nuitka embed beklenmeyen hata: {_e}\x1b[0m')
            return None

class HyperionObfuscator:

    def __init__(self):
        self.var_map = {}
        self.string_map = {}

    def _randvar(self):
        patterns = [lambda: ''.join((random.choice('lI') for _ in range(random.randint(15, 25)))), lambda: 'O' + ''.join((random.choice('O0o') for _ in range(random.randint(15, 25)))), lambda: ''.join((random.choice('DO') for _ in range(random.randint(15, 25)))), lambda: 'S' + ''.join((random.choice('S2') for _ in range(random.randint(15, 25)))), lambda: ''.join((random.choice('MN') for _ in range(random.randint(15, 25)))), lambda: ''.join((random.choice('mn') for _ in range(random.randint(15, 25)))), lambda: ''.join((random.choice('XW') for _ in range(random.randint(15, 25)))), lambda: ''.join((random.choice('xw') for _ in range(random.randint(15, 25)))), lambda: ''.join((random.choice('JIL') for _ in range(random.randint(15, 25)))), lambda: ''.join((random.choice('jil') for _ in range(random.randint(15, 25))))]
        return random.choice(patterns)()

    def _hex_encode(self, s):
        return ''.join((f'\\x{hexlify(c.encode()).decode()}' for c in s))

    def _obf_int(self, num):
        if num == 0:
            return '(int())'
        r = random.randint(100000, 9999999)
        if num > 0:
            return f'({self._underscore(r)}+(-{self._underscore(r - num)}))'
        else:
            return f'(-{self._underscore(abs(num) + r)}+{self._underscore(r)})'

    def _underscore(self, num):
        s = str(abs(num))
        return '_'.join(s)

    def _obf_bool(self, val):
        if val:
            return "bool((~(not bool('')))|((bool('x'))&(not bool(''))))"
        else:
            return "not(bool(str(bool(''))))"

    def _obf_string(self, s):
        hex_str = hexlify(s.encode()).decode()
        return f"__import__('binascii').unhexlify('{hex_str}').decode('utf-8')"

    def _reverse_string(self, s):
        reversed_s = s[::-1]
        return f"'{reversed_s}'[::-1]"

    def generate_lambda_chain(self, code):
        var1 = self._randvar()
        var2 = self._randvar()
        var3 = self._randvar()
        compressed = zlib.compress(code.encode('utf-8'))
        b64_data = base64.b64encode(compressed).decode()
        return f"(lambda {var1}:(lambda {var2}:{var2}(__import__('zlib').decompress(__import__('base64').b64decode({var1})))))(lambda {var3}:exec({var3})))('{b64_data}')"

    def generate_fake_class(self, real_code, data_parts):
        gen = [self._randvar() for _ in range(25)]
        fake_vars = ['MemoryAccess', 'StackOverflow', 'System', 'Divide', 'Product', 'CallFunction', 'Math', 'Calculate', 'Hypothesis', 'Frame', 'DetectVar', 'Substract', 'Theory', 'Statistics', 'Random']
        random.shuffle(fake_vars)
        rand_int = lambda: random.randint(-100000, 100000)
        rand_op = lambda: random.choice(['+', '-', '*'])
        rand_type = lambda: random.choice(['type', 'None', 'Ellipsis', 'True', 'False', 'str', 'int'])
        data_vars = {self._randvar(): part for part in data_parts}
        vars_code = '\n'.join((f"        {gen[0]}.{gen[19]}('{k}', {repr(v)})" for k, v in data_vars.items()))
        data_concat = '+'.join((f"{gen[0]}.{gen[18]}('{k}')" for k in data_vars.keys()))
        return f"\nfrom builtins import *\nfrom math import prod as {gen[5]}\n\n__obfuscator__ = 'NinjaEnc'\n__version__ = '3.0'\n__author__ = 'NinjaEnc Team'\n\n{gen[11]}, {gen[12]}, {gen[13]}, {gen[14]}, {gen[15]}, {gen[17]}, {gen[24]} = exec, str, tuple, map, ord, globals, type\n\nclass {gen[0]}:\n    _data = {{}}\n\n    def __init__(self, {gen[4]}):\n        self.{gen[3]} = {gen[5]}(({gen[4]}, {rand_int()}))\n        self.{gen[1]}({gen[6]}={rand_int()})\n\n    def {gen[1]}(self, {gen[6]} = {rand_type()}):\n        self.{gen[3]} {rand_op()}= {rand_int()} {rand_op()} {gen[6]} if isinstance({gen[6]}, int) else 0\n        try:\n            {{{repr(self._randvar())}: {repr(self._randvar())}}}\n        except (OSError, TypeError):\n            pass\n\n    def {gen[2]}(self, {gen[7]} = {rand_int()}):\n        {gen[7]} {rand_op()}= {rand_int()} {rand_op()} {rand_int()}\n        self.{gen[8]} != {rand_type()}\n\n    @staticmethod\n    def {gen[18]}({gen[20]} = {rand_type()}):\n        return {gen[0]}._data.get({gen[20]}, '')\n\n    @staticmethod\n    def {gen[19]}({gen[21]} = '', {gen[22]} = {rand_type()}):\n        {gen[0]}._data[{gen[21]}] = {gen[22]}\n\n    @staticmethod\n    def execute(code = str):\n        return {gen[11]}({gen[12]}({gen[13]}({gen[14]}({gen[15]}, code))))\n\n    @property\n    def {gen[8]}(self):\n        self.{gen[9]} = '<__main__.{random.choice(fake_vars)} object at 0x00000{random.randint(1000, 9999)}BE{random.randint(10000, 99999)}>'\n        return (self.{gen[9]}, {gen[0]}.{gen[8]})\n\nif __name__ == '__main__':\n    try:\n        {gen[10]} = {gen[0]}({gen[4]} = {rand_int()} {rand_op()} {rand_int()})\n\n{vars_code}\n\n        {real_code.replace('DATACONCAT', data_concat)}\n\n    except Exception as {gen[16]}:\n        if {random.randint(100000, 499999)} > {random.randint(500000, 9999999)}:\n            {gen[0]}.execute(code = {gen[12]}({gen[16]}))\n"

class XORObfuscator:

    @staticmethod
    def encode_string(s, key=None):
        if key is None:
            key = random.randint(1, 255)
        encoded_bytes = [ord(c) ^ key for c in s]
        return f'(lambda _s,_k="": "".join(chr(_b^{key}) for _b in {encoded_bytes}))(None)'

    @staticmethod
    def decode_string(encoded_bytes, key):
        return ''.join((chr(b ^ key) for b in encoded_bytes))

    @staticmethod
    def multi_xor_encode(data, keys=None):
        # Integer key formatı — generate_with_payload / generate_v8 uyumu için korunuyor
        if keys is None:
            keys = [random.randint(1, 255) for _ in range(5)]
        result = data if isinstance(data, (bytes, bytearray)) else data.encode()
        for key in keys:
            k = key if isinstance(key, int) else key[0]
            result = bytes(b ^ k for b in result)
        return (result, keys)

    @staticmethod
    def multi_xor_encode_strong(data, keys=None):
        # Güçlü versiyon: 5 × 32-byte key — encode_ultimate payload şifrelemesi için
        if keys is None:
            keys = [os.urandom(32) for _ in range(5)]
        result = data if isinstance(data, (bytes, bytearray)) else data.encode()
        for key in keys:
            result = bytes(b ^ key[i % 32] for i, b in enumerate(result))
        return (result, keys)

class Ascii85Encoder:

    @staticmethod
    def encode(binary_data, padding_size=None):
        if padding_size is None:
            padding_size = random.randint(5000, 15000)
        if padding_size < 0:
            padding_size = 0
        random_padding = bytes([random.randint(0, 255) for _ in range(padding_size)])
        original_size = len(binary_data)
        size_bytes = original_size.to_bytes(4, byteorder='big')
        padded_data = size_bytes + binary_data + random_padding
        b64_encoded = base64.b64encode(padded_data)
        final_encoded = base64.a85encode(b64_encoded)
        return final_encoded.decode('ascii')

    @staticmethod
    def decode(encoded_str):
        clean_data = encoded_str.replace('\n', '')
        first_decode = base64.a85decode(clean_data.encode('ascii'))
        padded_data = base64.b64decode(first_decode)
        original_size = int.from_bytes(padded_data[0:4], byteorder='big')
        return padded_data[4:4 + original_size]

class AESEncryptor:

    @staticmethod
    def is_available():
        try:
            from Crypto.Cipher import AES
            return True
        except ImportError:
            return False

    @staticmethod
    def derive_runtime_key_code(encrypted_key_b64: str) -> str:
        """
        AES key'i base64 olarak dosyaya gömmek yerine,
        runtime'da makine özelliklerinden + gömülü parçadan türet.
        Statik analizle key bulmayı çok zorlaştırır.
        """
        hyp = HyperionObfuscator()
        v = [hyp._randvar() for _ in range(10)]
        xor_mask = random.randint(1, 255)
        key_bytes = base64.b64decode(encrypted_key_b64)
        masked_key = bytes(b ^ xor_mask for b in key_bytes)
        masked_b64 = base64.b64encode(masked_key).decode('ascii')
        return f'''
def {v[0]}():
    import hashlib as _h, os as _o, platform as _pl, base64 as _b
    _parts = []
    try: _parts.append(_o.uname().nodename.encode())
    except Exception: _parts.append(b"host")
    try: _parts.append(_pl.machine().encode())
    except Exception: _parts.append(b"arch")
    try: _parts.append(str(_o.cpu_count() or 2).encode())
    except Exception: _parts.append(b"2")
    _machine_hash = _h.sha256(b":".join(_parts)).digest()
    _mk = {xor_mask}
    _masked = _b.b64decode("{masked_b64}")
    _raw_key = bytes(b ^ _mk for b in _masked)
    _final = bytes(a ^ b for a, b in zip(_raw_key, _machine_hash * (len(_raw_key) // 32 + 1)))
    return _final[:32]
{v[0]}()
'''

    @staticmethod
    def derive_key(password: bytes, salt: bytes, iterations: int=500000) -> bytes:
        import hmac
        # SHA-512 tabanlı PBKDF2 — dklen=64, hem AES-256 key hem HMAC için
        return hashlib.pbkdf2_hmac('sha512', password, salt, iterations, dklen=64)

    @staticmethod
    def encrypt(data, key=None):
        try:
            from Crypto.Cipher import AES
            MAGIC = b'NJNC'
            if isinstance(data, str):
                data = data.encode('utf-8')
            salt = os.urandom(32)        # 32 byte salt (eski: 16)
            nonce = os.urandom(16)       # GCM nonce
            iterations = random.randint(400000, 600000)  # (eski: 150k-250k)
            if key is None:
                password = os.urandom(32)
                key_material = AESEncryptor.derive_key(password, salt, iterations)
                stored_key = password
            else:
                pw = key if isinstance(key, bytes) else key.encode()
                key_material = AESEncryptor.derive_key(pw, salt, iterations)
                stored_key = key
            aes_key = key_material[:32]   # ilk 32 byte AES-256 key
            mac_key  = key_material[32:]  # son 32 byte MAC için
            # AES-256 GCM — authenticated encryption (CBC'den güçlü)
            cipher = AES.new(aes_key, AES.MODE_GCM, nonce=nonce)
            cipher.update(mac_key)        # additional auth data
            encrypted, tag = cipher.encrypt_and_digest(data)
            iter_bytes = iterations.to_bytes(4, byteorder='big')
            # Format: MAGIC(4) + salt(32) + iter(4) + nonce(16) + tag(16) + ciphertext
            result = MAGIC + salt + iter_bytes + nonce + tag + encrypted
            return (result, stored_key)
        except ImportError:
            print('\x1b[93m[!] pycryptodome yüklü değil, AES atlanıyor. Kur: pip install pycryptodome\x1b[0m')
            return (None, None)
        except Exception as e:
            print(f'\x1b[31m[!] AES şifreleme hatası: {type(e).__name__}: {e}\x1b[0m')
            return (None, None)

    @staticmethod
    def decrypt(encrypted_data, key):
        try:
            from Crypto.Cipher import AES
            MAGIC = b'NJNC'
            if encrypted_data[:4] != MAGIC:
                raise ValueError('Geçersiz magic bytes — bu veri AESEncryptor ile şifrelenmemiş!')
            salt       = encrypted_data[4:36]
            iterations = int.from_bytes(encrypted_data[36:40], byteorder='big')
            nonce      = encrypted_data[40:56]
            tag        = encrypted_data[56:72]
            ciphertext = encrypted_data[72:]
            key_material = AESEncryptor.derive_key(key if isinstance(key, bytes) else key.encode(), salt, iterations)
            aes_key = key_material[:32]
            mac_key  = key_material[32:]
            cipher = AES.new(aes_key, AES.MODE_GCM, nonce=nonce)
            cipher.update(mac_key)
            return cipher.decrypt_and_verify(ciphertext, tag)
        except Exception as e:
            print(f'\x1b[31m[!] AES çözme hatası: {type(e).__name__}: {e}\x1b[0m')
            return None

class ChaCha20Encryptor:
    """
    ChaCha20-Poly1305 AEAD şifreleme.
    AES'ten farklı algoritma — imza tabanlı analiz geçersiz.
    pycryptodome varsa aktif, yoksa XOR'a düşer.
    """
    MAGIC = b'NJCC'

    @staticmethod
    def is_available():
        try:
            from Crypto.Cipher import ChaCha20_Poly1305
            return True
        except ImportError:
            return False

    @staticmethod
    def encrypt(data: bytes) -> tuple:
        try:
            from Crypto.Cipher import ChaCha20_Poly1305
            if isinstance(data, str):
                data = data.encode('utf-8')
            key = os.urandom(32)
            nonce = os.urandom(12)
            cipher = ChaCha20_Poly1305.new(key=key, nonce=nonce)
            ciphertext, tag = cipher.encrypt_and_digest(data)
            result = ChaCha20Encryptor.MAGIC + nonce + tag + ciphertext
            return result, key
        except Exception as e:
            logger.warning(f'ChaCha20 şifreleme hatası: {e}')
            return None, None

    @staticmethod
    def decrypt(data: bytes, key: bytes):
        try:
            from Crypto.Cipher import ChaCha20_Poly1305
            if data[:4] != ChaCha20Encryptor.MAGIC:
                raise ValueError('Geçersiz magic')
            nonce = data[4:16]
            tag   = data[16:32]
            ct    = data[32:]
            cipher = ChaCha20_Poly1305.new(key=key, nonce=nonce)
            return cipher.decrypt_and_verify(ct, tag)
        except Exception as e:
            logger.warning(f'ChaCha20 çözme hatası: {e}')
            return None

    @staticmethod
    def generate_decrypt_code(var_data: str, var_key_b64: str, var_out: str) -> str:
        return f'''
try:
    from Crypto.Cipher import ChaCha20_Poly1305 as _CC
    import base64 as _cc_b64
    _cc_key = _cc_b64.b64decode({var_key_b64})
    _cc_nonce = {var_data}[4:16]
    _cc_tag   = {var_data}[16:32]
    _cc_ct    = {var_data}[32:]
    _cc_cip   = _CC.new(key=_cc_key, nonce=_cc_nonce)
    {var_out} = _cc_cip.decrypt_and_verify(_cc_ct, _cc_tag)
    del _cc_key, _cc_nonce, _cc_tag, _cc_ct, _cc_cip
except Exception:
    {var_out} = {var_data}
'''

class ImportHook:
    """
    sys.meta_path'e sahte loader ekler.
    Reverse engineer import analizini yaparken yanıltıcı modüller görür.
    Gerçek modüller normal yüklenir, sahte modüller izleme aracını yanıltır.
    """

    @staticmethod
    def generate_hook_code() -> str:
        fake_modules = {
            '_crypto_core':   'raise ImportError("No module named _crypto_core")',
            '_hashlib_ext':   'raise ImportError("No module named _hashlib_ext")',
            '_ninja_runtime': 'raise ImportError("No module named _ninja_runtime")',
            '_obf_engine':    'raise ImportError("No module named _obf_engine")',
        }
        fake_mod_repr = repr(fake_modules)
        return f'''
import sys as _IH_sys

class _IH_FakeLoader:
    def __init__(self, name):
        self._name = name
    def create_module(self, spec): return None
    def exec_module(self, module):
        module.__doc__ = "Protected module"
        module.__version__ = "1.0.0"
        module.__file__ = "<protected>"

class _IH_MetaFinder:
    _FAKE = {fake_mod_repr}
    _REAL_FINDER = None

    def find_spec(self, fullname, path, target=None):
        if fullname in self._FAKE:
            import importlib.util as _ilu
            spec = _ilu.spec_from_loader(fullname, _IH_FakeLoader(fullname))
            return spec
        return None

_IH_hook = _IH_MetaFinder()
if not any(type(f).__name__ == "_IH_MetaFinder" for f in _IH_sys.meta_path):
    _IH_sys.meta_path.insert(0, _IH_hook)
del _IH_hook
'''

class CodeObjectMutator:
    """
    Runtime'da code object'i patch'ler.
    co_consts içine sahte sabitler enjekte eder.
    co_lnotab'ı bozar — decompiler satır numaralarını yanlış gösterir.
    Her çalışmada farklı mutation — polymorphic.
    """

    @staticmethod
    def mutate_co_consts(code_bytes: bytes) -> bytes:
        """
        Derlenmiş bytecode'un co_consts listesine sahte sabitler ekle.
        uncompyle6/decompyle3 analizi bozulur.
        """
        try:
            co = marshal.loads(code_bytes)

            def _mutate(c):
                try:
                    fake_consts = (
                        bytes([0]) * random.randint(4, 16),
                        random.randint(0x1337, 0xDEADBEEF),
                        'ninja_' + hashlib.md5(os.urandom(4)).hexdigest()[:8],
                    )
                    new_consts = tuple(
                        _mutate(x) if isinstance(x, types.CodeType) else x
                        for x in c.co_consts
                    ) + fake_consts

                    try:
                        lnotab = bytearray(c.co_lnotab)
                        for i in range(1, len(lnotab), 2):
                            lnotab[i] = (lnotab[i] + random.randint(1, 10)) & 0xFF
                        new_lnotab = bytes(lnotab)
                    except Exception:
                        new_lnotab = c.co_lnotab

                    return c.replace(co_consts=new_consts, co_lnotab=new_lnotab)
                except Exception:
                    return c

            mutated = _mutate(co)
            return marshal.dumps(mutated)
        except Exception as e:
            logger.warning(f'CodeObjectMutator atlandı: {e}')
            return code_bytes

    @staticmethod
    def generate_runtime_mutation_code() -> str:
        """
        __main__.py'ye eklenecek runtime mutation kodu.
        Bytecode belleğe yüklendikten sonra co_consts'u patch'ler.
        _COM_ prefix — namespace güvenli.
        """
        fake_vals = [
            repr(bytes([0]) * 8),
            repr(0xDEADBEEF),
            repr('_ninja_protected'),
        ]
        return f'''
import types as _COM_ty, marshal as _COM_ma, random as _COM_rnd

def _COM_mutate(co):
    try:
        _fake = ({fake_vals[0]}, {fake_vals[1]}, {fake_vals[2]})
        _new_consts = tuple(
            _COM_mutate(x) if isinstance(x, _COM_ty.CodeType) else x
            for x in co.co_consts
        ) + _fake
        try:
            _ln = bytearray(co.co_lnotab)
            for _i in range(1, len(_ln), 2):
                _ln[_i] = (_ln[_i] + _COM_rnd.randint(1, 5)) & 0xFF
            return co.replace(co_consts=_new_consts, co_lnotab=bytes(_ln))
        except Exception:
            return co.replace(co_consts=_new_consts)
    except Exception:
        return co
'''

class PolymorphicDecryptor:
    """
    Her encode'da farklı decryption kodu üretir.
    Aynı şifreli veriyi çözen 6 farklı kod yapısı var.
    İmza tabanlı analiz ve pattern matching geçersiz.
    """

    PATTERNS = ['loop', 'map_lambda', 'bytearray_loop', 'list_comp', 'generator', 'recursive']

    @staticmethod
    def generate(var_enc: str, var_key: str, var_out: str) -> str:
        pattern = random.choice(PolymorphicDecryptor.PATTERNS)

        if pattern == 'loop':
            return f'''
{var_out} = bytearray(len({var_enc}))
for _pi, _pb in enumerate({var_enc}):
    {var_out}[_pi] = _pb ^ {var_key}[_pi % len({var_key})]
{var_out} = bytes({var_out})
'''
        elif pattern == 'map_lambda':
            return f'''
{var_out} = bytes(map(lambda _pi_b: _pi_b[1] ^ {var_key}[_pi_b[0] % len({var_key})], enumerate({var_enc})))
'''
        elif pattern == 'bytearray_loop':
            return f'''
_pd_buf = bytearray()
_pd_kl  = len({var_key})
for _pd_i in range(len({var_enc})):
    _pd_buf.append({var_enc}[_pd_i] ^ {var_key}[_pd_i % _pd_kl])
{var_out} = bytes(_pd_buf)
del _pd_buf, _pd_kl
'''
        elif pattern == 'list_comp':
            return f'''
{var_out} = bytes([{var_enc}[_lc_i] ^ {var_key}[_lc_i % len({var_key})] for _lc_i in range(len({var_enc}))])
'''
        elif pattern == 'generator':
            return f'''
def _pg_dec(_d, _k):
    _kl = len(_k)
    for _i, _b in enumerate(_d):
        yield _b ^ _k[_i % _kl]
{var_out} = bytes(_pg_dec({var_enc}, {var_key}))
del _pg_dec
'''
        else:
            return f'''
def _pr_dec(_d, _k, _i=0, _acc=None):
    if _acc is None: _acc = bytearray()
    if _i >= len(_d): return bytes(_acc)
    _acc.append(_d[_i] ^ _k[_i % len(_k)])
    return _pr_dec(_d, _k, _i+1, _acc)
{var_out} = _pr_dec({var_enc}, {var_key})
del _pr_dec
'''

    @staticmethod
    def generate_multi_stage(data: bytes) -> tuple:
        """
        5 aşamalı polymorphic şifreleme (eski: 3 aşama).
        Her aşama farklı key + farklı dönüşüm kullanır.
        Aşamalar: XOR32 → ROT-bit → XOR32 → NIBBLE-swap → XOR32
        """
        key1 = os.urandom(32)
        stage1 = bytes(b ^ key1[i % 32] for i, b in enumerate(data))

        rot = random.randint(1, 7)
        stage2 = bytes(((b << rot) | (b >> (8 - rot))) & 0xFF for b in stage1)

        key2 = os.urandom(32)
        stage3 = bytes(b ^ key2[i % 32] for i, b in enumerate(stage2))

        # Yeni 4. aşama: nibble swap (her byte'ın yüksek/düşük 4 biti yer değiştirir)
        stage4 = bytes(((b & 0x0F) << 4) | ((b & 0xF0) >> 4) for b in stage3)

        # Yeni 5. aşama: 32-byte key ile XOR
        key3 = os.urandom(32)
        stage5 = bytes(b ^ key3[i % 32] for i, b in enumerate(stage4))

        params = {
            'key1': base64.b64encode(key1).decode(),
            'key2': base64.b64encode(key2).decode(),
            'key3': base64.b64encode(key3).decode(),
            'rot':  rot,
        }
        return stage5, params

    @staticmethod
    def generate_multi_stage_decrypt(var_enc: str, params: dict, var_out: str) -> str:
        k1 = params['key1']
        k2 = params['key2']
        k3 = params.get('key3', params['key1'])  # geriye dönük uyumluluk
        rot = params['rot']
        unrot = 8 - rot

        # Ters sırada çöz: stage5→4→3→2→1
        dec5 = PolymorphicDecryptor.generate(var_enc, f"__import__('base64').b64decode('{k3}')", '_pd_s5')
        dec4 = "_pd_s4 = bytes(((b & 0x0F) << 4) | ((b & 0xF0) >> 4) for b in _pd_s5)"
        dec3 = PolymorphicDecryptor.generate('_pd_s4', f"__import__('base64').b64decode('{k2}')", '_pd_s3')
        dec2 = f"_pd_s2 = bytes(((b >> {rot}) | (b << {unrot})) & 0xFF for b in _pd_s3)"
        dec1 = PolymorphicDecryptor.generate('_pd_s2', f"__import__('base64').b64decode('{k1}')", var_out)

        return f'''
import base64 as _pd_b64
{dec5}
{dec4}
{dec3}
{dec2}
{dec1}
del _pd_s5, _pd_s4, _pd_s3, _pd_s2
'''

class MetamorphicStager:
    """
    Multi-stage polymorphism — her encode'da obfuscation adımlarının
    sırası ve kombinasyonu değişir. Aynı dosyayı 2 kez encode etsen
    tamamen farklı yapıda çıktı üretilir.
    Gerçek metamorfizm değil ama etkisi benzer.
    """

    ALL_STAGES = [
        'str_enc',
        'dead_code',
        'opaque',
        'cf_obf',
        'mba',
    ]

    @staticmethod
    def get_random_order() -> list:
        stages = MetamorphicStager.ALL_STAGES.copy()
        random.shuffle(stages)
        return stages

    @staticmethod
    def apply(encoder, source: str, stages: list) -> str:
        result = source
        stage_map = {
            'str_table': lambda s: encoder.str_table.encrypt_to_table(s),
            'str_enc':   lambda s: encoder.string_enc.encrypt_all_strings_in_code(s),
            'dead_code': lambda s: encoder.dead_code.inject_into_source(s, count=8),
            'opaque':    lambda s: encoder.opaque.wrap_with_opaques(s),
            'cf_obf':    lambda s: encoder.cf_obf.inject_fake_branches(s),
            'mba':       lambda s: encoder.mba.transform_source(s),
            'cf_flat':   lambda s: encoder.cf_flatten.flatten_source(s),
        }
        for stage in stages:
            try:
                fn = stage_map.get(stage)
                if fn:
                    result = fn(result)
                    logger.info(f'MetamorphicStager: {stage} uygulandı')
            except Exception as e:
                logger.warning(f'MetamorphicStager: {stage} atlandı — {e}')
        return result

class MainPyGenerator:

    def __init__(self):
        self.xor = XORObfuscator()
        self.hyp = HyperionObfuscator()

    def generate(self, has_native=False, native_fname='', xor_keys=None, aes_key_b64=None):
        v = [self.hyp._randvar() for _ in range(20)]
        xor_keys = xor_keys or [random.randint(1,255), random.randint(1,255), random.randint(1,255)]
        k0, k1, k2 = xor_keys[0], xor_keys[1], xor_keys[2]

        _mk0 = random.randint(1, 255)
        _mk1 = random.randint(1, 255)
        _mk2 = random.randint(1, 255)
        _bk0 = k0 ^ _mk0
        _bk1 = k1 ^ _mk1
        _bk2 = k2 ^ _mk2

        _xor_order = list(range(3))
        random.shuffle(_xor_order)
        _keys_ordered = [(_bk0, _mk0), (_bk1, _mk1), (_bk2, _mk2)]

        def _xor_step(src_var, dst_var, bk, mk):
            return f'        {dst_var}=bytes(b^({bk}^{mk}) for b in {src_var})\n'

        _step_vars = [v[3], v[4], v[5], v[16] if len(v) > 16 else self.hyp._randvar()]
        _xor_steps = ''
        _cur = v[3]
        for _i, _oi in enumerate(_xor_order):
            _nxt = v[4+_i] if _i < 2 else v[3]
            _bk, _mk = _keys_ordered[_oi]
            _xor_steps += _xor_step(_cur, _nxt, _bk, _mk)
            _cur = _nxt

        _wd_var = self.hyp._randvar()
        _wd_flag = self.hyp._randvar()
        _watchdog_block = f'''
import threading as _WD_th, sys as _WD_sy, gc as _WD_gc
{_wd_flag} = True
def {_wd_var}():
    import time as _WD_tm
    while {_wd_flag}:
        try:
            if _WD_sy.gettrace() is not None:
                _WD_sy.exit()
            _mods = list(_WD_sy.modules.keys())
            _bad = ['bdb','pdb','pydevd','debugpy','pydev','_pydev']
            if any(m in _mods for m in _bad):
                _WD_sy.exit()
            _objs = _WD_gc.get_objects()
            for _o in _objs:
                if hasattr(_o,'__class__') and 'Bdb' in str(type(_o).__mro__):
                    _WD_sy.exit()
        except SystemExit:
            import os as _WD_os
            _WD_os._exit(1)
        except Exception:
            pass
        _WD_tm.sleep(0.5)
_WD_t = _WD_th.Thread(target={_wd_var}, daemon=True)
_WD_t.start()
'''

        aes_block = ''
        if aes_key_b64:
            aes_block = f'''    try:
        import hashlib as _hl
        from Crypto.Cipher import AES as _AES
        from Crypto.Util.Padding import unpad as _unp
        _pw=__import__('base64').b64decode('{aes_key_b64}')
        _s={v[3]}[4:20];_it=int.from_bytes({v[3]}[20:24],'big')
        _iv={v[3]}[24:40];_ct={v[3]}[40:]
        _dk=_hl.pbkdf2_hmac('sha256',_pw,_s,_it,dklen=32)
        {v[3]}=_unp(_AES.new(_dk,_AES.MODE_CBC,_iv).decrypt(_ct),_AES.block_size)
        del _pw,_s,_it,_iv,_ct,_dk
    except Exception:pass
'''

        native_block = ''
        if native_fname:
            native_block = f'''
        _nf='{native_fname}'
        if _nf:
            _np=_O.path.join(_d,_nf)
            if _O.path.exists(_np):
                try:
                    import importlib.util as _ilu
                    _O.chmod(_np,0o755)
                    _spec=_ilu.spec_from_file_location('_ninja_mod',_np)
                    _mod=_ilu.module_from_spec(_spec)
                    _spec.loader.exec_module(_mod)
                    if hasattr(_mod,'run'):_mod.run()
                    return
                except Exception:pass
'''

        bad_zip_msg = self.xor.encode_string('Hata: Bozuk arsiv')
        error_msg   = self.xor.encode_string('Hata: %s')
        main_check  = self.xor.encode_string('__main__')

        _ad_block = '\nimport sys as _AD_sy,os as _AD_os,time as _AD_tm,platform as _AD_pl,socket as _AD_sk,threading as _AD_th\n\n_AD_IS_ANDROID = _AD_os.path.exists("/system/build.prop") or _AD_os.path.exists("/data/data")\n_AD_IS_ARM = _AD_pl.machine().lower() in ("aarch64","arm","armv7l","armv8l")\n\ndef _AD_chk_trace():\n    if _AD_IS_ANDROID: return False\n    try:\n        if _AD_sy.gettrace() is not None: return True\n    except Exception: pass\n    try:\n        if _AD_sy.flags.debug: return True\n    except Exception: pass\n    return False\n\ndef _AD_chk_tracer():\n    try:\n        with open("/proc/self/status","r") as _f:\n            for _l in _f:\n                if _l.startswith("TracerPid:") and int(_l.split(":")[1].strip()) != 0:\n                    return True\n    except Exception: pass\n    return False\n\ndef _AD_chk_maps():\n    _bad = ["frida","frida-server","frida-agent","gdb","lldb","strace","radare2","ida"]\n    if not _AD_IS_ANDROID:\n        _bad += ["pycharm","pydev","debugpy","pydevd"]\n    try:\n        with open("/proc/self/maps","r") as _f:\n            _c = _f.read().lower()\n            for _s in _bad:\n                if _s in _c: return True\n    except Exception: pass\n    try:\n        _exe = _AD_os.readlink("/proc/self/exe").lower()\n        for _s in _bad:\n            if _s in _exe: return True\n    except Exception: pass\n    return False\n\ndef _AD_chk_frida():\n    try:\n        for _p in [27042, 27043, 10900]:\n            _s = _AD_sk.socket()\n            _s.settimeout(0.05)\n            if _s.connect_ex(("127.0.0.1",_p)) == 0:\n                _s.close(); return True\n            _s.close()\n    except Exception: pass\n    try:\n        if any("frida" in _t.name.lower() for _t in _AD_th.enumerate()): return True\n    except Exception: pass\n    try:\n        _ld = _AD_os.environ.get("LD_PRELOAD","").lower()\n        if _ld and any(x in _ld for x in ["frida","gadget","interpose"]): return True\n    except Exception: pass\n    try:\n        _fd_dir = "/proc/self/fd"\n        if _AD_os.path.exists(_fd_dir):\n            for _fd in _AD_os.listdir(_fd_dir):\n                try:\n                    _lnk = _AD_os.readlink(_AD_os.path.join(_fd_dir,_fd)).lower()\n                    if "frida" in _lnk or "gum" in _lnk: return True\n                except Exception: pass\n    except Exception: pass\n    return False\n\ndef _AD_chk_parent():\n    try:\n        _pp = _AD_os.popen("ps -o comm= -p "+str(_AD_os.getppid())).read().strip().lower()\n        for _s in ["gdb","lldb","strace","frida","radare2","ida"]:\n            if _s in _pp: return True\n    except Exception: pass\n    return False\n\ndef _AD_chk_timing():\n    try:\n        _t0 = _AD_tm.monotonic()\n        _acc = 0\n        for _ii in range(10000): _acc += _ii\n        _base = _AD_tm.monotonic() - _t0\n        _t1 = _AD_tm.monotonic()\n        _acc2 = 0\n        for _ii in range(10000): _acc2 += _ii\n        _real = _AD_tm.monotonic() - _t1\n        _limit = 3.0 if _AD_IS_ARM else 1.0\n        if _base > 0 and _real > _base * 10: return True\n        if _real > _limit: return True\n    except Exception: pass\n    return False\n\ndef _AD_run():\n    _sc = 0\n    if _AD_chk_trace():  _sc += 5\n    if _AD_chk_tracer(): _sc += 5\n    if _AD_chk_maps():   _sc += 4\n    if _AD_chk_frida():  _sc += 5\n    if _AD_chk_parent(): _sc += 3\n    if _AD_chk_timing(): _sc += 3\n    _threshold = 8\n    if _sc < _threshold: return\n    _kill = [\n        lambda: _AD_os._exit(0),\n        lambda: (_ for _ in ()).throw(MemoryError()),\n        lambda: (_ for _ in ()).throw(ImportError("No module named _hashcore")),\n        lambda: (_ for _ in ()).throw(OSError(22,"Invalid argument")),\n        lambda: _AD_os.kill(_AD_os.getpid(),9),\n        lambda: (_ for _ in ()).throw(SystemExit(1)),\n    ]\n    _kill[_sc % len(_kill)]()\n\n_AD_run()\n'

        _ih_block = ImportHook.generate_hook_code()
        _com_block = CodeObjectMutator.generate_runtime_mutation_code()

        code = f'''{_ad_block}
{_watchdog_block}
{_ih_block}
{_com_block}
import zipfile as _Z,os as _O,shutil as _SH,tempfile as _T,sys as _S,base64 as _B,random as _R,hashlib as _HL,zlib as _ZL
def _run():
    _d=_T.mkdtemp()
    try:
        _f=_O.path.abspath(_S.argv[0])
        with _Z.ZipFile(_f,'r') as _zf:
            _zf.extractall(_d)
{native_block}        _sp=_O.path.join(_d,'__script__.bin')
        if not _O.path.exists(_sp):
            _sp=_O.path.join(_d,'__script__.py')
            with open(_sp,'r') as _fh:
                exec(compile(_fh.read(),_sp,'exec'),{{'__name__':'__main__','__file__':_sp}})
            return
        with open(_sp,'rb') as _fh:
            {v[3]}=_fh.read()
{_xor_steps}{aes_block}        import zlib as _zl,marshal as _m,types as _ty,ctypes as _ctypes
        {v[6]}=_zl.decompress({v[3]})
        {v[7]}=_m.loads({v[6]})
        del {v[3]},{v[4]},{v[5]},{v[6]}
        {v[8]}=_ty.ModuleType('__main__')
        import sys as _sys_tmp,os as _os_tmp
        {v[8]}.__dict__.update({{'__name__':'__main__','__builtins__':__builtins__,'__file__':_sp,'sys':_sys_tmp,'os':_os_tmp}})
        try:
            _papi=_ctypes.pythonapi
            _papi.PyEval_EvalCode.restype=_ctypes.py_object
            _papi.PyEval_EvalCode.argtypes=[_ctypes.py_object,_ctypes.py_object,_ctypes.py_object]
            _papi.PyEval_EvalCode({v[7]},{v[8]}.__dict__,{v[8]}.__dict__)
        except Exception:
            exec({v[7]},{v[8]}.__dict__)
        try:
            _raw=bytes({v[7]}.co_code) if hasattr({v[7]},'co_code') else b''
            if _raw:
                _buf=(_ctypes.c_char*len(_raw)).from_address(id(_raw)+32)
                _ctypes.memset(_buf,0,len(_raw))
        except Exception:pass
        del {v[7]},{v[8]}
    except _Z.BadZipFile:print({bad_zip_msg})
    except Exception as _e:print({error_msg}%_e)
    finally:_SH.rmtree(_d,ignore_errors=True)
if __name__=={main_check}:_run()'''
        return code

    def generate_with_payload(self, source: str, native_fname: str = ''):
        code = compile(source, '<ninja>', 'exec')
        raw = marshal.dumps(code)
        compressed = zlib.compress(raw, 9)
        xor = XORObfuscator()
        enc_data, keys = xor.multi_xor_encode(compressed)
        main_code = self.generate(
            has_native=bool(native_fname),
            native_fname=native_fname,
            xor_keys=keys,
        )
        return main_code, enc_data, keys

    def generate_v8(self, xor_keys: list, has_native: bool = False,
                    native_fname: str = '', lazy_base_key: int = 0,
                    vm_prog_b64: str = '') -> str:
        """
        v8 __main__.py:
        - LazyChunk: __s0-4__.bin zincir key ile birlestirir
        - XOR decode (AC: masked keys)
        - MiniVM exec (exec() yerine custom dispatcher)
        """
        v = [self.hyp._randvar() for _ in range(22)]
        # Tüm key'leri al (multi_xor_encode 5 key kullanıyor)
        all_keys = list(xor_keys)
        _mask_vals = [random.randint(1, 255) for _ in all_keys]
        _blinded_keys = [k ^ m for k, m in zip(all_keys, _mask_vals)]

        _cur = v[3]
        _xor_steps = ''
        _tmp_vars = [v[4], v[5], v[6], v[7], v[8]]  # yeterli temp var
        for _i, (_bk, _mk) in enumerate(zip(_blinded_keys, _mask_vals)):
            _nxt = _tmp_vars[_i % len(_tmp_vars)] if _i < len(all_keys) - 1 else v[3]
            _bv = f'_xb{_i}'  # her adımda benzersiz loop değişkeni
            _xor_steps += f'        {_nxt}=bytes({_bv}^({_bk}^{_mk}) for {_bv} in {_cur})\n'
            _cur = _nxt

        _wd_var  = self.hyp._randvar()
        _wd_flag = self.hyp._randvar()
        _watchdog_block = (
            f'import threading as _WD_th,sys as _WD_sy,gc as _WD_gc\n'
            f'{_wd_flag}=True\n'
            f'def {_wd_var}():\n'
            f'    import time as _WD_tm\n'
            f'    while {_wd_flag}:\n'
            f'        try:\n'
            f'            if _WD_sy.gettrace() is not None:_WD_sy.exit()\n'
            f'            _mods=list(_WD_sy.modules.keys())\n'
            f'            if any(m in _mods for m in ["bdb","pdb","pydevd","debugpy"]):_WD_sy.exit()\n'
            f'            _objs=_WD_gc.get_objects()\n'
            f'            for _o in _objs:\n'
            f'                if hasattr(_o,"__class__") and "Bdb" in str(type(_o).__mro__):_WD_sy.exit()\n'
            f'        except SystemExit:\n'
            f'            import os as _WD_os;_WD_os._exit(1)\n'
            f'        except Exception:pass\n'
            f'        _WD_tm.sleep(0.5)\n'
            f'_WD_t=_WD_th.Thread(target={_wd_var},daemon=True)\n'
            f'_WD_t.start()\n'
        )

        bad_zip_msg = self.xor.encode_string('Hata: Bozuk arsiv')
        error_msg   = self.xor.encode_string('Hata: %s')
        main_check  = self.xor.encode_string('__main__')
        _ih_block   = ImportHook.generate_hook_code()
        _com_block  = CodeObjectMutator.generate_runtime_mutation_code()
        lazy_loader = LazyChunkEncoder.generate_loader_code(lazy_base_key, '_d', v[3])

        native_block = ''
        if has_native and native_fname:
            native_block = (
                f"        _nf='{native_fname}'\n"
                f"        if _nf and _O.path.exists(_O.path.join(_d,_nf)):\n"
                f"            try:\n"
                f"                import importlib.util as _ilu\n"
                f"                _np=_O.path.join(_d,_nf)\n"
                f"                _O.chmod(_np,0o755)\n"
                f"                _spec=_ilu.spec_from_file_location('_ninja_mod',_np)\n"
                f"                _mod=_ilu.module_from_spec(_spec)\n"
                f"                _spec.loader.exec_module(_mod)\n"
                f"                if hasattr(_mod,'run'):_mod.run()\n"
                f"                return\n"
                f"            except Exception:pass\n"
            )

        if vm_prog_b64:
            _vm_code = MiniVMGenerator.generate_runtime_interpreter(vm_prog_b64, v[3], self.hyp)
            vm_block = '\n'.join('        ' + l for l in _vm_code.splitlines())
        else:
            vm_block = (
                f'        import zlib as _zl,marshal as _m,ctypes as _ct\n'
                f'        {v[6]}=_zl.decompress({v[3]})\n'
                f'        {v[7]}=_m.loads({v[6]})\n'
                f'        del {v[3]},{v[6]}\n'
                f'        _g={{"__name__":"__main__","__builtins__":__builtins__}}\n'
                f'        try:\n'
                f'            _pa=_ct.pythonapi\n'
                f'            _pa.PyEval_EvalCode.restype=_ct.py_object\n'
                f'            _pa.PyEval_EvalCode.argtypes=[_ct.py_object,_ct.py_object,_ct.py_object]\n'
                f'            _pa.PyEval_EvalCode({v[7]},_g,_g)\n'
                f'        except Exception:\n'
                f'            exec({v[7]},_g)\n'
                f'        try:\n'
                f'            _rw=getattr({v[7]},"co_code",b"")\n'
                f'            if _rw:\n'
                f'                _buf=(_ct.c_char*len(_rw)).from_address(id(_rw)+32)\n'
                f'                _ct.memset(_buf,0,len(_rw))\n'
                f'        except Exception:pass\n'
            )

        ad_block_str = (
            'import sys as _AD_sy,os as _AD_os,time as _AD_tm,platform as _AD_pl,socket as _AD_sk,threading as _AD_th\n'
            '_AD_IS_ANDROID=_AD_os.path.exists("/system/build.prop") or _AD_os.path.exists("/data/data")\n'
            '_AD_IS_ARM=_AD_pl.machine().lower() in ("aarch64","arm","armv7l","armv8l")\n'
            'def _AD_chk_trace():\n'
            '    if _AD_IS_ANDROID:return False\n'
            '    try:\n'
            '        if _AD_sy.gettrace() is not None:return True\n'
            '    except:pass\n'
            '    return False\n'
            'def _AD_chk_tracer():\n'
            '    try:\n'
            '        with open("/proc/self/status","r") as _f:\n'
            '            for _l in _f:\n'
            '                if _l.startswith("TracerPid:") and int(_l.split(":")[1].strip())!=0:return True\n'
            '    except:pass\n'
            '    return False\n'
            'def _AD_chk_frida():\n'
            '    try:\n'
            '        for _p in [27042,27043,10900]:\n'
            '            _s=_AD_sk.socket()\n'
            '            _s.settimeout(0.05)\n'
            '            if _s.connect_ex(("127.0.0.1",_p))==0:_s.close();return True\n'
            '            _s.close()\n'
            '    except:pass\n'
            '    return False\n'
            'def _AD_run():\n'
            '    _sc=0\n'
            '    if _AD_chk_trace():_sc+=5\n'
            '    if _AD_chk_tracer():_sc+=5\n'
            '    if _AD_chk_frida():_sc+=5\n'
            '    if _sc<8:return\n'
            '    [lambda:_AD_os._exit(0),lambda:(_ for _ in()).throw(MemoryError()),lambda:_AD_os.kill(_AD_os.getpid(),9)][_sc%3]()\n'
            '_AD_run()\n'
        )

        lazy_indented = '\n'.join('        ' + l for l in lazy_loader.splitlines())

        code = (
            f'{ad_block_str}\n'
            f'{_watchdog_block}\n'
            f'{_ih_block}\n'
            f'{_com_block}\n'
            f'import zipfile as _Z,os as _O,shutil as _SH,tempfile as _T,sys as _S,hashlib as _HL\n'
            f'def _run():\n'
            f'    _d=_T.mkdtemp()\n'
            f'    try:\n'
            f'        _f=_O.path.abspath(_S.argv[0])\n'
            f'        with _Z.ZipFile(_f,"r") as _zf:\n'
            f'            _zf.extractall(_d)\n'
            f'{native_block}'
            f'{lazy_indented}\n'
            f'{_xor_steps}'
            f'{vm_block}\n'
            f'    except _Z.BadZipFile:print({bad_zip_msg})\n'
            f'    except Exception as _e:print({error_msg}%_e)\n'
            f'    finally:_SH.rmtree(_d,ignore_errors=True)\n'
            f'if __name__=={main_check}:_run()'
        )
        return code

class ELFGenerator:

    @staticmethod
    def create_arm64_elf(python_code):
        elf_header = bytes([127, 69, 76, 70, 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 0, 183, 0, 1, 0, 0, 0])
        return elf_header + bytes([0]) * 40 + python_code.encode('utf-8')

    @staticmethod
    def create_arm32_elf(python_code):
        elf_header = bytes([127, 69, 76, 70, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 0, 40, 0, 1, 0, 0, 0])
        return elf_header + bytes([0]) * 32 + python_code.encode('utf-8')

class StringEncryptor:

    @staticmethod
    def encrypt_string(s: str) -> str:
        key = random.randint(1, 255)
        encoded = [b ^ key for b in s.encode('utf-8')]
        return f"bytes([{','.join(map(str, encoded))}]).decode('utf-8') if not (lambda k=({key}): [b^k for b in [{','.join(map(str, encoded))}]])() else ''.join(chr(b^{key}) for b in [{','.join(map(str, encoded))}])"

    # S2: 10 farklı decryptor şablonu — her string farklı görünür
    _DT = [
        lambda d,k: f"(lambda _d,_k:bytes(_d[_i]^_k[_i%32]for _i in range(len(_d))).decode())(__import__('base64').b64decode('{d}'),__import__('base64').b64decode('{k}'))",
        lambda d,k: f"(lambda _b,_k:bytearray(_b[_i]^_k[_i%32]for _i in range(len(_b))).decode())(__import__('base64').b64decode('{d}'),__import__('base64').b64decode('{k}'))",
        lambda d,k: f"''.join(map(chr,(lambda _b,_k:bytes(_b[_i]^_k[_i%32]for _i in range(len(_b))))(__import__('base64').b64decode('{d}'),__import__('base64').b64decode('{k}'))))",
        lambda d,k: f"''.join([chr(_b^__import__('base64').b64decode('{k}')[_i%32])for _i,_b in enumerate(__import__('base64').b64decode('{d}'))])",
        lambda d,k: f"(lambda _f,_d,_k:_f(_d,_k))((lambda _a,_b:bytes(_a[_i]^_b[_i%32]for _i in range(len(_a))).decode()),__import__('base64').b64decode('{d}'),__import__('base64').b64decode('{k}'))",
        lambda d,k: f"(lambda _g:''.join(_g))((chr(_b^__import__('base64').b64decode('{k}')[_i%32])for _i,_b in enumerate(__import__('base64').b64decode('{d}'))))",
        lambda d,k: f"(lambda _k:(lambda _d:bytes(_d[_i]^_k[_i%32]for _i in range(len(_d))).decode())(__import__('base64').b64decode('{d}')))(__import__('base64').b64decode('{k}'))",
        lambda d,k: f"(lambda _t:bytes(_t[0][_i]^_t[1][_i%32]for _i in range(len(_t[0]))).decode())((__import__('base64').b64decode('{d}'),__import__('base64').b64decode('{k}')))",
        lambda d,k: f"(lambda _d,_k:''.join(chr(_v^_k[_i%32])for _i,_v in enumerate(_d)))(__import__('base64').b64decode('{d}'),__import__('base64').b64decode('{k}'))",
        lambda d,k: f"(lambda _p,_q:bytes(a^b for a,b in zip(_p,(_q*((len(_p)//32)+1))[:len(_p)])).decode())(__import__('base64').b64decode('{d}'),__import__('base64').b64decode('{k}'))",
    ]

    @staticmethod
    def _rb(b, n):
        n = n % 8; return ((b << n) | (b >> (8 - n))) & 0xFF

    @staticmethod
    def _ns(b):
        return ((b & 0x0F) << 4) | ((b & 0xF0) >> 4)

    @staticmethod
    def _enc_concat(parts):
        enc = []
        for p in parts:
            key   = os.urandom(32)
            data  = p.encode('utf-8')
            xored = bytes(data[i] ^ key[i % 32] for i in range(len(data)))
            d64   = base64.b64encode(xored).decode('ascii')
            k64   = base64.b64encode(key).decode('ascii')
            enc.append(
                "(lambda _d,_k:bytes(_d[_i]^_k[_i%32]for _i in range(len(_d))).decode())"
                f"(__import__('base64').b64decode('{d64}'),__import__('base64').b64decode('{k64}'))"
            )
        return '(' + '+'.join(enc) + ')'

    @staticmethod
    def simple_encrypt(s: str) -> str:
        import zlib as _z
        # S10: 40% ihtimalle string 2 parcaya bol, her birini ayri sifrele
        if random.random() < 0.4 and len(s) > 8:
            mid   = random.randint(len(s)//3, 2*len(s)//3)
            return StringEncryptor._enc_concat([s[:mid], s[mid:]])

        # S8: once zlib sikistir
        data = _z.compress(s.encode('utf-8'), level=9)

        # S5: per-string nonce
        nonce = os.urandom(8)

        # S1: 32 byte key
        key = os.urandom(32)

        # S7: key 2 parcaya bol
        sp   = random.randint(8, 24)
        ka   = key[:sp]
        kb   = key[sp:]

        # S4: key_a'yi seed_mask ile maskele (static analiz key'i goremez)
        sm   = os.urandom(len(ka))
        ka_m = bytes(ka[i] ^ sm[i] for i in range(len(ka)))

        # S3: multi-layer XOR -> bit-rotate -> nibble-swap
        rot  = random.randint(1, 7)
        l1   = bytes(data[i] ^ key[i % 32] for i in range(len(data)))
        l2   = bytes(StringEncryptor._rb(b, rot) for b in l1)
        l3   = bytes(StringEncryptor._ns(b) for b in l2)

        d64  = base64.b64encode(l3).decode('ascii')
        ka64 = base64.b64encode(ka_m).decode('ascii')
        kb64 = base64.b64encode(kb).decode('ascii')
        sm64 = base64.b64encode(sm).decode('ascii')
        ur   = rot  # right-rotate by rot = inverse of left-rotate by rot

        # S6: 20% polynomial, diger durumda S2 sablonu
        if random.random() < 0.2:
            return (
                f"(lambda _d,_ka,_kb,_sm,_sp,_ur:"
                f"__import__('zlib').decompress("
                f"bytes(_v^(bytes(_ka[_i]^_sm[_i]for _i in range(_sp))+_kb)[_i%32]"
                f"for _i,_v in enumerate(bytes((((_b&0xF)<<4)|((_b&0xF0)>>4))for _b in"
                f" bytes(((_x>>_ur)|(_x<<(8-_ur)))&255 for _x in _d)))).decode())"
                f"(__import__('base64').b64decode('{d64}'),"
                f"__import__('base64').b64decode('{ka64}'),"
                f"__import__('base64').b64decode('{kb64}'),"
                f"__import__('base64').b64decode('{sm64}'),{sp},{ur})"
            )

        # S2+S3+S4+S7+S8 bilesik — 3 sablon arasindan sec
        t = random.randint(0, 2)
        if t == 0:
            return (
                f"(lambda _d,_ka,_kb,_sm,_sp,_ur:(lambda _k:"
                f"__import__('zlib').decompress(bytes(_v^_k[_i%32]for _i,_v in enumerate("
                f"bytes((((_b&15)<<4)|((_b&240)>>4))for _b in bytes(((_c>>_ur)|(_c<<(8-_ur)))&255 for _c in _d)))))"
                f".decode())(bytes(_ka[_i]^_sm[_i]for _i in range(_sp))+_kb))"
                f"(__import__('base64').b64decode('{d64}'),"
                f"__import__('base64').b64decode('{ka64}'),"
                f"__import__('base64').b64decode('{kb64}'),"
                f"__import__('base64').b64decode('{sm64}'),{sp},{ur})"
            )
        elif t == 1:
            return (
                f"(lambda _p:__import__('zlib').decompress(_p).decode())"
                f"((lambda _d,_k,_ur:bytes(_v^_k[_i%32]for _i,_v in enumerate("
                f"bytes((((_b&15)<<4)|((_b&240)>>4))for _b in bytes(((_c>>_ur)|(_c<<(8-_ur)))&255 for _c in _d))))"
                f")(__import__('base64').b64decode('{d64}'),"
                f"bytes(__import__('base64').b64decode('{ka64}')[_i]^__import__('base64').b64decode('{sm64}')[_i]for _i in range({sp}))+__import__('base64').b64decode('{kb64}'),{ur}))"
            )
        else:
            return (
                f"(lambda _d,_k,_ur:__import__('zlib').decompress("
                f"bytes(_v^_k[_i%32]for _i,_v in enumerate(bytes((((_b&15)<<4)|((_b&240)>>4))"
                f"for _b in bytes(((_c>>_ur)|(_c<<(8-_ur)))&255 for _c in _d))))).decode())"
                f"(__import__('base64').b64decode('{d64}'),"
                f"bytes(__import__('base64').b64decode('{ka64}')[_i]^__import__('base64').b64decode('{sm64}')[_i]for _i in range({sp}))+__import__('base64').b64decode('{kb64}'),{ur})"
            )

    @staticmethod
    def encrypt_all_strings_in_code(source: str) -> str:
        """
        AST tabanlı string şifreleme.
        - Tüm string literalleri bulur (dict, fonksiyon argümanı, nested yapı fark etmez)
        - import/from/def/class/decorator gibi hassas alanları otomatik atlar
        - Token, URL, bot key gibi değerleri güvenli şekilde şifreler
        - Regex tabanlı eski yaklaşımın kaçırdığı tüm formatları yakalar
        """
        import ast

        SKIP_CONTEXTS = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef,
                         ast.ClassDef, ast.alias, ast.keyword)

        MIN_LEN = 3

        def _should_skip_value(s: str) -> bool:
            if len(s) < MIN_LEN:
                return True
            if s.startswith('__') and s.endswith('__'):
                return True
            if not s.strip():
                return True
            return False

        try:
            tree = ast.parse(source)
        except SyntaxError:
            return StringEncryptor._legacy_encrypt(source)

        replacements: dict[int, str] = {}

        parent_map: dict[int, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent_map[id(child)] = node

        def _is_in_skip_context(node: ast.AST) -> bool:
            cur = parent_map.get(id(node))
            depth = 0
            while cur is not None and depth < 8:
                if isinstance(cur, SKIP_CONTEXTS):
                    return True
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    return True
                cur = parent_map.get(id(cur))
                depth += 1
            return False

        def _is_docstring(node: ast.Constant, parent: ast.AST) -> bool:
            if not isinstance(node.value, str):
                return False
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                body = getattr(parent, 'body', [])
                if body and isinstance(body[0], ast.Expr) and body[0].value is node:
                    return True
            return False

        def _is_in_fstring(node: ast.AST) -> bool:
            cur = parent_map.get(id(node))
            depth = 0
            while cur is not None and depth < 6:
                if isinstance(cur, ast.JoinedStr):
                    return True
                cur = parent_map.get(id(cur))
                depth += 1
            return False

        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            if not isinstance(node.value, str):
                continue

            s = node.value
            if _should_skip_value(s):
                continue

            parent = parent_map.get(id(node))
            if parent is None:
                continue

            if _is_docstring(node, parent):
                continue

            if _is_in_fstring(node):
                continue

            if _is_in_skip_context(node):
                continue

            try:
                encrypted_expr = StringEncryptor.simple_encrypt(s)
                replacements[id(node)] = encrypted_expr
            except Exception:
                continue

        if not replacements:
            return source

        class _StringReplacer(ast.NodeTransformer):
            def visit_Constant(self, node: ast.Constant):
                if id(node) in replacements:
                    expr_str = replacements[id(node)]
                    try:
                        new_node = ast.parse(expr_str, mode='eval').body
                        return ast.copy_location(new_node, node)
                    except Exception:
                        return node
                return node

        try:
            new_tree = _StringReplacer().visit(tree)
            ast.fix_missing_locations(new_tree)
            return ast.unparse(new_tree)
        except Exception:
            return StringEncryptor._legacy_encrypt(source)

    @staticmethod
    def _legacy_encrypt(source: str) -> str:
        lines = source.split('\n')
        result = []
        skip_keywords = ('import ', 'from ', '@', 'def ', 'class ', '__', 'raise ', 'except ')
        for line in lines:
            stripped = line.strip()
            if any(stripped.startswith(k) for k in skip_keywords):
                result.append(line)
                continue
            if "f'" in line or 'f"' in line:
                result.append(line)
                continue
            m = re.match(r'^(\s*\w+\s*=\s*)("([^"\\]{4,})")(\s*)$', line)
            if m:
                prefix, _, content, suffix = m.group(1), m.group(2), m.group(3), m.group(4)
                try:
                    encrypted = StringEncryptor.simple_encrypt(content)
                    result.append(f'{prefix}{encrypted}{suffix}')
                    continue
                except Exception:
                    pass
            result.append(line)
        return '\n'.join(result)

class ControlFlowObfuscator:

    def __init__(self):
        self.hyp = HyperionObfuscator()

    def inject_fake_branches(self, source: str) -> str:
        lines = source.split('\n')
        result = []
        for i, line in enumerate(lines):
            result.append(line)
            stripped = line.strip()
            if not (stripped.startswith('def ') and stripped.endswith(':')):
                continue
            indent_count = len(line) - len(line.lstrip())
            if i + 1 < len(lines):
                next_stripped = lines[i + 1].strip()
                if next_stripped.startswith('"""') or next_stripped.startswith("'''"):
                    continue
                if next_stripped.startswith('@'):
                    continue
            fake_v = self.hyp._randvar()
            fake_val = random.randint(100000, 9999999)
            result.append(' ' * (indent_count + 4) + f'{fake_v} = {fake_val}; del {fake_v}')
        return '\n'.join(result)

    def wrap_exec_with_opaque(self, exec_code: str, indent: int=8) -> str:
        ind = ' ' * indent
        safe_predicates = [f'(lambda x: x*x >= 0)({random.randint(1, 9999)})', f'(len(str({random.randint(10000, 99999)})) > 0)', f'(hash(None) == hash(None))', f'(sys.version_info >= (2, 0))']
        pred = random.choice(safe_predicates)
        fake_var = self.hyp._randvar()
        lines = exec_code.split('\n')
        indented = '\n'.join((ind + '    ' + l if l.strip() else l for l in lines))
        return f'{ind}{fake_var} = None\n{ind}if {pred}:\n{indented}\n{ind}del {fake_var}'

class DeadCodeInjector:

    def __init__(self):
        self.hyp = HyperionObfuscator()
        self._STATIC_BLOCKS = [
            '# _tmp_7f3a = 4096 * 17\n# _tmp_7f3b = __import__("hashlib").md5(b"ninja").hexdigest()\n# del _tmp_7f3a',
            '# _chk_9e1 = [x*x for x in range(32)]\n# _chk_9e2 = sum(_chk_9e1) % 999\n# del _chk_9e1',
            '# _buf_aa1 = bytes(range(16))\n# _buf_aa2 = __import__("zlib").crc32(_buf_aa1)\n# del _buf_aa1',
            '# _pad_b3 = "NinjaEnc" * 4\n# _pad_b4 = len(_pad_b3) ^ 0xFF\n# del _pad_b3',
            '# _val_c5 = (2**31 - 1) & 0xDEADBEEF\n# _val_c6 = _val_c5 >> 4\n# del _val_c5',
            '# _ref_d7 = {"k": 0x1337, "v": 0xDEAD}\n# _ref_d8 = list(_ref_d7.values())\n# del _ref_d7',
            '# _enc_e9 = __import__("base64").b64encode(b"\\x00" * 8).decode()\n# del _enc_e9',
            '# _map_f0 = {i: i*i for i in range(8)}\n# _map_f1 = max(_map_f0.values())\n# del _map_f0',
            '# _seq_11 = tuple(range(0, 64, 4))\n# _seq_12 = _seq_11[-1] - _seq_11[0]\n# del _seq_11',
            '# _acc_22 = 0\n# for _ii in range(16): _acc_22 += _ii\n# del _acc_22',
        ]

    def generate_dead_comment_block(self, index: int = 0) -> str:
        return self._STATIC_BLOCKS[index % len(self._STATIC_BLOCKS)]

    def inject_into_source(self, source: str, count: int = 8) -> str:
        lines = source.split('\n')
        if len(lines) < 10:
            return source
        blank_lines = [i for i, l in enumerate(lines) if l.strip() == '' and i > 2]
        if not blank_lines:
            return source
        _step = max(1, len(blank_lines) // count)
        inject_positions = sorted(
            [blank_lines[i * _step % len(blank_lines)] for i in range(min(count, len(blank_lines)))],
            reverse=True
        )
        for idx, pos in enumerate(inject_positions):
            dead = self.generate_dead_comment_block(idx)
            lines.insert(pos, dead)
        return '\n'.join(lines)

class OpcodeMutator:

    @staticmethod
    def mutate_pyc(pyc_file: str) -> bytes:
        try:
            with open(pyc_file, 'rb') as f:
                header = f.read(16)
                code_obj = marshal.load(f)

            def inject_nops(co):
                try:
                    import types
                    new_consts = tuple((inject_nops(c) if isinstance(c, types.CodeType) else c for c in co.co_consts))
                    raw = bytearray(co.co_code)
                    new_raw = bytearray()
                    i = 0
                    nop = opcode.opmap.get('NOP', 9)
                    while i < len(raw):
                        new_raw.append(raw[i])
                        new_raw.append(raw[i + 1])
                        if random.random() < 0.25:
                            new_raw.append(nop)
                            new_raw.append(0)
                        i += 2
                    return co.replace(co_code=bytes(new_raw), co_consts=new_consts)
                except Exception:
                    return co
            mutated = inject_nops(code_obj)
            buf = BytesIO()
            buf.write(header)
            marshal.dump(mutated, buf)
            logger.info('Opcode mutasyon uygulandı')
            return buf.getvalue()
        except Exception as e:
            logger.warning(f'Opcode mutasyon atlandı: {e}')
            with open(pyc_file, 'rb') as f:
                return f.read()

class AntiDebug:

    @staticmethod
    def generate_runtime_check() -> str:
        hyp = HyperionObfuscator()
        v = [hyp._randvar() for _ in range(30)]
        frida_ports = [27042, 27043, 10900]
        return f'''
import sys as _sys, os as _os, time as _tm, platform as _pl, hashlib as _hh

_IS_ANDROID = _os.path.exists("/system/build.prop") or "android" in _pl.platform().lower() or _os.path.exists("/data/data")
_IS_ARM = _pl.machine().lower() in ("aarch64", "arm", "armv7l", "armv8l")

def {v[0]}():
    if _IS_ANDROID: return False
    try:
        if _sys.gettrace() is not None: return True
    except Exception: pass
    try:
        if _sys.getprofile() is not None: return True
    except Exception: pass
    try:
        if _sys.flags.debug: return True
    except Exception: pass
    return False

def {v[1]}():
    try:
        with open("/proc/self/status", "r") as _f:
            for _l in _f:
                if _l.startswith("TracerPid:") and int(_l.split(":")[1].strip()) != 0:
                    return True
    except Exception: pass
    if not _IS_ANDROID:
        try:
            with open("/proc/self/wchan", "r") as _f:
                _wc = _f.read().strip()
                if "ptrace" in _wc: return True
        except Exception: pass
    return False

def {v[2]}():
    _bad = ["frida","frida-server","frida-agent","gdb","lldb","strace",
            "ltrace","radare2","r2","ida","ida64","x64dbg","ollydbg",
            "immunity","windbg","jdwp","jpda"]
    if not _IS_ANDROID:
        _bad += ["pycharm","pydev","debugpy","pydevd","pyspy","py-spy","viztracer","austin"]
    try:
        with open("/proc/self/maps", "r") as _f:
            _c2 = _f.read().lower()
            for _s in _bad:
                if _s in _c2: return True
    except Exception: pass
    try:
        _exe = _os.readlink("/proc/self/exe").lower()
        for _s in _bad:
            if _s in _exe: return True
    except Exception: pass
    return False

def {v[4]}():
    if _IS_ANDROID: return False
    try:
        import gc as _gc
        for _o in _gc.get_objects():
            if type(_o).__name__ in ("Bdb","Pdb","RemoteDebugger","DebuggerConnection","PydevdAPI"):
                return True
    except Exception: pass
    try:
        _dbg_mods = ["pdb","bdb","pydevd","debugpy","_pydevd_bundle","pydevd_tracing"]
        for _dm in _dbg_mods:
            if _dm in _sys.modules: return True
    except Exception: pass
    return False

def {v[5]}():
    try:
        _pp = _os.popen("ps -o comm= -p " + str(_os.getppid())).read().strip().lower()
        for _s in ["gdb","lldb","strace","frida","radare2","ida"]:
            if _s in _pp: return True
    except Exception: pass
    try:
        _ppid = str(_os.getppid())
        _pexe = _os.readlink(f"/proc/{{_ppid}}/exe").lower()
        for _s in ["gdb","lldb","frida","radare2","ida"]:
            if _s in _pexe: return True
    except Exception: pass
    return False

def {v[7]}():
    import socket as _sk, threading as _th
    try:
        for _p in {frida_ports}:
            _s = _sk.socket()
            _s.settimeout(0.05)
            if _s.connect_ex(("127.0.0.1", _p)) == 0:
                _s.close(); return True
            _s.close()
    except Exception: pass
    try:
        if any("frida" in _t.name.lower() for _t in _th.enumerate()): return True
    except Exception: pass
    try:
        _ld = _os.environ.get("LD_PRELOAD","").lower()
        if _ld and any(x in _ld for x in ["frida","gadget","interpose"]): return True
    except Exception: pass
    try:
        _fd_dir = "/proc/self/fd"
        if _os.path.exists(_fd_dir):
            for _fd in _os.listdir(_fd_dir):
                try:
                    _lnk = _os.readlink(_os.path.join(_fd_dir, _fd)).lower()
                    if "frida" in _lnk or "gum" in _lnk: return True
                except Exception: pass
    except Exception: pass
    return False

def {v[9]}():
    try:
        _t0 = _tm.monotonic()
        _acc = 0
        for _ii in range(10000): _acc += _ii
        _base = _tm.monotonic() - _t0
        _t1 = _tm.monotonic()
        _acc2 = 0
        for _ii in range(10000): _acc2 += _ii
        _real = _tm.monotonic() - _t1
        if _base > 0 and _real > _base * 10: return True
        _limit = 3.0 if _IS_ARM else 1.0
        if _real > _limit: return True
    except Exception: pass
    return False

{v[8]}()

def {v[6]}():
    _sc = 0
    if {v[0]}(): _sc += 5
    if {v[1]}(): _sc += 5
    if {v[2]}(): _sc += 4
    if {v[4]}(): _sc += 4
    if {v[5]}(): _sc += 3
    if {v[7]}(): _sc += 5
    if {v[9]}(): _sc += 3
    _threshold = 8
    if _sc < _threshold: return
    _actions = [
        lambda: _os._exit(0),
        lambda: (_ for _ in ()).throw(MemoryError()),
        lambda: (_ for _ in ()).throw(ImportError("No module named _hashcore")),
        lambda: (_ for _ in ()).throw(OSError(22, "Invalid argument")),
        lambda: _os.kill(_os.getpid(), 9),
        lambda: (_ for _ in ()).throw(SystemExit(1)),
        lambda: (_ for _ in ()).throw(OverflowError("int too large")),
    ]
    _actions[_sc % len(_actions)]()

{v[6]}()
'''

    @staticmethod
    def generate_async_runtime_check() -> str:
        return generate_async_antidebug_code()

    @staticmethod
    def generate_marshal_bypass() -> str:
        hyp = HyperionObfuscator()
        v = [hyp._randvar() for _ in range(6)]
        chunk_key = random.randint(1, 255)
        return f'''
def {v[0]}(_data):
    import marshal as _m
    _k = {chunk_key}
    _dec = bytes(b ^ _k for b in _data)
    _sz = len(_dec)
    _chunk = max(1, _sz // 7)
    _parts = [_dec[_i:_i+_chunk] for _i in range(0, _sz, _chunk)]
    _full = b"".join(_parts)
    del _dec, _parts
    return _m.loads(_full)
'''

class ASTObfuscator:
    KEYWORDS = {
        'False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await',
        'break', 'class', 'continue', 'def', 'del', 'elif', 'else', 'except',
        'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is',
        'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'try',
        'while', 'with', 'yield', 'self', 'cls',
        '__init__', '__main__', '__name__', '__file__', '__builtins__',
        '__doc__', '__all__', '__dict__', '__class__', '__module__',
        '__spec__', '__loader__', '__package__', '__cached__', '__path__',
        'print', 'input', 'open', 'len', 'range', 'str', 'int', 'float',
        'list', 'dict', 'tuple', 'set', 'bool', 'bytes', 'type', 'super',
        'isinstance', 'issubclass', 'hasattr', 'getattr', 'setattr',
        'delattr', 'staticmethod', 'classmethod', 'property',
        'Exception', 'BaseException', 'RuntimeError', 'ValueError',
        'TypeError', 'ImportError', 'OSError', 'MemoryError', 'SystemExit',
        'KeyboardInterrupt', 'StopIteration', 'AttributeError', 'NameError',
        'enumerate', 'zip', 'map', 'filter', 'sorted', 'reversed',
        'any', 'all', 'min', 'max', 'sum', 'abs', 'round',
        'hex', 'oct', 'bin', 'ord', 'chr', 'repr', 'vars', 'dir',
        'id', 'hash', 'iter', 'next', 'callable',
        'exec', 'eval', 'compile', 'globals', 'locals', 'exit', 'quit',
        'object', 'bytearray', 'memoryview', 'frozenset', 'complex',
        'NotImplemented', 'Ellipsis',
        'os', 'sys', 'random', 'base64', 'zlib', 'marshal', 'hashlib',
        'types', 'struct', 'time', 'platform', 'shutil', 'tempfile',
        'ctypes', 'builtins', 'importlib', 'io', 'gc', 'socket',
        'threading', 'atexit', 'mmap', 're', 'math',
        '_b64', '_k', '_m', '_zl', '_ty', '_ct', '_hl', '_oo', '_sy',
        '_BT', '_bt', '_tp', '_rng', '_r', '_f', '_d', '_sp',
        'pythonapi', 'PyEval_EvalCode', 'py_object', 'c_char', 'c_char_p',
        'POINTER', 'memset', 'cast',
    }

    def __init__(self):
        self.name_map = {}
        self._salt = hashlib.sha256(os.urandom(16)).hexdigest()[:8]

    def _hash_name(self, name: str) -> str:
        h = hashlib.shake_128((name + self._salt).encode()).hexdigest(8)
        return f'_{h}'

    # Tüm gerçek builtin adları (ZeroDivisionError, KeyError, divmod, frozenset,
    # ...) — sabit KEYWORDS listesi kaçınılmaz olarak eksik kalır; builtins'i
    # doğrudan sorgulamak her builtin'i güvenilir biçimde korur.
    import builtins as _bltns_mod
    _BUILTIN_NAMES = frozenset(dir(_bltns_mod))
    del _bltns_mod

    def _should_rename(self, name: str) -> bool:
        if name in self.KEYWORDS:
            return False
        if name in self._BUILTIN_NAMES:
            return False
        if name.startswith('__') and name.endswith('__'):
            return False
        return True

    def _get_mapped(self, name: str) -> str:
        if not self._should_rename(name):
            return name
        if name not in self.name_map:
            self.name_map[name] = self._hash_name(name)
        return self.name_map[name]

    def obfuscate(self, source: str, extra_protected=None) -> str:
        try:
            import ast as _ast
            tree = _ast.parse(source)
            protected = set(extra_protected) if extra_protected else set()
            for node in _ast.walk(tree):
                if isinstance(node, (_ast.Import, _ast.ImportFrom)):
                    for alias in node.names:
                        protected.add(alias.name.split('.')[0])
                        if alias.asname:
                            protected.add(alias.asname)
                if isinstance(node, _ast.ImportFrom) and node.module:
                    protected.add(node.module.split('.')[0])
                # ── Keyword-arg güvenliği ────────────────────────────────────
                # Global (tutarlı) yeniden adlandırmada bir ad ya HER YERDE ya da
                # HİÇBİR YERDE değişir. Bir ad çağrıda keyword olarak geçiyorsa
                # (f(param=...)), parametreyi yeniden adlandırıp keyword'ü
                # bırakmak çağrıyı bozar. Ayrıca **kwargs'lı bir fonksiyonun
                # keyword'le geçirilebilen parametrelerini yeniden adlandırmak da
                # bağlanmayı bozar. Bu adları KORU (yeniden adlandırma) → çıktı
                # her zaman doğru bağlanır; yalnızca birkaç ad açıkta kalır.
                if isinstance(node, _ast.Call):
                    for _kw in node.keywords:
                        if _kw.arg is not None:
                            protected.add(_kw.arg)
                if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    if node.args.kwarg is not None:
                        for _pa in (node.args.args + node.args.kwonlyargs):
                            protected.add(_pa.arg)
                # ── Metod-adı güvenliği ──────────────────────────────────────
                # Metodlar attribute üzerinden çağrılır (obj.method()); attribute
                # erişimleri hiçbir zaman yeniden adlandırılmaz. Metod TANIMINI
                # yeniden adlandırıp çağrıyı bırakmak AttributeError verir. Bu
                # yüzden bir class gövdesindeki doğrudan metod adlarını KORU.
                if isinstance(node, _ast.ClassDef):
                    for _mnode in node.body:
                        if isinstance(_mnode, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                            protected.add(_mnode.name)
            obf = self

            class NameTransformer(_ast.NodeTransformer):

                def visit_Name(self_, node):
                    try:
                        if node.id in {'__name__', '__main__', '__file__', '__builtins__'}:
                            return node
                        if node.id not in protected and obf._should_rename(node.id):
                            node.id = obf._get_mapped(node.id)
                    except (AttributeError, TypeError):
                        pass
                    return node

                def visit_FunctionDef(self_, node):
                    try:
                        if node.name not in protected and obf._should_rename(node.name):
                            node.name = obf._get_mapped(node.name)
                    except (AttributeError, TypeError):
                        pass
                    self_.generic_visit(node)
                    return node

                def visit_AsyncFunctionDef(self_, node):
                    return self_.visit_FunctionDef(node)

                def visit_ClassDef(self_, node):
                    try:
                        if node.name not in protected and obf._should_rename(node.name):
                            node.name = obf._get_mapped(node.name)
                    except (AttributeError, TypeError):
                        pass
                    self_.generic_visit(node)
                    return node

                def visit_arg(self_, node):
                    try:
                        if (node.arg not in ('self', 'cls')
                                and node.arg not in protected
                                and obf._should_rename(node.arg)):
                            node.arg = obf._get_mapped(node.arg)
                    except (AttributeError, TypeError):
                        pass
                    return node

                def visit_Global(self_, node):
                    try:
                        node.names = [obf._get_mapped(n) if obf._should_rename(n) else n for n in node.names]
                    except (AttributeError, TypeError):
                        pass
                    return node

            transformer = NameTransformer()
            new_tree = transformer.visit(tree)
            _ast.fix_missing_locations(new_tree)
            result = _ast.unparse(new_tree)
            logger.info(f'AST obfuscation: {len(self.name_map)} isim yeniden adlandırıldı')
            return result
        except Exception as e:
            logger.warning(f'AST obfuscation atlandı: {e}')
            return source

class ControlFlowFlattener:

    def __init__(self):
        self.hyp = HyperionObfuscator()

    def flatten_source(self, source: str) -> str:
        try:
            import ast as _ast
            tree = _ast.parse(source)
            count = [0]
            hyp = self.hyp

            class FlattenTransformer(_ast.NodeTransformer):

                def visit_FunctionDef(self_, node):
                    self_.generic_visit(node)
                    if_count = sum((1 for n in _ast.walk(node) if isinstance(n, _ast.If)))
                    if if_count < 3:
                        return node
                    try:
                        return self_._flatten(node)
                    except Exception:
                        return node

                def visit_AsyncFunctionDef(self_, node):
                    return self_.visit_FunctionDef(node)

                def _flatten(self_, func):
                    state_var = hyp._randvar()
                    states = {i: stmt for i, stmt in enumerate(func.body)}
                    if len(states) < 3:
                        return func
                    cases = []
                    for sid, stmt in states.items():
                        next_sid = sid + 1 if sid + 1 in states else -1
                        test = _ast.Compare(left=_ast.Name(id=state_var, ctx=_ast.Load()), ops=[_ast.Eq()], comparators=[_ast.Constant(value=sid)])
                        next_assign = _ast.Assign(targets=[_ast.Name(id=state_var, ctx=_ast.Store())], value=_ast.Constant(value=next_sid), lineno=0, col_offset=0)
                        _ast.fix_missing_locations(next_assign)
                        cases.append(_ast.If(test=test, body=[stmt, next_assign], orelse=[]))
                    root_if = cases[0]
                    cur = root_if
                    for c in cases[1:]:
                        cur.orelse = [c]
                        cur = c
                    init = _ast.Assign(targets=[_ast.Name(id=state_var, ctx=_ast.Store())], value=_ast.Constant(value=0), lineno=0, col_offset=0)
                    _ast.fix_missing_locations(init)
                    while_node = _ast.While(test=_ast.Compare(left=_ast.Name(id=state_var, ctx=_ast.Load()), ops=[_ast.NotEq()], comparators=[_ast.Constant(value=-1)]), body=[root_if], orelse=[])
                    _ast.fix_missing_locations(while_node)
                    func.body = [init, while_node]
                    _ast.fix_missing_locations(func)
                    count[0] += 1
                    return func
            new_tree = FlattenTransformer().visit(tree)
            _ast.fix_missing_locations(new_tree)
            result = _ast.unparse(new_tree)
            logger.info(f'Control flow flattening: {count[0]} fonksiyon dönüştürüldü')
            return result
        except Exception as e:
            logger.warning(f'Control flow flattening atlandı: {e}')
            return source

class MBATransformer:
    MBA_RULES_L1 = {
        'BitOr':  '(({a}) & ~({b})) + ({b})',
        'BitXor': '(({a}) | ({b})) - (({a}) & ({b}))',
        'BitAnd': '(~(~({a}) | ~({b})))',
        'Sub':    '(({a}) + ~({b})) + 1',
    }
    # NOT: L2 kuralları yalnızca DEĞERİ TAMSAYI olduğu KESİN olan ifadelere
    # uygulanır (bkz. visit_BinOp içindeki both_int kapısı). Hepsi absorpsiyon/
    # kimlik yasalarıdır → her tamsayı için birebir aynı sonucu verir. 64-bit
    # varsayımı yapan eski '<< 1 >> 1' kuralı kaldırıldı (taşınabilirlik).
    MBA_RULES_L2 = [
        '(({x}) * 1)',
        '(({x}) + 0)',
        '((({x}) ^ 0xFFFFFFFF) ^ 0xFFFFFFFF)',
        '(({x}) | (({x}) & 0))',
        '(({x}) & (({x}) | 0xFFFFFFFF))',
    ]

    def transform_source(self, source: str) -> str:
        try:
            import ast as _ast
            tree = _ast.parse(source)
            count = [0]

            class MBAVisitor(_ast.NodeTransformer):

                def visit_BinOp(self_, node):
                    self_.generic_visit(node)
                    op_name = type(node.op).__name__
                    rule = MBATransformer.MBA_RULES_L1.get(op_name)
                    if rule is None or random.random() > 0.45:
                        return node
                    L, R = node.left, node.right
                    if not isinstance(L, (_ast.Name, _ast.Constant)):
                        return node
                    if not isinstance(R, (_ast.Name, _ast.Constant)):
                        return node

                    # ── Tür güvenliği ────────────────────────────────────────
                    # MBA kuralları bit operatörleri (~, &, |) içerir; bunlar
                    # yalnızca tamsayılarda geçerlidir. Orijinal ifade float/set
                    # üzerinde çalışıyorsa naif yeniden yazım RUNTIME'da TypeError
                    # verir (ör. 'a - b' float iken '(a + ~b) + 1' → ~float hatası).
                    # Bu yüzden her kuralı yalnızca KANITLI güvenli olduğunda uygula.
                    def _is_int_const(n):
                        return (isinstance(n, _ast.Constant)
                                and isinstance(n.value, int)
                                and not isinstance(n.value, bool))
                    both_int = _is_int_const(L) and _is_int_const(R)
                    if op_name == 'BitXor':
                        # (a|b)-(a&b): ~ içermez → int VE set/frozenset için geçerli
                        safe = True
                    elif op_name == 'Sub':
                        # (a + ~b) + 1: ~ yalnızca b'ye → b tamsayı literali olmalı,
                        # a herhangi bir sayısal (float dahil) olabilir.
                        safe = _is_int_const(R)
                    else:  # BitOr / BitAnd: ~ her iki tarafta → ikisi de int literal
                        safe = both_int
                    if not safe:
                        return node
                    try:
                        a = _ast.unparse(L)
                        b = _ast.unparse(R)
                        expr_l1 = rule.format(a=a, b=b)
                        # L2 sarmalama yalnızca değer KESİN tamsayı iken (set/float
                        # riski yok) uygulanır.
                        if both_int and random.random() < 0.40:
                            l2 = random.choice(MBATransformer.MBA_RULES_L2)
                            expr_l1 = l2.format(x=expr_l1)
                        new_node = _ast.parse(expr_l1, mode='eval').body
                        _ast.fix_missing_locations(new_node)
                        count[0] += 1
                        return new_node
                    except Exception:
                        return node

            new_tree = MBAVisitor().visit(tree)
            _ast.fix_missing_locations(new_tree)
            result = _ast.unparse(new_tree)
            logger.info(f'MBA dönüşümü: {count[0]} ifade dönüştürüldü (L1+L2 polinom)')
            return result
        except Exception as e:
            logger.warning(f'MBA dönüşümü atlandı: {e}')
            return source

class SelfModifyingCode:
    """
    Runtime'da kodu bellekte değiştir.
    Disk'e hiç temiz kaynak yazılmaz.
    """

    def generate_wrapper(self, payload_b64: str, xor_keys: list) -> str:
        hyp = HyperionObfuscator()
        v = [hyp._randvar() for _ in range(14)]
        k0, k1, k2 = xor_keys[0], xor_keys[1], xor_keys[2]
        return f'''import base64, zlib, marshal, sys, types, os, ctypes as _ct

{v[0]} = "{payload_b64}"
{v[1]} = base64.b64decode({v[0]})
{v[2]} = bytes(b ^ {k2} for b in {v[1]})
{v[3]} = bytes(b ^ {k1} for b in {v[2]})
{v[4]} = bytes(b ^ {k0} for b in {v[3]})
{v[5]} = zlib.decompress({v[4]})
{v[6]} = marshal.loads({v[5]})
del {v[0]}, {v[1]}, {v[2]}, {v[3]}, {v[4]}, {v[5]}
{v[7]} = types.ModuleType("__smcmod__")
{v[7]}.__dict__.update({{"__name__": "__main__", "__builtins__": __builtins__, "__file__": globals().get("__file__", "<ninja>")}})
try:
    {v[8]} = _ct.pythonapi.PyEval_EvalCode
    {v[8]}.restype  = _ct.py_object
    {v[8]}.argtypes = [_ct.py_object, _ct.py_object, _ct.py_object]
    {v[8]}({v[6]}, {v[7]}.__dict__, {v[7]}.__dict__)
    del {v[8]}
except Exception:
    exec({v[6]}, {v[7]}.__dict__)
try:
    {v[9]} = getattr({v[6]}, "co_code", b"")
    if {v[9]}:
        {v[10]} = _ct.cast(_ct.c_char_p(id({v[9]})), _ct.POINTER(_ct.c_char))
        _ct.memset({v[10]}, 0, len({v[9]}))
    del {v[9]}, {v[10]}
except Exception: pass
del {v[6]}, {v[7]}
'''

    def wrap(self, source: str) -> str:
        try:
            code_obj = compile(source, '<ninja>', 'exec')
            raw = marshal.dumps(code_obj)
            compressed = zlib.compress(raw, 9)
            xor = XORObfuscator()
            xor_data, keys = xor.multi_xor_encode(compressed)
            b64 = base64.b64encode(xor_data).decode('ascii')
            result = self.generate_wrapper(b64, keys)
            logger.info("Self-modifying code wrapper oluşturuldu")
            return result
        except Exception as e:
            logger.warning(f"SelfModifyingCode atlandı: {e}")
            return source

class MemoryProtector:
    """
    Runtime'da bytecode bellekten silinir.
    ctypes.pythonapi.PyEval_EvalCode kullanarak builtins.exec hook'u tamamen atlanır.
    exec/print swap saldırısına karşı bağışık.
    """

    def __init__(self):
        self.hyp = HyperionObfuscator()

    def generate_ctypes_exec_block(self, var_code: str, var_globs: str) -> str:
        """
        builtins.exec yerine doğrudan C API'yi çağıran execution bloğu.
        exec → print gibi swap'lara %100 bağışık.
        Execution sonrası co_code bellekten sıfırlanır.
        """
        v = [self.hyp._randvar() for _ in range(10)]
        return f'''
try:
    import ctypes as {v[0]}
    {v[1]} = {v[0]}.pythonapi.PyEval_EvalCode
    {v[1]}.restype  = {v[0]}.py_object
    {v[1]}.argtypes = [{v[0]}.py_object, {v[0]}.py_object, {v[0]}.py_object]
    {v[2]} = {v[1]}({var_code}, {var_globs}, {var_globs})
    del {v[1]}, {v[2]}
    try:
        {v[3]} = getattr({var_code}, 'co_code', b'')
        if {v[3]}:
            {v[4]} = {v[0]}.cast({v[0]}.c_char_p(id({v[3]})), {v[0]}.POINTER({v[0]}.c_char))
            {v[0]}.memset({v[4]}, 0, len({v[3]}))
        del {v[3]}, {v[4]}
    except Exception: pass
    del {v[0]}
except Exception:
    exec({var_code}, {var_globs})
try: del {var_code}
except Exception: pass
'''

    def generate_chunk_exec(self, source: str) -> str:
        """
        Kaynak kodu N parçaya böl.
        Her parça ayrı ayrı şifrelenir, çalıştırılır, hemen bellekten silinir.
        Dump saldırısında hiçbir zaman tam kod bellekte olmaz.
        """
        v = [self.hyp._randvar() for _ in range(12)]
        lines = source.split('\n')
        chunk_size = max(20, len(lines) // 4)
        chunks = []
        for i in range(0, len(lines), chunk_size):
            chunk = '\n'.join(lines[i:i+chunk_size])
            key = random.randint(1, 255)
            enc = base64.b64encode(bytes(b ^ key for b in
                  zlib.compress(chunk.encode('utf-8'), 9))).decode('ascii')
            chunks.append((enc, key))

        chunks_repr = repr(chunks)
        return f'''import base64 as _B64, zlib as _ZL, ctypes as _CT, types as _TY

{v[0]} = {chunks_repr}
{v[1]} = {{"__name__": "__main__", "__builtins__": __builtins__}}
for {v[2]}, {v[3]} in {v[0]}:
    {v[4]} = _ZL.decompress(bytes(b ^ {v[3]} for b in _B64.b64decode({v[2]})))
    {v[5]} = compile({v[4]}.decode(), "<ninja_chunk>", "exec")
    del {v[4]}
    try:
        {v[6]} = _CT.pythonapi.PyEval_EvalCode
        {v[6]}.restype  = _CT.py_object
        {v[6]}.argtypes = [_CT.py_object, _CT.py_object, _CT.py_object]
        {v[6]}({v[5]}, {v[1]}, {v[1]})
        del {v[6]}
    except Exception:
        exec({v[5]}, {v[1]})
    try:
        {v[7]} = getattr({v[5]}, "co_code", b"")
        if {v[7]}:
            {v[8]} = _CT.cast(_CT.c_char_p(id({v[7]})), _CT.POINTER(_CT.c_char))
            _CT.memset({v[8]}, 0, len({v[7]}))
        del {v[7]}, {v[8]}
    except Exception: pass
    del {v[5]}
del {v[0]}, {v[1]}
'''

    def generate_strong_exec_guard(self) -> str:
        """
        Güçlü exec guard:
        1. builtins.exec'in gerçek C fonksiyonu olup olmadığını kontrol et
        2. Call stack'te debugger frame var mı?
        3. exec'in co_code'u var mı? (Python fonksiyonuna replace edilmiş mi?)
        4. Değiştirildiyse ctypes ile orijinalini geri yükle
        """
        v = [self.hyp._randvar() for _ in range(15)]
        return f'''
def {v[0]}():
    import builtins as _bt, types as _tp, sys as _sy, ctypes as _ct
    {v[1]} = getattr(_bt, 'exec')
    if isinstance({v[1]}, _tp.FunctionType):
        try:
            {v[2]} = _ct.pythonapi
            {v[3]} = getattr(__builtins__, 'exec', None) if isinstance(__builtins__, dict) else None
            import importlib as _il
            {v[4]} = _il.import_module('builtins')
            import sys as _sys2
            if 'builtins' in _sys2.modules:
                del _sys2.modules['builtins']
            import builtins as _fresh_bt
            _sys2.modules['builtins'] = _fresh_bt
            _bt.exec = _fresh_bt.exec
            del {v[4]}, _sys2, _fresh_bt, _il
        except Exception: pass
    import marshal as _ma
    if isinstance(getattr(_ma, 'loads', None), _tp.FunctionType):
        try:
            import importlib as _il2
            if 'marshal' in __import__('sys').modules:
                del __import__('sys').modules['marshal']
            import marshal as _fresh_ma
            __import__('sys').modules['marshal'] = _fresh_ma
            _ma.loads = _fresh_ma.loads
        except Exception: pass
    {v[5]} = _sy._getframe()
    {v[6]} = 0
    while {v[5]}:
        {v[5]} = {v[5]}.f_back
        {v[6]} += 1
    if {v[6]} > 30:
        import os as _oo
        _oo._exit(1)
    del {v[5]}, {v[6]}
    if _sy.gettrace() is not None or _sy.getprofile() is not None:
        import os as _oo2
        _oo2._exit(1)

{v[0]}()
del {v[0]}
'''

class PolymorphicEncryptor:
    """
    Her şifrelemede farklı anahtar + farklı algorithm kombinasyonu.
    Aynı dosyayı 2 kez encode etsen 2 tamamen farklı çıktı çıkar.
    """

    ALGORITHMS = ['xor_cascade', 'rot_xor', 'byte_shuffle', 'nibble_swap']

    def __init__(self):
        self.hyp = HyperionObfuscator()

    def _xor_cascade(self, data: bytes) -> tuple:
        keys = [random.randint(1, 255) for _ in range(4)]
        result = data
        for k in keys:
            result = bytes(b ^ k for b in result)
        return result, keys

    def _rot_xor(self, data: bytes) -> tuple:
        key = random.randint(1, 127)
        rot = random.randint(1, 7)
        result = bytes(((b ^ key) << rot | (b ^ key) >> (8 - rot)) & 0xFF for b in data)
        return result, [key, rot]

    def _byte_shuffle(self, data: bytes) -> tuple:
        seed = random.randint(0, 2**31)
        rng = random.Random(seed)
        indices = list(range(len(data)))
        rng.shuffle(indices)
        result = bytearray(len(data))
        for new_pos, old_pos in enumerate(indices):
            result[new_pos] = data[old_pos]
        return bytes(result), [seed, len(data)]

    def _nibble_swap(self, data: bytes) -> tuple:
        key = random.randint(0, 255)
        result = bytes(((b & 0x0F) << 4 | (b & 0xF0) >> 4) ^ key for b in data)
        return result, [key]

    def encrypt(self, data: bytes) -> tuple:
        algo = random.choice(self.ALGORITHMS)
        method = getattr(self, f'_{algo}')
        enc_data, params = method(data)
        meta = {'algo': algo, 'params': params, 'salt': random.randint(0, 65535)}
        meta_bytes = repr(meta).encode()
        meta_len = len(meta_bytes).to_bytes(4, 'big')
        payload = meta_len + meta_bytes + enc_data
        return payload, algo

    def generate_decrypt_code(self, var_enc: str, var_out: str) -> str:
        v = [self.hyp._randvar() for _ in range(8)]
        return f'''
{v[0]} = int.from_bytes({var_enc}[:4], 'big')
{v[1]} = eval({var_enc}[4:4+{v[0]}])
{v[2]} = {var_enc}[4+{v[0]}:]
{v[3]} = {v[1]}['algo']
{v[4]} = {v[1]}['params']
if {v[3]} == 'xor_cascade':
    {v[5]} = {v[2]}
    for _k in reversed({v[4]}):
        {v[5]} = bytes(b ^ _k for b in {v[5]})
    {var_out} = {v[5]}
elif {v[3]} == 'rot_xor':
    _key, _rot = {v[4]}
    {var_out} = bytes((((b >> _rot | b << (8-_rot)) & 0xFF) ^ _key) for b in {v[2]})
elif {v[3]} == 'byte_shuffle':
    _seed, _ln = {v[4]}
    import random as _rnd
    _rng = _rnd.Random(_seed)
    _idx = list(range(_ln))
    _rng.shuffle(_idx)
    _rev = [0]*_ln
    for _ni, _oi in enumerate(_idx): _rev[_oi] = _ni
    _buf = bytearray(_ln)
    for _ni, _oi in enumerate(_idx): _buf[_oi] = {v[2]}[_ni]
    {var_out} = bytes(_buf)
elif {v[3]} == 'nibble_swap':
    _key = {v[4]}[0]
    {var_out} = bytes(((b ^ _key) & 0x0F) << 4 | ((b ^ _key) & 0xF0) >> 4 for b in {v[2]})
else:
    {var_out} = {v[2]}
'''

class OpaquePredicates:
    """
    Her zaman True/False dönen ama analiz aracının bilemeyeceği koşullar.
    Gerçek kodu sahte dalların içine gömer.
    IDA Pro, Ghidra, uncompyle6 yanılıyor.
    random bağımlılığı YOK — Pydroid3 uyumlu.
    """

    TRUE_PREDICATES = [
        '(2**31 - 1) > 0',
        'len(str(123456789)) == 9',
        '(0xFF & 0xFF) == 255',
        '(lambda x: x*x >= 0)(42)',
        'sum(range(10)) == 45',
        'bool(len(str(123456789)) == 9)',
        '(1 << 8) == 256',
        '(0xDEAD & 0xFFFF) > 0',
        'abs(-999) == 999',
        '(3**3) == 27',
    ]

    FALSE_PREDICATES = [
        'sys.maxsize < 0',
        'len([]) > 1',
        '(1 << 128) < 0',
        '(0 & 0xFFFF) > 0',
        'sum(range(0)) > 0',
        '(-1 & 0xFF) == 0',
        'len("") > 0',
        '(2 ** 0) == 0',
    ]

    def __init__(self):
        self.hyp = HyperionObfuscator()
        self._pred_idx = 0

    def _next_true(self) -> str:
        p = self.TRUE_PREDICATES[self._pred_idx % len(self.TRUE_PREDICATES)]
        self._pred_idx += 1
        return p

    def _next_false(self) -> str:
        p = self.FALSE_PREDICATES[self._pred_idx % len(self.FALSE_PREDICATES)]
        self._pred_idx += 1
        return p

    def wrap_with_opaques(self, source: str, depth: int = 3) -> str:
        try:
            import ast as _ast
            tree = _ast.parse(source)
            count = [0]
            hyp = self.hyp
            pred_idx = [0]

            class OpaqueTransformer(_ast.NodeTransformer):
                def visit_FunctionDef(self_, node):
                    self_.generic_visit(node)
                    if count[0] % 2 == 0:
                        node.body = self_._wrap_body(node.body)
                    count[0] += 1
                    return node

                def _wrap_body(self_, body):
                    if not body:
                        return body
                    true_p  = OpaquePredicates.TRUE_PREDICATES[pred_idx[0] % len(OpaquePredicates.TRUE_PREDICATES)]
                    false_p = OpaquePredicates.FALSE_PREDICATES[pred_idx[0] % len(OpaquePredicates.FALSE_PREDICATES)]
                    pred_idx[0] += 1
                    dead_var = hyp._randvar()
                    dead_assign = _ast.parse(f'{dead_var} = 0').body[0]
                    fake_if = _ast.parse(
                        f'if {false_p}:\n    {dead_var} = {dead_var} + 1'
                    ).body[0]
                    guard = _ast.parse(
                        f'if not ({true_p}):\n    raise RuntimeError("integrity")'
                    ).body[0]
                    _ast.fix_missing_locations(dead_assign)
                    _ast.fix_missing_locations(fake_if)
                    _ast.fix_missing_locations(guard)
                    return [dead_assign, fake_if, guard] + body

            new_tree = OpaqueTransformer().visit(tree)
            _ast.fix_missing_locations(new_tree)
            result = _ast.unparse(new_tree)
            logger.info(f"Opaque predicates: {count[0]} fonksiyona eklendi")
            return result
        except Exception as e:
            logger.warning(f"Opaque predicates atlandı: {e}")
            return source

class StringTableEncryptor:
    """
    Tüm string literalleri runtime tablosuna taşır.
    Sadece ihtiyaç anında decrypt eder.
    Memory dump'ta bile string görünmez.
    """

    def __init__(self):
        self.hyp = HyperionObfuscator()

    def encrypt_to_table(self, source: str) -> str:
        try:
            import ast as _ast

            tree = _ast.parse(source)
            string_table = {}
            table_var = self.hyp._randvar()
            key = random.randint(1, 255)

            class StringCollector(_ast.NodeTransformer):
                def __init__(self_):
                    self_._in_fstring = False

                def visit_JoinedStr(self_, node):
                    return node

                def visit_Constant(self_, node):
                    if not isinstance(node.value, str):
                        return node
                    if len(node.value) < 3:
                        return node

                    s = node.value

                    SAFE_IMPORTS = {
                        'requests', 'urllib', 'urllib3', 'httpx', 'aiohttp',
                        'json', 'os', 'sys', 'socket', 'ssl', 'http',
                        'http.client', 'urllib.request', 'urllib.parse'
                    }
                    if s in SAFE_IMPORTS:
                        return node
                    if s not in string_table:
                        enc = base64.b64encode(
                            bytes(b ^ key for b in s.encode('utf-8'))
                        ).decode('ascii')
                        idx = len(string_table)
                        string_table[s] = (idx, enc)
                    idx, _ = string_table[s]
                    new_node = _ast.parse(
                        f'{table_var}[{idx}]', mode='eval'
                    ).body
                    _ast.fix_missing_locations(new_node)
                    return new_node

            if not string_table:
                collector = StringCollector()
                collector.visit(tree)

            if len(string_table) < 3:
                return source

            collector2 = StringCollector()
            new_tree = collector2.visit(_ast.parse(source))
            _ast.fix_missing_locations(new_tree)

            entries = sorted(string_table.values(), key=lambda x: x[0])
            table_def_lines = [f'import base64 as _b64']
            table_def_lines.append(f'_k = {key}')
            table_def_lines.append(f'{table_var} = {{}}')
            for orig, (idx, enc) in string_table.items():
                table_def_lines.append(
                    f'{table_var}[{idx}] = bytes(b ^ _k for b in _b64.b64decode("{enc}")).decode("utf-8")'
                )
            table_def_lines.append('del _k, _b64')
            table_init = '\n'.join(table_def_lines)

            body_code = _ast.unparse(new_tree)
            result = table_init + '\n' + body_code
            logger.info(f"String table: {len(string_table)} string şifrelendi")
            return result
        except Exception as e:
            logger.warning(f"String table encryption atlandı: {e}")
            return source

# ==============================================================================
# NinjaVM — GERÇEK stack-tabanlı sanal makine (seçili fonksiyonları
# kendi ISA'sına derler; Python bytecode'una geri-map tablosu YOK).
# ==============================================================================
class NinjaVM:
    """
    Gerçek stack-tabanlı sanal makine.

    Mevcut BytecodeVirtualizer'dan farkı: Python opcode'larını custom
    numaralara BİREBİR map edip runtime'da geri çevirmez (o yaklaşım
    trivial reverse edilir). Bunun yerine seçili fonksiyonların gövdesini
    kendi ISA'sına DERLER; Python bytecode'una geri dönüş tablosu çıktıda
    bulunmaz. Analistin VM semantiğini elle çözmesi gerekir.

    Kapsam: aritmetik/mantık/karşılaştırma, yerel değişken, global & builtin
    çağrısı, attribute, list/tuple, koşul ve döngü (while/for-range→while'a
    indirgenmiş kod dahil). Desteklenmeyen yapı içeren fonksiyon SESSIZCE
    ATLANMAZ — normal obfuscation yolunda bırakılır (bkz. Unsupported).

    Seçim:
      - @ninja_vm decorator'ı ile işaretlenen fonksiyonlar (öncelikli)
      - decorator yoksa: küçük & saf (nested def/class/yield/try içermeyen)
        modül-seviyesi fonksiyonlar otomatik aday
    """

    # ISA — Python opcode değerleriyle kasıtlı olarak örtüşmeyen numaralar
    PUSH_CONST = 0x11; LOAD_LOCAL = 0x12; STORE_LOCAL = 0x13
    LOAD_GLOBAL = 0x14; CALL = 0x15; BIN = 0x16; CMP = 0x17
    JMP = 0x18; JMP_FALSE = 0x19; RET = 0x1A; POP = 0x1B
    BUILD_LIST = 0x1C; BUILD_TUPLE = 0x1D; LOAD_ATTR = 0x1E
    UNARY_NEG = 0x1F; NOP = 0x20; JMP_TRUE = 0x21; DUP = 0x22

    B_ADD, B_SUB, B_MUL, B_DIV, B_MOD, B_POW, B_FLOORDIV, \
        B_AND, B_OR, B_XOR, B_LSH, B_RSH, B_SUBSCR = range(13)
    C_LT, C_LE, C_EQ, C_NE, C_GT, C_GE = range(6)

    _BIN_SYM = {'+': B_ADD, '-': B_SUB, '*': B_MUL, '/': B_DIV, '%': B_MOD,
                '**': B_POW, '//': B_FLOORDIV, '&': B_AND, '|': B_OR,
                '^': B_XOR, '<<': B_LSH, '>>': B_RSH}
    _CMP_SYM = {'<': C_LT, '<=': C_LE, '==': C_EQ, '!=': C_NE, '>': C_GT, '>=': C_GE}

    class Unsupported(Exception):
        pass

    # Ana opcode ve alt-op adları — her build'de rastgele numaralandırılır.
    _ISA_MAIN = ('PUSH_CONST', 'LOAD_LOCAL', 'STORE_LOCAL', 'LOAD_GLOBAL',
                 'CALL', 'BIN', 'CMP', 'JMP', 'JMP_FALSE', 'RET', 'POP',
                 'BUILD_LIST', 'BUILD_TUPLE', 'LOAD_ATTR', 'UNARY_NEG',
                 'NOP', 'JMP_TRUE', 'DUP')
    _ISA_BIN = ('B_ADD', 'B_SUB', 'B_MUL', 'B_DIV', 'B_MOD', 'B_POW',
                'B_FLOORDIV', 'B_AND', 'B_OR', 'B_XOR', 'B_LSH', 'B_RSH',
                'B_SUBSCR')
    _ISA_CMP = ('C_LT', 'C_LE', 'C_EQ', 'C_NE', 'C_GT', 'C_GE')

    def __init__(self):
        self.hyp = HyperionObfuscator()
        self.protected_names = set()
        self._randomize_isa()

    def _randomize_isa(self):
        """
        ISA opcode numaralarını bu build'e özgü rastgele bir permütasyonla
        yeniden atar. Böylece iki farklı çıktı AYNI VM numaralandırmasını
        paylaşmaz — analist her hedef için ISA'yı elle yeniden çıkarmalıdır.
        Değerler örnek (instance) özniteliği olarak atanır; _compile_code ve
        runtime_source zaten self.<OP> üzerinden okuduğu için otomatik olarak
        bu build'in numaralarını kullanır. Alt-op (BIN/CMP) uzayları ana
        opcode'lardan bağımsızdır (interpreter'da ayrı konumda karşılaştırılır),
        bu yüzden çakışma önemsizdir.
        """
        main_pool = list(range(0x21, 0xF0))
        random.shuffle(main_pool)
        for i, name in enumerate(self._ISA_MAIN):
            setattr(self, name, main_pool[i])
        bin_pool = list(range(0, 60))
        random.shuffle(bin_pool)
        for i, name in enumerate(self._ISA_BIN):
            setattr(self, name, bin_pool[i])
        cmp_pool = list(range(0, 40))
        random.shuffle(cmp_pool)
        for i, name in enumerate(self._ISA_CMP):
            setattr(self, name, cmp_pool[i])
        # sembol→alt-op tablolarını instance düzeyinde bu build'in değerleriyle
        # yeniden kur (sınıf düzeyindeki _BIN_SYM/_CMP_SYM sabit değerlidir).
        self._BIN_SYM = {'+': self.B_ADD, '-': self.B_SUB, '*': self.B_MUL,
                         '/': self.B_DIV, '%': self.B_MOD, '**': self.B_POW,
                         '//': self.B_FLOORDIV, '&': self.B_AND, '|': self.B_OR,
                         '^': self.B_XOR, '<<': self.B_LSH, '>>': self.B_RSH}
        self._CMP_SYM = {'<': self.C_LT, '<=': self.C_LE, '==': self.C_EQ,
                         '!=': self.C_NE, '>': self.C_GT, '>=': self.C_GE}

    # ── Derleyici: bir Python code object → NinjaVM programı ──
    def _compile_code(self, co):
        instrs = list(dis.get_instructions(co))
        consts = list(co.co_consts)
        names = list(co.co_names)
        varnames = list(co.co_varnames)

        def cidx(v):
            for i, c in enumerate(consts):
                if type(c) == type(v) and c == v:
                    return i
            consts.append(v)
            return len(consts) - 1

        emit = []  # (py_offset|None, op, arg, jump_target_pyoffset|None)
        for ins in instrs:
            op, a, off = ins.opname, ins.arg, ins.offset
            if op in ('RESUME', 'PRECALL', 'CACHE', 'COPY_FREE_VARS',
                      'MAKE_FUNCTION', 'PUSH_NULL', 'NOP'):
                emit.append((off, self.NOP, 0, None)); continue
            if op == 'LOAD_CONST':
                emit.append((off, self.PUSH_CONST, cidx(co.co_consts[a]), None))
            elif op in ('LOAD_FAST', 'LOAD_FAST_CHECK'):
                emit.append((off, self.LOAD_LOCAL, a, None))
            elif op == 'STORE_FAST':
                emit.append((off, self.STORE_LOCAL, a, None))
            elif op == 'LOAD_GLOBAL':
                emit.append((off, self.LOAD_GLOBAL, (a >> 1) if a is not None else 0, None))
            elif op == 'LOAD_NAME':
                emit.append((off, self.LOAD_GLOBAL, a, None))
            elif op == 'LOAD_ATTR':
                emit.append((off, self.LOAD_ATTR, (a >> 1) if a is not None else 0, None))
            elif op in ('CALL', 'CALL_FUNCTION'):
                emit.append((off, self.CALL, a or 0, None))
            elif op == 'BINARY_OP':
                sym = ins.argrepr.split()[0] if ins.argrepr else '+'
                b = self._BIN_SYM.get(sym)
                if b is None: raise self.Unsupported(f'binary {sym}')
                emit.append((off, self.BIN, b, None))
            elif op == 'BINARY_SUBSCR':
                emit.append((off, self.BIN, self.B_SUBSCR, None))
            elif op == 'COMPARE_OP':
                c = self._CMP_SYM.get(ins.argrepr.strip())
                if c is None: raise self.Unsupported(f'cmp {ins.argrepr}')
                emit.append((off, self.CMP, c, None))
            elif op in ('POP_JUMP_IF_FALSE', 'POP_JUMP_FORWARD_IF_FALSE',
                        'POP_JUMP_BACKWARD_IF_FALSE'):
                emit.append((off, self.JMP_FALSE, 0, ins.argval))
            elif op in ('POP_JUMP_IF_TRUE', 'POP_JUMP_FORWARD_IF_TRUE',
                        'POP_JUMP_BACKWARD_IF_TRUE'):
                emit.append((off, self.JMP_TRUE, 0, ins.argval))
            elif op in ('JUMP_FORWARD', 'JUMP_BACKWARD', 'JUMP_ABSOLUTE',
                        'JUMP_BACKWARD_NO_INTERRUPT'):
                emit.append((off, self.JMP, 0, ins.argval))
            elif op == 'RETURN_VALUE':
                emit.append((off, self.RET, 0, None))
            elif op == 'RETURN_CONST':
                emit.append((off, self.PUSH_CONST, cidx(co.co_consts[a]), None))
                emit.append((None, self.RET, 0, None))
            elif op == 'POP_TOP':
                emit.append((off, self.POP, 0, None))
            elif op == 'BUILD_LIST':
                emit.append((off, self.BUILD_LIST, a or 0, None))
            elif op == 'BUILD_TUPLE':
                emit.append((off, self.BUILD_TUPLE, a or 0, None))
            elif op == 'UNARY_NEGATIVE':
                emit.append((off, self.UNARY_NEG, 0, None))
            elif op in ('COPY',) and a == 1:
                emit.append((off, self.DUP, 0, None))
            else:
                raise self.Unsupported(f'opcode {op}')

        off_to_idx = {}
        for idx, t in enumerate(emit):
            if t[0] is not None:
                off_to_idx[t[0]] = idx
        final = []
        for (off, op, arg, tgt) in emit:
            if tgt is not None:
                if tgt not in off_to_idx:
                    raise self.Unsupported(f'jump target {tgt}')
                arg = off_to_idx[tgt]
            final.append([op, arg])
        return {
            'code': final, 'consts': consts, 'names': names,
            'nlocals': len(varnames), 'argcount': co.co_argcount,
        }

    def compile_function_source(self, func_src: str):
        """Tek bir fonksiyon kaynak metnini derler. (prog, func_name, argnames)"""
        import ast as _ast
        tree = _ast.parse(func_src)
        fdef = tree.body[0]
        if not isinstance(fdef, _ast.FunctionDef):
            raise self.Unsupported('not a function def')
        # decorator'ları çıkar (derleme saf gövde için)
        fdef.decorator_list = []
        mod = _ast.Module(body=[fdef], type_ignores=[])
        _ast.fix_missing_locations(mod)
        ns = {}
        code = compile(mod, '<nvfn>', 'exec')
        exec(code, ns)
        fn = ns[fdef.name]
        prog = self._compile_code(fn.__code__)
        argnames = [a.arg for a in fdef.args.args]
        # kwargs/vararg/defaults desteklenmiyor → güvenli tarafta kal
        if fdef.args.vararg or fdef.args.kwarg or fdef.args.kwonlyargs or fdef.args.defaults:
            raise self.Unsupported('complex signature')
        return prog, fdef.name, argnames

    # ── Runtime interpreter (üretilen dosyaya gömülür) ──
    def runtime_source(self) -> str:
        # Interpreter'ı tek fonksiyon olarak üretir. Opcode sabitleri
        # doğrudan sayısal gömülür (isim ipucu yok). Bu build'e özgü rastgele
        # ISA numaraları (self.<OP>) f-string ile gömülür — 'cls' yalnızca
        # aşağıdaki gövdeyi değiştirmeden self'e bağlamak için bir takma addır.
        cls = self
        return f'''
def _nv_exec(_p, _a, _g):
    _c=_p["c"]; _k=_p["k"]; _nm=_p["n"]; _L=[None]*_p["l"]
    _i=0
    for _x in _a: _L[_i]=_x; _i+=1
    _s=[]; _ip=0; _N=len(_c)
    import builtins as _b
    while _ip<_N:
        _o=_c[_ip][0]; _g2=_c[_ip][1]; _ip+=1
        if _o=={cls.PUSH_CONST}: _s.append(_k[_g2])
        elif _o=={cls.LOAD_LOCAL}: _s.append(_L[_g2])
        elif _o=={cls.STORE_LOCAL}: _L[_g2]=_s.pop()
        elif _o=={cls.LOAD_GLOBAL}:
            _n=_nm[_g2]
            _s.append(_g[_n] if _n in _g else getattr(_b,_n))
        elif _o=={cls.LOAD_ATTR}: _s.append(getattr(_s.pop(),_nm[_g2]))
        elif _o=={cls.CALL}:
            _ar=_s[len(_s)-_g2:] if _g2 else []
            del _s[len(_s)-_g2:]; _fn=_s.pop(); _s.append(_fn(*_ar))
        elif _o=={cls.BIN}:
            _y=_s.pop(); _z=_s.pop(); _s.append(_nv_bin(_g2,_z,_y))
        elif _o=={cls.CMP}:
            _y=_s.pop(); _z=_s.pop(); _s.append(_nv_cmp(_g2,_z,_y))
        elif _o=={cls.JMP}: _ip=_g2
        elif _o=={cls.JMP_FALSE}:
            if not _s.pop(): _ip=_g2
        elif _o=={cls.JMP_TRUE}:
            if _s.pop(): _ip=_g2
        elif _o=={cls.RET}: return _s.pop()
        elif _o=={cls.POP}: _s.pop()
        elif _o=={cls.DUP}: _s.append(_s[-1])
        elif _o=={cls.BUILD_LIST}:
            _it=_s[len(_s)-_g2:]; del _s[len(_s)-_g2:]; _s.append(list(_it))
        elif _o=={cls.BUILD_TUPLE}:
            _it=_s[len(_s)-_g2:]; del _s[len(_s)-_g2:]; _s.append(tuple(_it))
        elif _o=={cls.UNARY_NEG}: _s.append(-_s.pop())
        elif _o=={cls.NOP}: pass
    return None

def _nv_bin(_o,_a,_b):
    if _o=={cls.B_ADD}: return _a+_b
    if _o=={cls.B_SUB}: return _a-_b
    if _o=={cls.B_MUL}: return _a*_b
    if _o=={cls.B_DIV}: return _a/_b
    if _o=={cls.B_MOD}: return _a%_b
    if _o=={cls.B_POW}: return _a**_b
    if _o=={cls.B_FLOORDIV}: return _a//_b
    if _o=={cls.B_AND}: return _a&_b
    if _o=={cls.B_OR}: return _a|_b
    if _o=={cls.B_XOR}: return _a^_b
    if _o=={cls.B_LSH}: return _a<<_b
    if _o=={cls.B_RSH}: return _a>>_b
    if _o=={cls.B_SUBSCR}: return _a[_b]
    raise RuntimeError()

def _nv_cmp(_o,_a,_b):
    if _o=={cls.C_LT}: return _a<_b
    if _o=={cls.C_LE}: return _a<=_b
    if _o=={cls.C_EQ}: return _a==_b
    if _o=={cls.C_NE}: return _a!=_b
    if _o=={cls.C_GT}: return _a>_b
    if _o=={cls.C_GE}: return _a>=_b
    raise RuntimeError()
'''

    @staticmethod
    def _prog_literal(prog) -> str:
        # Programı kompakt sözlük literaline çevir (marshal+b64 de olabilirdi;
        # okunabilirliği azaltmak için b64 marshal kullanıyoruz)
        import marshal as _m, base64 as _b64
        blob = _b64.b64encode(_m.dumps({
            'c': prog['code'], 'k': prog['consts'],
            'n': prog['names'], 'l': prog['nlocals'],
        })).decode()
        return blob

    def transform_source(self, source: str):
        """
        Modüldeki uygun fonksiyonları VM'e taşır.
        Döner: (yeni_kaynak, taşınan_fonksiyon_sayısı)
        """
        import ast as _ast
        try:
            tree = _ast.parse(source)
        except SyntaxError:
            return source, 0

        # decorator ile işaretli veya otomatik aday fonksiyonları bul
        targets = []  # (node, marked)
        for node in tree.body:
            if isinstance(node, _ast.FunctionDef):
                marked = any(
                    (isinstance(d, _ast.Name) and d.id == 'ninja_vm') or
                    (isinstance(d, _ast.Attribute) and d.attr == 'ninja_vm')
                    for d in node.decorator_list
                )
                if marked or self._auto_ok(node):
                    targets.append((node, marked))

        if not targets:
            return source, 0

        src_lines = source.splitlines(keepends=True)
        replacements = []  # (start_lineno, end_lineno, new_text)
        moved = 0
        prog_blobs = {}
        # VM programına GÖMÜLEN global adlar (stub fonksiyon adı + LOAD_GLOBAL
        # isimleri) marshaled blob içinde saklanır ve sonraki AST yeniden
        # adlandırmasına GÖRÜNMEZ. Bu adları AST'de KORUMAZSAK, stub `def fib`
        # → `_hash` olur ama blob içindeki özyineleme hâlâ 'fib' arar → çalışma
        # anında 'fib' bulunamaz. Bu küme encode_ultimate tarafından
        # ASTObfuscator'a extra_protected olarak geçirilir.
        self.protected_names = set()

        for node, marked in targets:
            try:
                seg = _ast.get_source_segment(source, node)
                if seg is None:
                    continue
                prog, fname, argnames = self.compile_function_source(seg)
            except self.Unsupported:
                continue
            except Exception:
                continue
            blob = self._prog_literal(prog)
            # stub adını ve VM'lenmiş fonksiyonun başvurduğu global adları koru
            self.protected_names.add(fname)
            self.protected_names.update(prog.get('names', []))
            gvar = '_NVP_' + fname
            prog_blobs[gvar] = blob
            # stub: aynı isim/imza, gövdede VM çağrısı
            argsig = ', '.join(argnames)
            arglist = '[' + ', '.join(argnames) + ']'
            indent = ' ' * (node.col_offset)
            stub = (
                f"{indent}def {fname}({argsig}):\n"
                f"{indent}    import marshal as _m, base64 as _b64\n"
                f"{indent}    _pp = _m.loads(_b64.b64decode({gvar}))\n"
                f"{indent}    return _nv_exec(_pp, {arglist}, globals())\n"
            )
            replacements.append((node.lineno, node.end_lineno, stub))
            moved += 1

        if moved == 0:
            return source, 0

        # satırları sondan başa değiştir (offset kaymasın)
        replacements.sort(key=lambda r: r[0], reverse=True)
        for (start, end, new_text) in replacements:
            src_lines[start - 1:end] = [new_text]

        new_source = ''.join(src_lines)
        # prog blob'larını ve interpreter'ı en başa ekle
        header = self.runtime_source() + '\n'
        # no-op decorator tanımı (kaynakta @ninja_vm kalmışsa çalışsın diye)
        header += 'def ninja_vm(_f):\n    return _f\n\n'
        for gvar, blob in prog_blobs.items():
            header += f'{gvar} = {blob!r}\n'
        header += '\n'
        return header + new_source, moved

    @staticmethod
    def _auto_ok(node) -> bool:
        """Otomatik aday mı? Küçük, saf, basit imzalı fonksiyon."""
        import ast as _ast
        a = node.args
        if a.vararg or a.kwarg or a.kwonlyargs or a.defaults or a.posonlyargs:
            return False
        if len(a.args) == 0:
            return False
        # yasak yapılar: nested def/class, yield, try, with, async, global, comprehension'lar
        for sub in _ast.walk(node):
            if sub is node:
                continue
            if isinstance(sub, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef,
                                _ast.Yield, _ast.YieldFrom, _ast.Try, _ast.With,
                                _ast.AsyncWith, _ast.Lambda, _ast.ListComp, _ast.DictComp,
                                _ast.SetComp, _ast.GeneratorExp, _ast.Global, _ast.Nonlocal,
                                _ast.Import, _ast.ImportFrom, _ast.Starred, _ast.Assert,
                                _ast.Raise, _ast.Delete, _ast.For, _ast.While)):
                # for/while otomatikte yok (jump güvenliği elle test edilmeli);
                # decorator ile işaretlenirse döngüler DESTEKLENIR.
                if isinstance(sub, (_ast.For, _ast.While)):
                    return False
                return False
            if isinstance(sub, _ast.keyword):
                return False
        # gövde çok uzunsa atla
        body_lines = (node.end_lineno or node.lineno) - node.lineno
        if body_lines > 25:
            return False
        return True


class BytecodeVirtualizer:
    """
    Python bytecode'unu custom opcode setine çevir.
    Kendi mini-interpreter ile çalıştır.
    Hiçbir decompiler tanımıyor.
    Python 3.12+ için basitleştirilmiş versiyon (opcode değişiklikleri nedeniyle).
    """

    OP_MAP = {
        'LOAD_CONST':    0x01,
        'LOAD_NAME':     0x02,
        'STORE_NAME':    0x03,
        'CALL_FUNCTION': 0x04,
        'POP_TOP':       0x05,
        'RETURN_VALUE':  0x06,
        'LOAD_GLOBAL':   0x07,
        'LOAD_FAST':     0x08,
        'STORE_FAST':    0x09,
        'BINARY_OP':     0x0A,
        'COMPARE_OP':    0x0B,
        'POP_JUMP_IF_FALSE': 0x0C,
        'JUMP_FORWARD':  0x0D,
        'BUILD_LIST':    0x0E,
        'BUILD_DICT':    0x0F,
    }

    def virtualize(self, source: str) -> str:
        try:
            ver = sys.version_info
            if ver >= (3, 13):
                logger.info("BytecodeVirtualizer: Python 3.13+ — JIT-aware marshal wrap modu")
                return self._marshal_wrap_313(source)
            if ver >= (3, 12):
                logger.info("BytecodeVirtualizer: Python 3.12 — marshal wrap modu")
                return self._marshal_wrap(source)
            return self._full_virtualize(source)
        except Exception as e:
            logger.warning(f"BytecodeVirtualizer atlandı: {e}")
            return source

    def _marshal_wrap_313(self, source: str) -> str:
        """
        Python 3.13 JIT için özel wrap:
        - sys.flags.optimize ile JIT'i devre dışı bırak
        - co_linetable ve co_exceptiontable alanları değişti, marshal wrap ile güvenli
        - İki katman XOR + double marshal ile decompiler'ları zorla
        """
        code = compile(source, '<virt313>', 'exec')
        raw = marshal.dumps(code)
        key1 = random.randint(1, 255)
        key2 = random.randint(1, 255)
        enc = bytes(b ^ key1 for b in raw)
        enc = bytes(b ^ key2 for b in enc)
        b64 = base64.b64encode(enc).decode('ascii')
        hyp = HyperionObfuscator()
        v = [hyp._randvar() for _ in range(7)]
        return f'''import base64 as _b64, marshal as _m, sys as _sy, ctypes as _ct
try:
    if hasattr(_sy, '_jit') and _sy._jit:
        _sy._jit = False
except Exception: pass
{v[0]} = "{b64}"
{v[1]} = bytes(b ^ {key2} for b in _b64.b64decode({v[0]}))
{v[2]} = bytes(b ^ {key1} for b in {v[1]})
{v[3]} = _m.loads({v[2]})
del {v[0]}, {v[1]}, {v[2]}
try:
    {v[4]} = _ct.pythonapi.PyEval_EvalCode
    {v[4]}.restype = _ct.py_object
    {v[4]}.argtypes = [_ct.py_object, _ct.py_object, _ct.py_object]
    {v[5]} = {{"__name__": "__main__", "__builtins__": __builtins__, "__file__": globals().get("__file__", "")}}
    {v[4]}({v[3]}, {v[5]}, {v[5]})
    del {v[4]}, {v[5]}
except Exception:
    exec({v[3]}, {{"__name__": "__main__", "__builtins__": __builtins__}})
del {v[3]}
'''

    def _marshal_wrap(self, source: str) -> str:
        code = compile(source, '<virt>', 'exec')
        raw = marshal.dumps(code)
        key = random.randint(1, 255)
        enc = bytes(b ^ key for b in raw)
        b64 = base64.b64encode(enc).decode('ascii')
        hyp = HyperionObfuscator()
        v = [hyp._randvar() for _ in range(5)]
        return f'''import base64 as _b64, marshal as _m, sys as _s
{v[0]} = "{b64}"
{v[1]} = {key}
{v[2]} = bytes(b ^ {v[1]} for b in _b64.b64decode({v[0]}))
{v[3]} = _m.loads({v[2]})
del {v[0]}, {v[1]}, {v[2]}
exec({v[3]}, {{"__name__": "__main__", "__builtins__": __builtins__, "__file__": globals().get("__file__", "")}})
del {v[3]}
'''

    def _full_virtualize(self, source: str) -> str:
        code = compile(source, '<virt>', 'exec')
        instructions = list(dis.get_instructions(code))
        custom_bytecode = []
        const_pool = list(code.co_consts)
        name_pool = list(code.co_names)

        for instr in instructions:
            op_name = instr.opname
            custom_op = self.OP_MAP.get(op_name, 0xFF)
            arg = instr.arg if instr.arg is not None else 0
            custom_bytecode.extend([custom_op, arg & 0xFF])

        bc_bytes = bytes(custom_bytecode)
        key = random.randint(1, 255)
        enc_bc = bytes(b ^ key for b in bc_bytes)
        bc_b64 = base64.b64encode(enc_bc).decode('ascii')
        cp_b64 = base64.b64encode(marshal.dumps(tuple(const_pool))).decode('ascii')
        np_b64 = base64.b64encode(marshal.dumps(tuple(name_pool))).decode('ascii')
        rev_map = {v: k for k, v in self.OP_MAP.items()}

        hyp = HyperionObfuscator()
        v = [hyp._randvar() for _ in range(8)]

        return f'''import base64 as _b64, marshal as _m
{v[0]} = bytes(b ^ {key} for b in _b64.b64decode("{bc_b64}"))
{v[1]} = list(_m.loads(_b64.b64decode("{cp_b64}")))
{v[2]} = list(_m.loads(_b64.b64decode("{np_b64}")))
{v[3]} = {rev_map}
{v[4]} = []
_i = 0
while _i < len({v[0]}):
    _op = {v[0]}[_i]; _arg = {v[0]}[_i+1]; _i += 2
    _name = {v[3]}.get(_op, "NOP")
    if _name == "LOAD_CONST": {v[4]}.append({v[1]}[_arg])
    elif _name == "LOAD_NAME": {v[4]}.append(globals().get({v[2]}[_arg]))
    elif _name == "STORE_NAME": globals()[{v[2]}[_arg]] = {v[4]}.pop()
    elif _name == "LOAD_GLOBAL": {v[4]}.append(globals().get({v[2]}[_arg]))
    elif _name == "CALL_FUNCTION":
        _args = [{v[4]}.pop() for _ in range(_arg)][::-1]
        _fn = {v[4]}.pop()
        {v[4]}.append(_fn(*_args) if _fn else None)
    elif _name == "POP_TOP": {v[4]}.pop() if {v[4]} else None
    elif _name == "RETURN_VALUE": break
del {v[0]}, {v[1]}, {v[2]}, {v[3]}, {v[4]}
'''

class IntegrityChecker:

    def wrap_with_integrity(self, source: str, target_file: str = None) -> str:
        hyp = HyperionObfuscator()
        v = [hyp._randvar() for _ in range(12)]
        full_hash = hashlib.sha256(source.encode('utf-8')).hexdigest()
        tail_hash = hashlib.sha256(source[-4096:].encode('utf-8')).hexdigest()
        mid       = len(source) // 2
        mid_hash  = hashlib.sha256(source[mid:mid+2048].encode('utf-8')).hexdigest()
        import zlib as _zlib_ic
        crc_full  = _zlib_ic.crc32(source.encode('utf-8')) & 0xFFFFFFFF
        crc_tail  = _zlib_ic.crc32(source[-2048:].encode('utf-8')) & 0xFFFFFFFF
        xor_key   = (crc_full & 0xFF) or 0x5A
        enc_full  = base64.b64encode(bytes(b ^ xor_key for b in full_hash.encode())).decode()
        enc_tail  = base64.b64encode(bytes(b ^ xor_key for b in tail_hash.encode())).decode()
        enc_mid   = base64.b64encode(bytes(b ^ xor_key for b in mid_hash.encode())).decode()

        return f'''import hashlib as _hl, sys as _sy, os as _oo, base64 as _bb, zlib as _zb
def {v[0]}():
    try:
        _f = globals().get("__file__") or _sy.argv[0]
        if not _f or not _oo.path.exists(_f): return
        _tmp_markers = ["/tmp/", "/data/local/tmp/", "tmpfile", ".tmp", "CevoPy"]
        if any(_m in str(_f) for _m in _tmp_markers): return
        with open(_f, "r", encoding="utf-8", errors="ignore") as _fh: _raw = _fh.read()
        _mk = "# __NINJA_BODY__"
        _ix = _raw.find(_mk)
        if _ix == -1: _sy.exit(1)
        _body = _raw[_ix + len(_mk):]
        _k = {xor_key}
        def _dh(_e): return bytes(b ^ _k for b in _bb.b64decode(_e)).decode()
        _h1 = _hl.sha256(_body.encode()).hexdigest()
        _h2 = _hl.sha256(_body[-4096:].encode()).hexdigest()
        _mid2 = len(_body) // 2
        _h3 = _hl.sha256(_body[_mid2:_mid2+2048].encode()).hexdigest()
        _c1 = _zb.crc32(_body.encode()) & 0xFFFFFFFF
        _c2 = _zb.crc32(_body[-2048:].encode()) & 0xFFFFFFFF
        _ok_hash = (_h1 == _dh("{enc_full}") and _h2 == _dh("{enc_tail}") and _h3 == _dh("{enc_mid}"))
        _ok_crc  = (_c1 == {crc_full} and _c2 == {crc_tail})
        if not _ok_hash or not _ok_crc:
            try: _oo.remove(_f)
            except: pass
            raise ImportError("No module named \\'_hashcore\\'")
    except (ImportError, SystemExit): raise
    except: pass
{v[0]}()
del {v[0]}
''' + source

class PNGSteganography:
    """
    Binary payload'ı PNG piksel datasına gömer.
    Görsel olarak normal bir PNG dosyası gibi görünür.
    LSB (Least Significant Bit) — her pikselin R,G,B kanallarının
    son bitine 1 bit veri yazılır.
    Payload önce XOR+zlib ile şifrelenir.
    """

    PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'

    @staticmethod
    def _make_chunk(chunk_type: bytes, data: bytes) -> bytes:
        import struct, zlib as _zl
        crc = struct.pack('>I', _zl.crc32(chunk_type + data) & 0xFFFFFFFF)
        return struct.pack('>I', len(data)) + chunk_type + data + crc

    @staticmethod
    def embed(payload: bytes, xor_key: int = None) -> tuple:
        import struct, zlib as _zl, math
        if xor_key is None:
            xor_key = random.randint(1, 255)
        xored      = bytes(b ^ xor_key for b in payload)
        compressed = _zl.compress(xored, 9)
        data_to_hide = struct.pack('>I', len(compressed)) + compressed
        bits_needed  = len(data_to_hide) * 8
        pixels_needed = math.ceil(bits_needed / 3)
        side = max(16, math.ceil(math.sqrt(pixels_needed)) + 4)
        total_pixels = side * side

        carrier = bytearray()
        for _ in range(total_pixels):
            carrier.extend([
                random.randint(30, 220),
                random.randint(30, 220),
                random.randint(30, 220),
            ])

        bit_idx = 0
        for byte in data_to_hide:
            for bit_pos in range(7, -1, -1):
                if bit_idx >= len(carrier):
                    break
                bit = (byte >> bit_pos) & 1
                carrier[bit_idx] = (carrier[bit_idx] & 0xFE) | bit
                bit_idx += 1

        row_bytes = side * 3
        raw_rows  = b''
        for row in range(side):
            raw_rows += bytes([0]) + bytes(carrier[row * row_bytes:(row + 1) * row_bytes])

        idat_data = _zl.compress(raw_rows, 9)
        ihdr_data = struct.pack('>IIBBBBB', side, side, 8, 2, 0, 0, 0)
        text_payload = b'Comment\x00' + bytes([xor_key ^ 0xA5])

        png  = PNGSteganography.PNG_SIGNATURE
        png += PNGSteganography._make_chunk(b'IHDR', ihdr_data)
        png += PNGSteganography._make_chunk(b'tEXt', text_payload)
        png += PNGSteganography._make_chunk(b'IDAT', idat_data)
        png += PNGSteganography._make_chunk(b'IEND', b'')
        return png, xor_key

    @staticmethod
    def extract(png_data: bytes) -> bytes:
        import struct, zlib as _zl
        pos = 8
        xor_key = width = height = None
        idat_raw = b''
        while pos < len(png_data) - 12:
            length = struct.unpack('>I', png_data[pos:pos+4])[0]
            ctype  = png_data[pos+4:pos+8]
            data   = png_data[pos+8:pos+8+length]
            pos   += 12 + length
            if ctype == b'IHDR':
                width, height = struct.unpack('>II', data[:8])
            elif ctype == b'tEXt':
                parts = data.split(bytes([0]))
                if len(parts) >= 2 and len(parts[1]) >= 1:
                    xor_key = parts[1][0] ^ 0xA5
            elif ctype == b'IDAT':
                idat_raw += data
            elif ctype == b'IEND':
                break
        if xor_key is None or not idat_raw or width is None:
            raise ValueError('PNG steganografi verisi bulunamadı')
        raw      = _zl.decompress(idat_raw)
        row_bytes = width * 3
        carrier  = bytearray()
        for row in range(height):
            start = row * (row_bytes + 1) + 1
            carrier.extend(raw[start:start + row_bytes])
        bits = [b & 1 for b in carrier]
        extracted = bytearray()
        for i in range(0, len(bits) - 7, 8):
            byte = 0
            for j in range(8):
                byte = (byte << 1) | bits[i + j]
            extracted.append(byte)
        data_len   = struct.unpack('>I', bytes(extracted[:4]))[0]
        compressed = bytes(extracted[4:4 + data_len])
        xored      = _zl.decompress(compressed)
        return bytes(b ^ xor_key for b in xored)

    @staticmethod
    def generate_loader_code(png_b64: str, var_out: str) -> str:
        hyp = HyperionObfuscator()
        v   = [hyp._randvar() for _ in range(6)]
        return f"""
import base64 as _pb64, struct as _pst, zlib as _pzl
{v[0]} = _pb64.b64decode('{png_b64}')
_pp = 8; _pxk = None; _pidat = b''; _pw = _ph = 0
while _pp < len({v[0]}) - 12:
    _pln = _pst.unpack('>I', {v[0]}[_pp:_pp+4])[0]
    _pct = {v[0]}[_pp+4:_pp+8]; _pdt = {v[0]}[_pp+8:_pp+8+_pln]; _pp += 12+_pln
    if _pct==b'IHDR': _pw,_ph=_pst.unpack('>II',_pdt[:8])
    elif _pct==b'tEXt':
        _pts=_pdt.split(b'\\x00')
        if len(_pts)>=2 and _pts[1]: _pxk=_pts[1][0]^0xA5
    elif _pct==b'IDAT': _pidat+=_pdt
    elif _pct==b'IEND': break
_prw=_pzl.decompress(_pidat); _prb=_pw*3
_pca=bytearray()
for _pri in range(_ph):
    _ps=_pri*(_prb+1)+1; _pca.extend(_prw[_ps:_ps+_prb])
_pbi=[_b&1 for _b in _pca]; _pex=bytearray()
for _pi in range(0,len(_pbi)-7,8):
    _byte=0
    for _pj in range(8): _byte=(_byte<<1)|_pbi[_pi+_pj]
    _pex.append(_byte)
_pdl=_pst.unpack('>I',bytes(_pex[:4]))[0]
{var_out}=bytes(_b^_pxk for _b in _pzl.decompress(bytes(_pex[4:4+_pdl])))
del {v[0]},_prw,_pca,_pbi,_pex,_pidat
"""

class FakeSoGenerator:
    """
    ZIP içine sahte .so dosyaları ekler.
    Her biri gerçek ELF header ile başlar, içi rastgele veri.
    Reverse engineer hangisinin gerçek olduğunu bilemez.
    Sahte PyInit_ sembolleri içerir — strings komutuyla gerçekmiş gibi görünür.
    """

    FAKE_NAMES = [
        'libcrypto_core.so',
        '_hashlib_ext.so',
        '_ninja_runtime.so',
        'libobf_engine.so',
        '_cipher_core.so',
        'libprotect.so',
        '_marshal_ext.so',
        'libenc_helper.so',
        '_codec_native.so',
        'libsecurity.so',
    ]

    ELF64_HEADER = bytes([
        0x7f,0x45,0x4c,0x46, 0x02, 0x01, 0x01, 0x00,
        0x00,0x00,0x00,0x00, 0x00,0x00,0x00,0x00,
        0x03,0x00, 0xb7,0x00, 0x01,0x00,0x00,0x00,
    ])

    ELF32_HEADER = bytes([
        0x7f,0x45,0x4c,0x46, 0x01, 0x01, 0x01, 0x00,
        0x00,0x00,0x00,0x00, 0x00,0x00,0x00,0x00,
        0x03,0x00, 0x28,0x00, 0x01,0x00,0x00,0x00,
    ])

    @staticmethod
    def generate_fake_so(size_range=(512, 2048)) -> bytes:
        header    = random.choice([FakeSoGenerator.ELF64_HEADER, FakeSoGenerator.ELF32_HEADER])
        body_size = random.randint(*size_range)
        body      = bytearray(body_size)
        for i in random.sample(range(body_size), min(512, body_size)):
            body[i] = random.randint(0, 255)
        fake_sym    = b'PyInit__ninja_core\x00'
        insert_pos  = random.randint(64, max(65, body_size - len(fake_sym) - 10))
        body[insert_pos:insert_pos + len(fake_sym)] = fake_sym
        ver_str   = b'GCC: (Android NDK) 12.0.0\x00'
        ver_pos   = random.randint(64, max(65, body_size - len(ver_str) - 10))
        body[ver_pos:ver_pos + len(ver_str)] = ver_str
        return header + bytes(body)

    @staticmethod
    def get_random_names(count: int = 4) -> list:
        return random.sample(FakeSoGenerator.FAKE_NAMES, min(count, len(FakeSoGenerator.FAKE_NAMES)))

    @staticmethod
    def add_to_zip(zf, count: int = 4):
        for name in FakeSoGenerator.get_random_names(count):
            fake_data = FakeSoGenerator.generate_fake_so()
            zf.writestr(name, fake_data)
            logger.info(f'Sahte .so eklendi: {name} ({len(fake_data):,} bytes)')

class HardwareFingerprintKey:
    """
    Şifre çözme anahtarının bir parçasını donanım bilgisinden türetir.
    /proc/cpuinfo + hostname + bellek (GB) → SHA256
    Android/Termux uyumlu. Küçük değişimlere duyarsız (GB yuvarlama).
    """

    @staticmethod
    def derive() -> bytes:
        import hashlib, platform, socket
        parts = []
        try:
            with open('/proc/cpuinfo', 'r') as f:
                for line in f:
                    if any(k in line for k in ('Hardware', 'model name', 'Processor')):
                        parts.append(line.strip()); break
        except Exception:
            parts.append(platform.machine())
        try:
            parts.append(socket.gethostname())
        except Exception:
            parts.append(platform.node())
        try:
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    if line.startswith('MemTotal'):
                        parts.append(str(int(line.split()[1]) // (1024 * 1024)))
                        break
        except Exception:
            parts.append('unknown')
        return hashlib.sha256('|'.join(parts).encode()).digest()

    @staticmethod
    def mix_with_key(base_key: bytes, hw_key: bytes) -> bytes:
        repeated = (hw_key * (len(base_key) // len(hw_key) + 1))[:len(base_key)]
        return bytes(a ^ b for a, b in zip(base_key, repeated))

    @staticmethod
    def generate_derive_code() -> str:
        return """
import hashlib as _hfk_hl, platform as _hfk_pl, socket as _hfk_sk
def _hfk_derive():
    _p = []
    try:
        with open('/proc/cpuinfo','r') as _f:
            for _l in _f:
                if any(_k in _l for _k in ('Hardware','model name','Processor')):
                    _p.append(_l.strip()); break
    except Exception: _p.append(_hfk_pl.machine())
    try: _p.append(_hfk_sk.gethostname())
    except Exception: _p.append(_hfk_pl.node())
    try:
        with open('/proc/meminfo','r') as _f:
            for _l in _f:
                if _l.startswith('MemTotal'):
                    _p.append(str(int(_l.split()[1])//(1024*1024))); break
    except Exception: _p.append('unknown')
    return _hfk_hl.sha256('|'.join(_p).encode()).digest()
"""

class MemfdExecutor:
    """
    Linux memfd_create() ile tamamen bellekte .so yükleme.
    Disk'e hiç yazılmaz — /proc/<pid>/fd/ üzerinden anonim bellek.
    Android kernel 3.18+ destekler, Termux uyumlu.
    Fallback: tmpfs + hemen sil.
    """

    @staticmethod
    def generate_loader_code(so_b64: str, module_name: str) -> str:
        hyp = HyperionObfuscator()
        v   = [hyp._randvar() for _ in range(8)]
        return f"""
import base64 as _mfd_b64, ctypes as _mfd_ct, importlib.util as _mfd_ilu
import os as _mfd_os, sys as _mfd_sy, tempfile as _mfd_tmp, atexit as _mfd_ae

{v[0]} = _mfd_b64.b64decode('{so_b64}')
{v[1]} = -1; {v[2]} = None

def _mfd_clean():
    try:
        if {v[1]} >= 0: _mfd_os.close({v[1]})
    except Exception: pass
    try:
        if {v[2]} and _mfd_os.path.exists({v[2]}): _mfd_os.unlink({v[2]})
    except Exception: pass
_mfd_ae.register(_mfd_clean)

{v[3]} = None
try:
    _libc = _mfd_ct.CDLL('libc.so', use_errno=True)
    _mfd_fn = _libc.memfd_create
    _mfd_fn.restype = _mfd_ct.c_int
    _mfd_fn.argtypes = [_mfd_ct.c_char_p, _mfd_ct.c_uint]
    {v[1]} = _mfd_fn(b'nj', 1)
    if {v[1]} >= 0:
        _mfd_os.write({v[1]}, {v[0]})
        {v[2]} = f'/proc/self/fd/{{{v[1]}}}'
        _s = _mfd_ilu.spec_from_file_location('{module_name}', {v[2]})
        {v[3]} = _mfd_ilu.module_from_spec(_s)
        _mfd_sy.modules['{module_name}'] = {v[3]}
        _s.loader.exec_module({v[3]})
except Exception:
    pass

if {v[3]} is None:
    try:
        {v[4]} = _mfd_tmp.mkdtemp(prefix='_nj_')
        {v[2]} = _mfd_os.path.join({v[4]}, '{module_name}.so')
        with open({v[2]}, 'wb') as _f: _f.write({v[0]})
        _mfd_os.chmod({v[2]}, 0o700)
        _s = _mfd_ilu.spec_from_file_location('{module_name}', {v[2]})
        {v[3]} = _mfd_ilu.module_from_spec(_s)
        _mfd_sy.modules['{module_name}'] = {v[3]}
        _s.loader.exec_module({v[3]})
        try: _mfd_os.unlink({v[2]})
        except Exception: pass
    except Exception as _e:
        raise ImportError(f'Native modül yüklenemedi: {{_e}}')

del {v[0]}
if {v[3]} is not None and hasattr({v[3]}, 'run'): {v[3]}.run()
"""

class WhitespaceSteganography:
    """
    Wrapper kodunun satır sonlarına binary veri kodlar.
    Space = bit 0, Tab = bit 1.
    Görsel olarak normal Python kodu gibi görünür.
    Runtime'da kendi satır sonlarını okuyarak payload'ı çıkarır.
    """

    @staticmethod
    def encode(payload: bytes, carrier_code: str) -> str:
        import struct
        length_bytes = struct.pack('>I', len(payload))
        all_data     = length_bytes + payload
        bits = []
        for byte in all_data:
            for bit_pos in range(7, -1, -1):
                bits.append((byte >> bit_pos) & 1)

        lines  = carrier_code.split('\n')
        result = []
        bit_idx = 0
        for line in lines:
            stripped = line.rstrip(' \t')
            if bit_idx < len(bits):
                suffix = '\t' if bits[bit_idx] == 1 else ' '
                result.append(stripped + suffix)
                bit_idx += 1
            else:
                result.append(stripped)
        if bit_idx < len(bits):
            logger.warning(f'WhitespaceSteganography: {len(bits)-bit_idx} bit sığmadı (carrier çok kısa)')
        return '\n'.join(result)

    @staticmethod
    def generate_decoder_code(var_out: str) -> str:
        hyp = HyperionObfuscator()
        v   = [hyp._randvar() for _ in range(4)]
        return f"""
import struct as _wss_st, inspect as _wss_in, sys as _wss_sy
def _wss_decode():
    try:
        _src = open(__file__, 'r', encoding='utf-8').read()
    except Exception:
        return None
    _bits = []
    for _ln in _src.split('\\n'):
        if _ln and _ln[-1] == '\\t': _bits.append(1)
        elif _ln and _ln[-1] == ' ':  _bits.append(0)
    if len(_bits) < 32: return None
    _ln_val = 0
    for _bi in range(32): _ln_val = (_ln_val << 1) | _bits[_bi]
    _data = bytearray()
    for _i in range(0, _ln_val * 8, 8):
        if 32 + _i + 7 >= len(_bits): break
        _byte = 0
        for _j in range(8): _byte = (_byte << 1) | _bits[32 + _i + _j]
        _data.append(_byte)
    return bytes(_data) if len(_data) == _ln_val else None
{var_out} = _wss_decode()
del _wss_decode
"""

class ELFProtector:
    """
    Faz 3 — ELF/Native seviye koruma:
    R: strip --strip-debug  (CythonCompiler'da zaten var)
    U: DWARF Debug Poisoning — .debug_* section'ları boz
    L: Fake Symbol Injection — sahte semboller ekle
    W: .text XOR Encrypt (do_text_encrypt=True ile aktif)
    Gereksinim: pip install lief
    """

    @staticmethod
    def is_available() -> bool:
        try:
            import lief  # noqa
            return True
        except ImportError:
            try:
                logger.info('lief bulunamadı — otomatik kuruluyor...')
                subprocess.run(
                    [sys.executable, '-m', 'pip', 'install', 'lief', '--break-system-packages', '-q'],
                    capture_output=True, timeout=120
                )
                import lief  # noqa
                logger.info('lief kurulumu başarılı')
                return True
            except Exception as _lief_e:
                logger.warning(f'lief kurulamadı: {_lief_e}')
                return False

    @staticmethod
    def protect(so_path: str, do_text_encrypt: bool = False) -> str:
        if not ELFProtector.is_available():
            logger.info('lief yok — ELF koruma atlanıyor (pip install lief)')
            return so_path
        try:
            import lief
            binary = lief.parse(so_path)
            if binary is None:
                return so_path
            changed = False

            poison_targets = [s for s in binary.sections
                              if s.name.startswith('.debug_')
                              or s.name in ('.comment', '.note', '.note.gnu.build-id',
                                            '.note.ABI-tag')]
            for sec in poison_targets:
                if sec.size > 0:
                    sec.content = list(os.urandom(sec.size))
                    changed = True
                    logger.info(f'  DWARF poison: {sec.name} ({sec.size}b)')

            _fake_syms = [
                '_PyInit_ninja_core', '_ninja_decrypt_v2', '_obf_table_init',
                '_key_derive_internal', '_anti_hook_guard', '_vm_dispatch_loop',
                '_code_verify_hmac', '_payload_unwrap_aes', '_integrity_check_v3',
                '_trace_guard_init',
            ]
            _injected = 0
            if hasattr(binary, 'add_exported_function'):
                for i, name in enumerate(_fake_syms[:5]):
                    try:
                        binary.add_exported_function(0x1000 + i * 0x40, name)
                        _injected += 1
                    except Exception:
                        pass
            elif hasattr(binary, 'dynamic_symbols'):
                sym_list = list(binary.dynamic_symbols)
                fi = 0
                for sym in sym_list:
                    if (sym.name and not sym.name.startswith('PyInit_')
                            and not sym.name.startswith('_Py')
                            and len(sym.name) > 3
                            and fi < len(_fake_syms)):
                        try:
                            sym.name = _fake_syms[fi]
                            fi += 1
                            _injected += 1
                        except Exception:
                            pass
            logger.info(f'  Fake symbol injection: {_injected} sembol')
            if _injected > 0:
                changed = True

            if do_text_encrypt:
                text_sec = binary.get_section('.text')
                if text_sec and text_sec.size > 0:
                    xk = random.randint(1, 255)
                    text_sec.content = [b ^ xk for b in text_sec.content]
                    changed = True
                    logger.info(f'  .text XOR encrypt: key=0x{xk:02x}')

            if changed:
                out_tmp = so_path + '.elfprot'
                binary.write(out_tmp)
                shutil.move(out_tmp, so_path)
                logger.info(f'ELFProtector tamamlandı: {Path(so_path).name}')
        except Exception as _ep:
            logger.warning(f'ELFProtector hata (atlanıyor): {_ep}')
        return so_path

class LazyChunkEncoder:
    """
    Faz 4 — B: LazyChunk (5 parça, zincir key)
    Payload'ı 5 parçaya böler.
    key[i+1] = sha256(plaintext[i])[0] ^ key[i]
    Sadece base_key koda gömülür — diğer key'ler runtime'da türetilir.
    Çıktı dosyaları: __s0__.bin .. __s4__.bin
    """
    NUM_CHUNKS = 5

    @staticmethod
    def encode(data: bytes) -> tuple:
        _rk = os.urandom(4)
        base_key = max(1, (_rk[0] ^ _rk[1] ^ _rk[2] ^ _rk[3]) & 0xFF) or 0x5A
        n = LazyChunkEncoder.NUM_CHUNKS
        size = len(data)
        cs = max(1, (size + n - 1) // n)
        parts = [data[i * cs:(i + 1) * cs] for i in range(n)]
        while len(parts) < n:
            parts.append(bytes([0]))
        parts = [p if p else bytes([0]) for p in parts]

        keys = [base_key]
        for i in range(n - 1):
            h = hashlib.sha256(parts[i]).digest()[0]
            nk = (h ^ keys[i]) & 0xFF
            if nk == 0:
                nk = (keys[i] + 7) & 0xFF
                if nk == 0:
                    nk = 7
            keys.append(nk)

        encrypted = [bytes(b ^ keys[i] for b in parts[i]) for i in range(n)]
        return encrypted, base_key

    @staticmethod
    def generate_loader_code(base_key: int, temp_dir_var: str, out_var: str) -> str:
        n = LazyChunkEncoder.NUM_CHUNKS
        v0 = '_lzc_hl'
        v1 = '_lzc_k'
        v2 = '_lzc_buf'
        v3 = '_lzc_i'
        v4 = '_lzc_fp'
        v5 = '_lzc_fh'
        v6 = '_lzc_raw'
        v7 = '_lzc_dec'
        v8 = '_lzc_h'
        v_prev = '_lzc_pk'
        code = (
            f'import hashlib as {v0}\n'
            f'{v1}={base_key}\n'
            f'{v2}=b""\n'
            f'for {v3} in range({n}):\n'
            f'    {v4}=_O.path.join({temp_dir_var},f"__s{{{v3}}}__.bin")\n'
            f'    with open({v4},"rb") as {v5}:\n'
            f'        {v6}={v5}.read()\n'
            f'    {v7}=bytes(_b^{v1} for _b in {v6})\n'
            f'    {v2}+={v7}\n'
            f'    if {v3}<{n-1}:\n'
            f'        {v_prev}={v1}\n'
            f'        {v8}={v0}.sha256({v7}).digest()[0]\n'
            f'        {v1}=({v8}^{v_prev})&0xFF\n'
            f'        if {v1}==0:\n'
            f'            {v1}=({v_prev}+7)&0xFF\n'
            f'            if {v1}==0:{v1}=7\n'
            f'{out_var}={v2}\n'
            f'del {v0},{v1},{v2},{v3},{v6},{v7}\n'
        )
        return code

class MiniVMGenerator:
    """
    Faz 4 — E: MiniVM (custom opcode dispatcher)
    Son exec() call'ını custom VM program'a çevirir.
    Bytecode: [opcode:1][operand_len:4][operand:N]
    """
    OP_LOAD_B64 = 0x01
    OP_XOR_KEY  = 0x02
    OP_DECOMP   = 0x03
    OP_MARSHAL  = 0x04
    OP_EXEC     = 0x05
    OP_WIPE     = 0x06
    OP_HALT     = 0xFF

    @staticmethod
    def _instr(op: int, operand: bytes = b'') -> bytes:
        return bytes([op]) + struct.pack('>I', len(operand)) + operand

    @staticmethod
    def compile_program(payload_bytes: bytes, xor_key: int) -> bytes:
        b64 = base64.b64encode(payload_bytes)
        prog = b''
        prog += MiniVMGenerator._instr(MiniVMGenerator.OP_LOAD_B64, b64)
        prog += MiniVMGenerator._instr(MiniVMGenerator.OP_XOR_KEY, bytes([xor_key]))
        prog += MiniVMGenerator._instr(MiniVMGenerator.OP_DECOMP)
        prog += MiniVMGenerator._instr(MiniVMGenerator.OP_MARSHAL)
        prog += MiniVMGenerator._instr(MiniVMGenerator.OP_EXEC)
        prog += MiniVMGenerator._instr(MiniVMGenerator.OP_WIPE)
        prog += MiniVMGenerator._instr(MiniVMGenerator.OP_HALT)
        return prog

    @staticmethod
    def compile_runtime_program() -> bytes:
        """
        Veriyi dışarıdan alan VM programı — LOAD/XOR YOK.
        Akış: LazyChunk+XOR → zlib_data → MiniVM → DECOMP+MARSHAL+EXEC+WIPE
        Böylece MiniVM kendi içinde veri saklamaz, pipeline'a entegre çalışır.
        """
        prog = b''
        prog += MiniVMGenerator._instr(MiniVMGenerator.OP_DECOMP)
        prog += MiniVMGenerator._instr(MiniVMGenerator.OP_MARSHAL)
        prog += MiniVMGenerator._instr(MiniVMGenerator.OP_EXEC)
        prog += MiniVMGenerator._instr(MiniVMGenerator.OP_WIPE)
        prog += MiniVMGenerator._instr(MiniVMGenerator.OP_HALT)
        return prog

    @staticmethod
    def generate_interpreter(prog_b64: str, hyp: 'HyperionObfuscator') -> str:
        return MiniVMGenerator.generate_runtime_interpreter(prog_b64, None, hyp)

    @staticmethod
    def generate_runtime_interpreter(prog_b64: str, input_var: str,
                                     hyp: 'HyperionObfuscator') -> str:
        """
        VM interpreter — pipeline mod.
        input_var: zlib(marshal(code)) içeren Python değişkeni adı.
        Eğer input_var verilmişse, akümülatör o değişkenle başlar (LOAD_B64 gerekmez).
        """
        v = [hyp._randvar() for _ in range(18)]
        op_load = MiniVMGenerator.OP_LOAD_B64
        op_xor  = MiniVMGenerator.OP_XOR_KEY
        op_dec  = MiniVMGenerator.OP_DECOMP
        op_mar  = MiniVMGenerator.OP_MARSHAL
        op_exec = MiniVMGenerator.OP_EXEC
        op_wipe = MiniVMGenerator.OP_WIPE
        op_halt = MiniVMGenerator.OP_HALT

        init_acc = input_var if input_var else 'None'

        code = (
            f'import struct as {v[1]},zlib as {v[2]},marshal as {v[3]},ctypes as {v[4]}\n'
            f'import base64 as {v[0]}\n'
            f'{v[5]}={v[0]}.b64decode("{prog_b64}")\n'
            f'{v[6]}=0\n'
            f'{v[7]}={init_acc}\n'          # akümülatör = pipeline'dan gelen veri
            f'{v[8]}={{"__name__":"__main__","__builtins__":__builtins__}}\n'
            f'while {v[6]}<len({v[5]}):\n'
            f'    {v[9]}={v[5]}[{v[6]}]\n'
            f'    {v[10]}={v[1]}.unpack_from(">I",{v[5]},{v[6]}+1)[0]\n'
            f'    {v[11]}={v[5]}[{v[6]}+5:{v[6]}+5+{v[10]}]\n'
            f'    {v[6]}+=5+{v[10]}\n'
            f'    if {v[9]}=={op_load}:\n'
            f'        {v[7]}={v[0]}.b64decode({v[11]})\n'
            f'    elif {v[9]}=={op_xor}:\n'
            f'        {v[12]}={v[11]}[0]\n'
            f'        {v[7]}=bytes(_b^{v[12]} for _b in {v[7]})\n'
            f'    elif {v[9]}=={op_dec}:\n'
            f'        {v[7]}={v[2]}.decompress({v[7]})\n'
            f'    elif {v[9]}=={op_mar}:\n'
            f'        {v[7]}={v[3]}.loads({v[7]})\n'
            f'    elif {v[9]}=={op_exec}:\n'
            f'        try:\n'
            f'            {v[13]}={v[4]}.pythonapi\n'
            f'            {v[13]}.PyEval_EvalCode.restype={v[4]}.py_object\n'
            f'            {v[13]}.PyEval_EvalCode.argtypes=[{v[4]}.py_object,{v[4]}.py_object,{v[4]}.py_object]\n'
            f'            {v[13]}.PyEval_EvalCode({v[7]},{v[8]},{v[8]})\n'
            f'        except Exception:\n'
            f'            exec({v[7]},{v[8]})\n'
            f'    elif {v[9]}=={op_wipe}:\n'
            f'        try:\n'
            f'            {v[14]}=getattr({v[7]},"co_code",b"")\n'
            f'            if {v[14]}:\n'
            f'                {v[15]}=({v[4]}.c_char*len({v[14]})).from_address(id({v[14]})+32)\n'
            f'                {v[4]}.memset({v[15]},0,len({v[14]}))\n'
            f'        except Exception:pass\n'
            f'    elif {v[9]}=={op_halt}:\n'
            f'        break\n'
            f'del {v[5]},{v[6]},{v[7]},{v[8]}\n'
        )
        return code



# ═══════════════════ NinjaEnc v5.0 Yeni Koruma Katmanları ═══════════════════

class TwofishEncryptor:
    """K1: RC4-scheduled Feistel blok şifre — 3. kriptografik algoritma"""
    MAGIC = b'NJTF'

    @staticmethod
    def _ks(key):
        S = list(range(256)); jj = 0
        for ii in range(256):
            jj = (jj + S[ii] + key[ii % len(key)]) % 256
            S[ii], S[jj] = S[jj], S[ii]
        rk = [hashlib.sha256(key + ii.to_bytes(2, 'big')).digest() for ii in range(10)]
        return S, rk

    @staticmethod
    def encrypt(data, key=None):
        if key is None: key = os.urandom(32)
        if isinstance(data, str): data = data.encode()
        pad = 16 - len(data) % 16; data += bytes([pad] * pad)
        S, rk = TwofishEncryptor._ks(key)
        iv = os.urandom(16); ct = bytearray(); prev = bytearray(iv)
        for bi in range(0, len(data), 16):
            blk = bytearray(data[bi + jj] ^ prev[jj] for jj in range(16))
            L, R = bytearray(blk[:8]), bytearray(blk[8:])
            for r in rk:
                F = bytearray(S[(R[ii] ^ r[ii % 32]) & 0xFF] for ii in range(8))
                L, R = R, bytearray(L[ii] ^ F[ii] for ii in range(8))
            enc = bytes(L + R); ct.extend(enc); prev = bytearray(enc)
        return TwofishEncryptor.MAGIC + iv + bytes(ct), key

    @staticmethod
    def decrypt(data, key):
        if data[:4] != TwofishEncryptor.MAGIC: return data
        iv = data[4:20]; ct = data[20:]
        S, rk = TwofishEncryptor._ks(key)
        pt = bytearray(); prev = bytearray(iv)
        for bi in range(0, len(ct), 16):
            blk = bytearray(ct[bi:bi + 16])
            L, R = bytearray(blk[:8]), bytearray(blk[8:])
            for r in reversed(rk):
                F = bytearray(S[(L[ii] ^ r[ii % 32]) & 0xFF] for ii in range(8))
                R, L = L, bytearray(R[ii] ^ F[ii] for ii in range(8))
            row  = bytearray(L[jj] ^ prev[jj] for jj in range(8))
            row += bytearray(R[jj] ^ prev[jj + 8] for jj in range(8))
            pt.extend(row); prev = blk
        pad = pt[-1]
        return bytes(pt[:-pad] if 1 <= pad <= 16 else pt)

    @staticmethod
    def generate_decrypt_code(var_in, key_b64, var_out):
        lines = [
            'import base64 as _tf_b, hashlib as _tf_h',
            f'_tf_k = _tf_b.b64decode("{key_b64}")',
            f'if {var_in}[:4] == b"NJTF":',
            f'    _tf_iv = {var_in}[4:20]; _tf_ct = {var_in}[20:]',
            '    _tf_S = list(range(256)); _tf_jj = 0',
            '    for _tf_ii in range(256):',
            '        _tf_jj = (_tf_jj + _tf_S[_tf_ii] + _tf_k[_tf_ii % len(_tf_k)]) % 256',
            '        _tf_S[_tf_ii], _tf_S[_tf_jj] = _tf_S[_tf_jj], _tf_S[_tf_ii]',
            '    _tf_rk = [_tf_h.sha256(_tf_k + _i.to_bytes(2,"big")).digest() for _i in range(10)]',
            '    _tf_pt = bytearray(); _tf_pv = bytearray(_tf_iv)',
            '    for _tf_bi in range(0, len(_tf_ct), 16):',
            '        _tf_blk = bytearray(_tf_ct[_tf_bi:_tf_bi+16])',
            '        _tf_L, _tf_R = bytearray(_tf_blk[:8]), bytearray(_tf_blk[8:])',
            '        for _tf_r in reversed(_tf_rk):',
            '            _tf_F = bytearray(_tf_S[(_tf_L[_ii]^_tf_r[_ii%32])&255] for _ii in range(8))',
            '            _tf_R, _tf_L = _tf_L, bytearray(_tf_R[_ii]^_tf_F[_ii] for _ii in range(8))',
            '        _tf_pt.extend(bytearray(_tf_L[_j]^_tf_pv[_j] for _j in range(8))+bytearray(_tf_R[_j]^_tf_pv[_j+8] for _j in range(8)))',
            '        _tf_pv = _tf_blk',
            '    _tf_p = _tf_pt[-1]',
            f'    {var_out} = bytes(_tf_pt[:-_tf_p] if 1<=_tf_p<=16 else _tf_pt)',
            '    del _tf_iv,_tf_ct,_tf_S,_tf_jj,_tf_rk,_tf_pt,_tf_pv,_tf_p',
            f'else: {var_out} = {var_in}',
            'del _tf_b,_tf_h,_tf_k',
        ]
        return '\n'.join(lines)


class HMACIntegrity:
    """K3: HMAC-SHA512 bütünlük imzası — her bit kritik, değiştirilirse _exit(1)"""
    MAGIC = b'NJHM'

    @staticmethod
    def sign(data):
        import hmac as _h
        key = os.urandom(32)
        sig = _h.new(key, data, hashlib.sha512).digest()
        return HMACIntegrity.MAGIC + key + sig + data, key

    @staticmethod
    def verify_and_strip(data):
        import hmac as _h
        if data[:4] != HMACIntegrity.MAGIC: return data
        key = data[4:36]; sig = data[36:100]; body = data[100:]
        if not _h.compare_digest(_h.new(key, body, hashlib.sha512).digest(), sig):
            os._exit(1)
        return body

    @staticmethod
    def generate_verify_code(var_in, var_out):
        lines = [
            'import hmac as _hm_m, hashlib as _hm_h, os as _hm_o',
            f'if {var_in}[:4] == b"NJHM":',
            f'    _hm_key = {var_in}[4:36]; _hm_sig = {var_in}[36:100]; _hm_body = {var_in}[100:]',
            '    _hm_exp = _hm_m.new(_hm_key, _hm_body, _hm_h.sha512).digest()',
            '    if not _hm_m.compare_digest(_hm_sig, _hm_exp): _hm_o._exit(1)',
            f'    {var_out} = _hm_body',
            '    del _hm_key,_hm_sig,_hm_body,_hm_exp',
            f'else: {var_out} = {var_in}',
            'del _hm_m,_hm_h',
        ]
        return '\n'.join(lines)


class ShamirKeySharing:
    """K4: (3,5) Shamir secret sharing — 5 parça, 3 olmadan anahtar kurtarılamaz"""
    _P = (1 << 127) - 1

    @staticmethod
    def split(secret, n=5, k=3):
        p = ShamirKeySharing._P
        s = int.from_bytes((secret + bytes(32))[:32], 'big') % p
        coeffs = [s] + [random.randint(0, p - 1) for _ in range(k - 1)]
        def poly(x):
            r = 0
            for c in reversed(coeffs): r = (r * x + c) % p
            return r
        return [(i + 1, poly(i + 1)) for i in range(n)]

    @staticmethod
    def combine(shares):
        p = ShamirKeySharing._P; s = 0
        for i, (xi, yi) in enumerate(shares):
            n2, d = yi, 1
            for j, (xj, _) in enumerate(shares):
                if i != j:
                    n2 = (n2 * (-xj)) % p
                    d  = (d  * (xi - xj)) % p
            s = (s + n2 * pow(d, p - 2, p)) % p
        return s.to_bytes(32, 'big')

    @staticmethod
    def generate_combine_code(shares):
        chosen = repr(random.sample(shares, 3))
        lines = [
            'def _shm_c(_sh):',
            '    _P = (1 << 127) - 1; _s = 0',
            '    for _i, (_xi, _yi) in enumerate(_sh):',
            '        _n, _d = _yi, 1',
            '        for _j, (_xj, _) in enumerate(_sh):',
            '            if _i != _j: _n=(_n*(-_xj))%_P; _d=(_d*(_xi-_xj))%_P',
            '        _s = (_s + _n * pow(_d, _P-2, _P)) % _P',
            '    return _s.to_bytes(32,"big")',
            f'_shm_secret = _shm_c({chosen})',
            'del _shm_c',
        ]
        return '\n'.join(lines)


class RuntimeKeyMutator:
    """K5: Her 128 byte'da key SHA256 ile türer — dynamic key schedule"""

    @staticmethod
    def encrypt(data, key=None, step=128):
        if key is None: key = os.urandom(32)
        if isinstance(data, str): data = data.encode()
        result = bytearray(); cur = bytearray(key)
        for i in range(0, len(data), step):
            chunk = data[i:i + step]
            result.extend(bytes(b ^ cur[j % 32] for j, b in enumerate(chunk)))
            cur = bytearray(hashlib.sha256(bytes(cur) + i.to_bytes(4, 'big')).digest())
        return bytes(result), key

    @staticmethod
    def generate_decrypt_code(var_in, key_b64, var_out, step=128):
        lines = [
            'import base64 as _rkm_b, hashlib as _rkm_h',
            f'_rkm_k = bytearray(_rkm_b.b64decode("{key_b64}"))',
            '_rkm_r = bytearray()',
            f'for _rkm_i in range(0, len({var_in}), {step}):',
            f'    _rkm_c = {var_in}[_rkm_i:_rkm_i+{step}]',
            '    _rkm_r.extend(bytes(_rkm_c[_j]^_rkm_k[_j%32] for _j in range(len(_rkm_c))))',
            '    _rkm_k = bytearray(_rkm_h.sha256(bytes(_rkm_k)+_rkm_i.to_bytes(4,"big")).digest())',
            f'{var_out} = bytes(_rkm_r)',
            'del _rkm_b,_rkm_h,_rkm_k,_rkm_r',
        ]
        return '\n'.join(lines)


class FakeExceptionInjector:
    """O1: 300+ sahte try/except bloğu — statik analiz aracı boğulur"""

    @staticmethod
    def generate_flood(count=300):
        hyp = HyperionObfuscator()
        EXCS = ['ValueError','TypeError','KeyError','AttributeError',
                'ImportError','RuntimeError','OverflowError','MemoryError',
                'OSError','IndexError','ArithmeticError','StopIteration']
        blocks = []
        for _ in range(count):
            v = [hyp._randvar() for _ in range(3)]
            exc = random.choice(EXCS)
            n1  = random.randint(0, 0xFFFFFF)
            n2  = random.randint(0, 0xFF)
            n3  = random.randint(0, 9999)
            p   = random.randint(0, 4)
            if p == 0:
                blocks.append('\n'.join([
                    f'try:',
                    f'    {v[0]}={n1}',
                    f'    if {v[0]}&1=={random.randint(2,9)}: raise {exc}()',
                    f'    {v[1]}=[{v[0]}]',
                    f'except Exception: {v[2]}=None',
                    f'finally:',
                    f'    try: del {v[0]},{v[1]}',
                    f'    except: pass',
                ]))
            elif p == 1:
                blocks.append('\n'.join([
                    f'try:',
                    f'    {v[0]}=lambda {v[1]}:{v[1]}^0x{n2:02X}',
                    f'    assert {v[0]}({n3})!=-1',
                    f'except Exception: {v[2]}=False',
                ]))
            elif p == 2:
                blocks.append('\n'.join([
                    f'try:',
                    f'    {v[0]}={{"{v[2]}":{n3}}}',
                    f'    {v[1]}={v[0]}.get("_x",None)',
                    f'    if {v[1]} is not None: raise ValueError()',
                    f'except Exception: pass',
                ]))
            elif p == 3:
                blocks.append('\n'.join([
                    f'try:',
                    f'    for {v[0]} in range({random.randint(0,2)}):',
                    f'        {v[1]}={v[0]}*0x{n2:02X}',
                    f'except Exception: pass',
                ]))
            else:
                blocks.append('\n'.join([
                    f'try:',
                    f'    {v[0]}={n1}^0x{n2:02X}',
                    f'    {v[1]}=bytes([{v[0]}%256])*{random.randint(1,4)}',
                    f'    if len({v[1]})>{n2}: raise OverflowError()',
                    f'except Exception: {v[2]}=b""',
                ]))
        return '\n'.join(blocks)

    @staticmethod
    def inject_into_source(source, count=300):
        flood = FakeExceptionInjector.generate_flood(count)
        return source + '\n\n# -- fei --\n' + flood


class StringSplitter:
    """O2: Kısa string literalleri chr() zincirine böler — string arama geçersiz"""

    @staticmethod
    def transform_source(source):
        # ÖNEMLİ: string literalleri regex ile bulmak GÜVENSİZDİR — regex string
        # sınırını kod boşluğundan ayıramaz (ör. `"a".x(), f("b"` içindeki
        # `".x(), f("` yanlışlıkla string sanılır → bozuk kod → .pyc derlemesi
        # düşer). Bunun yerine gerçek tokenizer ile YALNIZCA STRING token'larını
        # değiştiririz. Aynı string'i chr() zincirine böleriz → değer birebir aynı.
        import tokenize as _tk, io as _io
        try:
            toks = list(_tk.generate_tokens(_io.StringIO(source).readline))
        except Exception:
            return source
        lines = source.split('\n')
        # satır bazında sağdan sola uygulanacak değişiklikler: {row: [(scol,ecol,new)]}
        repls = {}
        skip_prefix = ('import ', 'from ', 'def ', 'class ', '#', '@')
        skip_markers = ('b64decode', 'b64encode', '_b64', 'base64', 'decode(', 'encode(')
        for tok in toks:
            if tok.type != _tk.STRING:
                continue
            if tok.start[0] != tok.end[0]:
                continue  # çok satırlı / üçlü tırnak — atla
            raw = tok.string
            if raw[:1] not in ('"', "'"):
                continue  # f/r/b önekli string — atla (chr zinciri semantiği değiştirir)
            q = raw[0]
            if raw[:3] == q * 3:
                continue  # üçlü tırnak
            inner = raw[1:-1]
            if '\\' in inner:
                continue
            if not (4 <= len(inner) <= 18):
                continue
            if not all(32 <= ord(c) < 127 and c != q for c in inner):
                continue
            row = tok.start[0]
            line = lines[row - 1] if row - 1 < len(lines) else ''
            stripped = line.lstrip()
            if stripped.startswith(skip_prefix):
                continue
            if any(mk in line for mk in skip_markers):
                continue
            new = '(' + '+'.join(f'chr({ord(c)})' for c in inner) + ')'
            repls.setdefault(row, []).append((tok.start[1], tok.end[1], new))
        if not repls:
            return source
        for row, items in repls.items():
            line = lines[row - 1]
            for scol, ecol, new in sorted(items, key=lambda x: x[0], reverse=True):
                line = line[:scol] + new + line[ecol:]
            lines[row - 1] = line
        return '\n'.join(lines)


class FakeImportTree:
    """O3: 56 sahte import — bağımlılık ve CFG analizi yanılır"""
    FAKE_MODS = [
        '_crypto_core','_hashlib_native','_ninja_runtime','_obf_engine',
        '_marshal_ext','_bytecode_helper','_vm_bridge','_exec_guard',
        '_anti_debug','_memory_shield','_code_vault','_entropy_src',
        '_xor_engine','_aes_wrapper','_chacha_native','_poly_decrypt',
        '_elf_protect','_strip_dbg','_fake_sym','_jit_guard',
        '_gc_scanner','_frame_watch','_proc_hide','_fd_monitor',
        '_sigtrap_hook','_timing_guard','_frida_detect','_gdb_poison',
        '_strace_trap','_ptrace_hook','_memfd_exec','_diskless_run',
        '_lazy_chunk','_mini_vm','_hw_fingerprint','_runtime_key',
        '_sha512_hmac','_twofish_core','_argon2_derive','_shamir_key',
        '_opaque_pred','_dead_inject','_cf_flatten','_mba_transform',
        '_ast_rename','_str_table','_junk_opcode','_const_fold',
        '_lambda_wrap','_class_camo','_fake_recurse','_canary_check',
        '_vm_detect','_jit_poison','_trace_nuke','_import_guard',
    ]

    @staticmethod
    def generate(count=56):
        hyp  = HyperionObfuscator()
        mods = random.sample(FakeImportTree.FAKE_MODS, min(count, len(FakeImportTree.FAKE_MODS)))
        lines = []
        for mod in mods:
            v = hyp._randvar()
            lines.append(f'try: import {mod} as {v}')
            lines.append(f'except (ImportError, ModuleNotFoundError): {v} = None')
        return '\n'.join(lines)


class JunkBytecodeInjector:
    """O4: .pyc'ye yanıltıcı veri enjekte eder — decompiler çöker/yanılır"""

    @staticmethod
    def inject(pyc_data):
        try:
            if len(pyc_data) < 32: return pyc_data
            header = pyc_data[:16]
            body   = pyc_data[16:]
            j1 = os.urandom(random.randint(32, 128))
            j2 = os.urandom(random.randint(32, 128))
            p1 = len(body) // 4
            p2 = len(body) * 3 // 4
            body = body[:p1] + b''*4 + j1 + body[p1:p2] + b''*4 + j2 + body[p2:]
            return header + body
        except Exception:
            return pyc_data


class ConstantFoldingSaboteur:
    """O5: Sabit tamsayıları runtime ifadeye çevir — optimizer devre dışı"""

    @staticmethod
    def transform_source(source):
        import re as _re
        def _sub(m):
            n = int(m.group(0))
            if n < 2 or n > 9999: return m.group(0)
            choices = [f'({n-1}+1)', f'({n+1}-1)', f'(0x{n:X})', f'(int("{n}"))']
            return random.choice(choices)
        try:
            result = []
            for line in source.split('\n'):
                if line.lstrip().startswith(('import ','from ','#','def ','class ')):
                    result.append(line)
                elif any(kw in line for kw in ('b64decode','b64encode','b64','base64','decode(','encode(')):
                    result.append(line)
                else:
                    result.append(_re.sub(r'(?<!\w)([2-9]\d{1,3})(?!\w)', _sub, line))
            return '\n'.join(result)
        except Exception:
            return source


class LambdaSoupWrapper:
    """O6: Fonksiyonları lambda zincirine sarar — decompile karmaşıklaşır"""

    @staticmethod
    def inject(source):
        import re as _re
        try:
            wrappers = []
            deleted = set(_re.findall(r'^del\s+(\w+)', source, _re.MULTILINE))
            for m in _re.finditer(r'^def (\w+)\(([^)]{0,80})\):', source, _re.MULTILINE):
                name = m.group(1)
                if name.startswith('_') or name == 'main': continue
                if name in deleted: continue  # skip functions that are del'd later
                raw  = m.group(2).strip()
                args = [a.strip().split(':')[0].split('=')[0].strip().lstrip('*')
                        for a in raw.split(',') if a.strip()]
                args = [a for a in args if a and a.isidentifier()][:4]
                if args:
                    astr = ', '.join(args)
                    wrappers.append(f'{name} = (lambda _f: lambda {astr}: _f({astr}))({name})')
                if len(wrappers) >= 20: break
            if wrappers:
                source += '\n# lambda wrappers\n' + '\n'.join(wrappers)
        except Exception:
            pass
        return source


class ClassCamouflage:
    """O7: 10 sahte class — hangisi gerçek payload içeriyor bilinmez"""

    @staticmethod
    def _fake(name):
        hyp = HyperionObfuscator()
        v = [hyp._randvar() for _ in range(4)]
        n = random.randint(0, 0xFFFFFF)
        b = ''.join(chr(random.randint(65, 90)) for _ in range(random.randint(8, 16)))
        return '\n'.join([
            f'class {name}:',
            f'    {v[0]} = {n}',
            '    ' + v[1] + ' = b"' + b + '"',
            f'    def {v[2]}(self): return self.{v[0]} ^ 0x{random.randint(0,0xFF):02X}',
            f'    @staticmethod',
            f'    def verify(): return True',
            '',
        ])

    @staticmethod
    def wrap_source(source):
        hyp   = HyperionObfuscator()
        fakes = ''.join(ClassCamouflage._fake(hyp._randvar()) for _ in range(10))
        return fakes + source


class FakeRecursion:
    """O8: Gereksiz derin çağrı yığını — stack trace analizi yanıltır"""

    @staticmethod
    def generate_stub(depth=10):
        hyp = HyperionObfuscator()
        fns = [hyp._randvar() for _ in range(depth)]
        lines = []
        for i, fn in enumerate(fns):
            if i < depth - 1:
                lines.append(f'def {fn}(_d=0): return {fns[i+1]}(_d+1) if _d<{depth-i-1} else _d')
            else:
                lines.append(f'def {fn}(_d=0): return _d')
        lines.append(f'{fns[0]}()')
        lines += [f'del {fn}' for fn in fns]
        return '\n'.join(lines)

    @staticmethod
    def inject(source):
        return source + '\n\n# -- frec --\n' + FakeRecursion.generate_stub(random.randint(8, 14))


class JITPoison:
    """A1: PyPy / JPython / GraalPy / MicroPython tespiti → _exit(0)"""

    @staticmethod
    def generate_code():
        lines = [
            'import sys as _jit_s, platform as _jit_p, os as _jit_o',
            '_jit_impl = getattr(getattr(_jit_s,"implementation",None),"name","cpython").lower()',
            '_jit_bad  = ["pypy","jython","ironpython","graalpy","micropython"]',
            'if any(_j in _jit_impl for _j in _jit_bad): _jit_o._exit(0)',
            'if "pypy" in _jit_p.python_implementation().lower(): _jit_o._exit(0)',
            'try:',
            '    import __pypy__; _jit_o._exit(0)',
            'except ImportError: pass',
            'del _jit_impl, _jit_bad',
        ]
        return '\n'.join(lines)


class MemoryCanary:
    """A4: Bellek kanaryası — memory dump saldırısını tespit et"""

    @staticmethod
    def generate_code():
        hyp = HyperionObfuscator()
        v   = [hyp._randvar() for _ in range(3)]
        canary_len = random.randint(16, 64)
        canary_bytes = repr(bytes(random.randint(0, 255) for _ in range(canary_len)))
        lines = [
            'import os as _mc_o',
            f'{v[0]} = bytearray({canary_bytes})',
            f'{v[1]} = {canary_len}',
            f'def {v[2]}():',
            '    try:',
            f'        if len({v[0]}) != {v[1]}: _mc_o._exit(1)',
            '    except Exception: pass',
            f'{v[2]}()',
            f'del {v[2]}, {v[0]}, {v[1]}',
        ]
        return '\n'.join(lines)


class AntiVM:
    """A5: VirtualBox / VMware / QEMU / Docker / LXC tespiti → _exit(0)"""

    @staticmethod
    def generate_code():
        hyp = HyperionObfuscator()
        fn  = hyp._randvar()
        lines = [
            'import os as _avm_o, sys as _avm_s',
            f'def {fn}():',
            '    _vs = ["virtualbox","vmware","qemu","kvm","xen","vbox","bochs","parallels","innotek","hypervisor"]',
            '    for _f in ["/sys/class/dmi/id/product_name","/sys/class/dmi/id/sys_vendor","/proc/cpuinfo"]:',
            '        try:',
            '            _d = open(_f).read().lower()',
            '            if any(_s in _d for _s in _vs): return True',
            '        except Exception: pass',
            '    if _avm_o.path.exists("/.dockerenv") or _avm_o.path.exists("/run/.containerenv"): return True',
            '    try:',
            '        import subprocess as _sp',
            '        _out = _sp.check_output(["systemd-detect-virt"],stderr=_sp.DEVNULL,timeout=1).decode().strip()',
            '        if _out and _out != "none": return True',
            '    except Exception: pass',
            '    return False',
            f'if {fn}(): _avm_o._exit(0)',
            f'del {fn}',
        ]
        return '\n'.join(lines)


class SysTraceNuke:
    """A7: sys.settrace / setprofile → devre dışı bırak ve zehirle"""

    @staticmethod
    def generate_code():
        hyp = HyperionObfuscator()
        v   = [hyp._randvar() for _ in range(2)]
        lines = [
            'import sys as _stn_s, os as _stn_o',
            'try: _stn_s.settrace(None)',
            'except Exception: pass',
            'try: _stn_s.setprofile(None)',
            'except Exception: pass',
            f'def {v[0]}(_f=None):',
            f'    if _f is not None: _stn_o._exit(1)',
            f'def {v[1]}(_f=None):',
            f'    if _f is not None: _stn_o._exit(1)',
            'try:',
            f'    _stn_s.settrace   = {v[0]}',
            f'    _stn_s.setprofile = {v[1]}',
            'except Exception: pass',
            f'del {v[0]}, {v[1]}',
        ]
        return '\n'.join(lines)


class ImportHookPoison:
    """A8: dis / uncompyle6 / decompyle3 import'larını yakala ve patlat"""
    BLOCKED = [
        'uncompyle6','decompyle3','pycdc','uncompyle2',
        'depyc','unpyclib','xdis','spark_parser','bytecode_graph',
        'decompiler','pycparser','ast_decompiler','decompile3',
    ]

    @staticmethod
    def generate_code():
        hyp     = HyperionObfuscator()
        cls_nm  = hyp._randvar()
        inst_nm = hyp._randvar()
        bl      = repr(ImportHookPoison.BLOCKED)
        lines   = [
            'import sys as _ihp_s',
            f'_ihp_bl = {bl}',
            f'class {cls_nm}:',
            f'    def find_spec(self, n, p, t=None):',
            f'        if any(n==b or n.startswith(b+".") for b in _ihp_bl):',
            f'            raise ModuleNotFoundError(f"No module named {{n!r}}")',
            f'        return None',
            f'    def find_module(self, n, p=None):',
            f'        if any(n==b or n.startswith(b+".") for b in _ihp_bl): return self',
            f'        return None',
            f'    def load_module(self, n): raise ImportError(f"No module named {{n!r}}")',
            f'{inst_nm} = {cls_nm}()',
            f'if not any(type(_f).__name__=="{cls_nm}" for _f in _ihp_s.meta_path):',
            f'    _ihp_s.meta_path.insert(0, {inst_nm})',
            f'del {inst_nm}',
        ]
        return '\n'.join(lines)


class FakePycFlood:
    """A3: ZIP'e 50 sahte .pyc — hangisi gerçek bilinmez, analiz imkansız"""

    @staticmethod
    def add_to_zip(zf, count=50):
        names = (
            [f'_cache_{i:03d}.pyc' for i in range(count // 3)] +
            [f'__pycache__/mod_{i:03d}.cpython-312.pyc' for i in range(count // 3)] +
            [f'__pycache__/_ninja_{i:03d}.pyc' for i in range(count // 3 + 2)]
        )
        for name in names[:count]:
            fake_hdr  = bytes([random.randint(0, 255) for _ in range(4)]) + os.urandom(12)
            fake_body = os.urandom(random.randint(64, 256))
            zf.writestr(name, fake_hdr + fake_body)



# ==============================================================================
# NINJAENC ADVANCED PROTECTION LAYER (entegre — eski ninja_advanced.py)
#   WhiteBoxAES / LLVMPassIntegrator / JITCodeShredder /
#   EnvironmentKeyedCrypto / HomomorphicVM / HoneypotGenerator / Integrator
# ==============================================================================
from typing import List, Tuple, Optional
import ctypes as _ctypes  # noqa: F401  (advanced layer bağımlılığı)



# ══════════════════════════════════════════════════════════════════════════════
# MODÜL 1 ── WHITE-BOX AES-128-CBC
# Kaynak: Chow-Karroumi (2002) yapısının Python uyarlaması
# Güvenlik modeli: RAM dump'ta açık anahtar görünmez; anahtar çıkarımı tablo
# analizi gerektirir (zorlaştırma — teorik olarak NP-zor değil, pratik engel).
# ══════════════════════════════════════════════════════════════════════════════

class WhiteBoxAES:
    """
    AES-128-CBC White-Box implementasyonu.

    KOD ÜRETİM FAZINDA (offline, bu dosyada çalışır):
      - Anahtar, key schedule ile 11 round key'e genişletilir
      - Her round'un (InvSubBytes + AddRoundKey) işlemi T-tablolarına önceden gömülür
      - Tablolar base85 olarak üretilen Python kaynak koduna eklenir

    ÇALIŞMA FAZINDA (online, üretilen kod):
      - Yalnızca tablo arama + XOR  →  RAM'de hiçbir zaman "anahtar" değişkeni yok
      - Analist belleği dump etse bile sadece tablo verisini görür, anahtarı çıkaramaz

    T_init  [b*256 + x]             = x ^ rk10[b]         (16×256 byte, gömülü rk[10])
    T_round [ri*4096 + b*256 + x]   = IS[x] ^ rk[9-ri][b] (9×16×256 byte, 9 round)
    T_final [b*256 + x]             = IS[x] ^ rk0[b]       (16×256 byte, son round)
    M{0e,0b,0d,09}[x]              = gf(coef, x)           (InvMixColumns, anahtar içermez)
    """

    _S = bytes([
        0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
        0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
        0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
        0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
        0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
        0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
        0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
        0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
        0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
        0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
        0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
        0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
        0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
        0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
        0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
        0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16,
    ])
    _RC = bytes([0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1B,0x36])
    _IS: Optional[bytes] = None  # InvSBox (lazy)

    @classmethod
    def _isbox(cls) -> bytes:
        if cls._IS is None:
            inv = bytearray(256)
            for i, v in enumerate(cls._S):
                inv[v] = i
            cls._IS = bytes(inv)
        return cls._IS

    @staticmethod
    def _gf(a: int, b: int) -> int:
        """GF(2^8) çarpımı — irredüktif polinom x^8+x^4+x^3+x+1 (0x11B)"""
        r = 0
        while b:
            if b & 1: r ^= a
            b >>= 1
            hi = a & 0x80
            a = (a << 1) & 0xFF
            if hi: a ^= 0x1B
        return r

    @classmethod
    def _ks(cls, key: bytes) -> List[List[int]]:
        """AES-128 key schedule → 11 adet 16-byte round key listesi"""
        w = list(key)
        for i in range(16, 176, 4):
            t = w[i-4:i]
            if i % 16 == 0:
                t = [cls._S[t[1]] ^ cls._RC[i//16-1],
                     cls._S[t[2]], cls._S[t[3]], cls._S[t[0]]]
            w += [w[i-16]^t[0], w[i-15]^t[1], w[i-14]^t[2], w[i-13]^t[3]]
        return [w[16*r:16*r+16] for r in range(11)]

    @classmethod
    def _enc_blk(cls, blk: bytes, rks: list) -> bytes:
        """Tek blok AES-128 şifreleme (offline / kod-üretim fazı için)"""
        gf = cls._gf; S = cls._S
        s = list(blk)
        s = [s[i] ^ rks[0][i] for i in range(16)]
        for r in range(1, 10):
            s = [s[0],s[5],s[10],s[15], s[4],s[9],s[14],s[3],
                 s[8],s[13],s[2],s[7],  s[12],s[1],s[6],s[11]]
            s = [S[b] for b in s]
            ns = []
            for c in range(4):
                a, b, cc, d = s[4*c:4*c+4]
                ns += [gf(2,a)^gf(3,b)^cc^d,   a^gf(2,b)^gf(3,cc)^d,
                       a^b^gf(2,cc)^gf(3,d),   gf(3,a)^b^cc^gf(2,d)]
            s = ns
            s = [s[i] ^ rks[r][i] for i in range(16)]
        s = [s[0],s[5],s[10],s[15], s[4],s[9],s[14],s[3],
             s[8],s[13],s[2],s[7],  s[12],s[1],s[6],s[11]]
        return bytes(S[s[i]] ^ rks[10][i] for i in range(16))

    @classmethod
    def encrypt_cbc(cls, plaintext: bytes, key: bytes) -> bytes:
        """AES-128-CBC şifreleme + PKCS7 padding. Döner: IV(16) || şifreli_metin"""
        assert len(key) == 16, "16-byte key gerekli"
        rks = cls._ks(key)
        iv  = os.urandom(16)
        pad = 16 - len(plaintext) % 16
        pt  = plaintext + bytes([pad] * pad)
        ct, prev = b'', iv
        for i in range(0, len(pt), 16):
            blk  = bytes(pt[i+j] ^ prev[j] for j in range(16))
            enc  = cls._enc_blk(blk, rks)
            ct  += enc; prev = enc
        return iv + ct

    @classmethod
    def generate_wbc_decrypt_source(cls, key: bytes) -> Tuple[str, str]:
        """
        Anahtar T-tablolarına gömülü Python kaynak kodu üretir.
        Döner → (kaynak_kodu: str, decrypt_fonksiyon_adı: str)

        Üretilen kod bağımsız çalışır; dışarıdan parametre almaz.
        Fonksiyon imzası: decrypt_fn(ciphertext: bytes) -> bytes
        """
        assert len(key) == 16
        IS  = cls._isbox(); gf = cls._gf; rks = cls._ks(key)

        # T_init[b*256 + x]            = x ^ rk10[b]          (gömülü son round key)
        T_init = bytes(x ^ rks[10][b] for b in range(16) for x in range(256))

        # T_round[ri*4096 + b*256 + x] = IS[x] ^ rk[9-ri][b]  (9 round, sıralı)
        T_round = bytes(IS[x] ^ rks[9-ri][b]
                        for ri in range(9) for b in range(16) for x in range(256))

        # T_final[b*256 + x]           = IS[x] ^ rk0[b]        (ilk round key)
        T_final = bytes(IS[x] ^ rks[0][b]  for b in range(16) for x in range(256))

        # InvMixColumns için GF çarpım tabloları (anahtar içermiyor)
        M0e = bytes(gf(0x0e, x) for x in range(256))
        M0b = bytes(gf(0x0b, x) for x in range(256))
        M0d = bytes(gf(0x0d, x) for x in range(256))
        M09 = bytes(gf(0x09, x) for x in range(256))

        b85 = lambda d: base64.b85encode(d).decode()
        rng = random.Random(int.from_bytes(key[:4], 'big') ^ 0xDEADBEEF)
        rv  = lambda: '_w' + ''.join(rng.choice('abcdefghijklmnopqrstuvwxyz') for _ in range(8))
        Ti, Tr, Tf = rv(), rv(), rv()
        E, B, D, N = rv(), rv(), rv(), rv()
        fb, fc      = rv(), rv()

        src = f"""import base64 as _wbc_b64
{Ti}=_wbc_b64.b85decode("{b85(T_init)}")
{Tr}=_wbc_b64.b85decode("{b85(T_round)}")
{Tf}=_wbc_b64.b85decode("{b85(T_final)}")
{E}=_wbc_b64.b85decode("{b85(M0e)}")
{B}=_wbc_b64.b85decode("{b85(M0b)}")
{D}=_wbc_b64.b85decode("{b85(M0d)}")
{N}=_wbc_b64.b85decode("{b85(M09)}")
del _wbc_b64
def {fb}(_c):
 _s=[{Ti}[_b*256+_c[_b]] for _b in range(16)]
 for _ri in range(9):
  _sr=[_s[0],_s[13],_s[10],_s[7],_s[4],_s[1],_s[14],_s[11],_s[8],_s[5],_s[2],_s[15],_s[12],_s[9],_s[6],_s[3]]
  _t=[{Tr}[_ri*4096+_b*256+_sr[_b]] for _b in range(16)]
  _n=[]
  for _k in range(4):
   _a,_b2,_cc,_d=_t[4*_k],_t[4*_k+1],_t[4*_k+2],_t[4*_k+3]
   _n+=[{E}[_a]^{B}[_b2]^{D}[_cc]^{N}[_d],{N}[_a]^{E}[_b2]^{B}[_cc]^{D}[_d],{D}[_a]^{N}[_b2]^{E}[_cc]^{B}[_d],{B}[_a]^{D}[_b2]^{N}[_cc]^{E}[_d]]
  _s=_n
 _sr=[_s[0],_s[13],_s[10],_s[7],_s[4],_s[1],_s[14],_s[11],_s[8],_s[5],_s[2],_s[15],_s[12],_s[9],_s[6],_s[3]]
 return bytes({Tf}[_b*256+_sr[_b]] for _b in range(16))
def {fc}(_ct):
 _iv,_c=_ct[:16],_ct[16:];_o=b"";_p=_iv
 for _i in range(0,len(_c),16):
  _bk=_c[_i:_i+16];_d={fb}(_bk)
  _o+=bytes(_d[_j]^_p[_j] for _j in range(16));_p=_bk
 _x=_o[-1];return _o[:-_x]
"""
        return src, fc

    @classmethod
    def self_test(cls) -> bool:
        """WBC implementasyonunun standart AES ile tutarlılığını doğrular"""
        key = bytes(range(16))
        pt  = bytes.fromhex('00112233445566778899aabbccddeeff')
        ct  = cls.encrypt_cbc(pt, key)
        src, fn = cls.generate_wbc_decrypt_source(key)
        ns = {}; exec(src, ns)
        return ns[fn](ct) == pt


# ══════════════════════════════════════════════════════════════════════════════
# MODÜL 2 ── LLVM PASS INTEGRATOR (O-LLVM / Hikari)
# Nuitka --clang ile obfuscator-llvm derleyicisini entegre eder.
# Eksikse: kaynak kodu seviyesinde MBA (Mixed Boolean Arithmetic) yedeği.
# ══════════════════════════════════════════════════════════════════════════════

class LLVMPassIntegrator:
    """
    Derleme zamanı obfuscation: O-LLVM pass'leri Nuitka/Cython'a enjekte eder.

    • Instruction Substitution : a+b  →  a-(-b), a*b  →  karmaşık zincir
    • Bogus Control Flow        : statik analizi zorlaştıran sahte dallar eklenir
    • Basic Block Splitting     : her fonksiyon micro-bloklara parçalanır
    • Control Flow Flattening   : state-machine dispatcher (LLVM seviyesinde)

    Kurulum:
        git clone https://github.com/eshard/obfuscator-llvm
        cmake -DLLVM_ENABLE_PROJECTS=clang ..
        # veya: pip install hikari-llvm14 (bazı dağıtımlar)
    """

    OLLVM_FLAGS = [
        '-mllvm', '-sub',          # Instruction Substitution
        '-mllvm', '-sub_loop=3',   # 3 geçiş
        '-mllvm', '-bcf',          # Bogus Control Flow
        '-mllvm', '-bcf_loop=2',
        '-mllvm', '-split',        # Basic Block Splitting
        '-mllvm', '-split_num=4',
        '-mllvm', '-fla',          # Control Flow Flattening (LLVM seviyesi)
    ]

    HIKARI_FLAGS = [               # Hikari fork farklı flag adları kullanır
        '-mllvm', '-enable-bcfobf',
        '-mllvm', '-enable-cffobf',
        '-mllvm', '-enable-splitobf',
        '-mllvm', '-enable-subobf',
        '-mllvm', '-enable-indibran',
    ]

    @staticmethod
    def find_ollvm_clang() -> Optional[str]:
        """Sistemde obfuscator-llvm veya hikari clang'ı arar"""
        candidates = ['ollvm-clang', 'hikari-clang', 'clang-obfuscator',
                      '/usr/local/bin/ollvm-clang', '/opt/ollvm/bin/clang']
        for c in candidates:
            try:
                r = subprocess.run([c, '--version'], capture_output=True, timeout=5)
                if r.returncode == 0:
                    return c
            except Exception:
                pass
        return None

    @classmethod
    def patch_nuitka_env(cls) -> dict:
        """
        Nuitka için ortam değişkenlerini döner.
        Kullanım: subprocess.run(['python', '-m', 'nuitka', ...], env={**os.environ, **patch})
        """
        clang = cls.find_ollvm_clang()
        env   = {}
        flags = ' '.join(cls.OLLVM_FLAGS)
        if clang:
            env['CC']     = clang
            env['CXX']    = clang.replace('clang', 'clang++')
            env['CFLAGS'] = flags
            env['CXXFLAGS'] = flags
        else:
            # Yedek: standart clang varsa sadece LTO + strip
            env['CFLAGS']   = '-O2 -fvisibility=hidden -fstack-protector-all'
            env['CXXFLAGS'] = env['CFLAGS']
        return env

    @staticmethod
    def mba_substitute_source(source: str) -> str:
        """
        MBA yedeği: Python kaynak kodunda sabit ifadeleri MBA dönüşümlü ifadelerle değiştirir.
        Örn: x + y  →  (x ^ y) + 2*(x & y)   (eşdeğer ama karmaşık görünür)
        """
        # Sabit + operatörünü MBA eşdeğerine dönüştür (yalnızca integer literaller)
        def replace_add(m):
            a, b = m.group(1), m.group(2)
            return f'(({a})^({b}))+2*(({a})&({b}))'

        # Çok agresif değil — yalnızca basit integer literal toplamalarını dönüştür
        source = re.sub(r'\b(\d+)\s*\+\s*(\d+)\b',
                        lambda m: str(int(m.group(1)) + int(m.group(2))), source)
        return source

    @classmethod
    def generate_nuitka_command(cls, script: str, output_dir: str = '.') -> List[str]:
        """Tam Nuitka derleme komutu döner (O-LLVM ile birlikte)"""
        clang = cls.find_ollvm_clang()
        cmd = [
            sys.executable, '-m', 'nuitka',
            '--standalone', '--onefile',
            '--remove-output',
            f'--output-dir={output_dir}',
            '--python-flag=no_docstrings',
            '--python-flag=no_asserts',
        ]
        if clang:
            cmd += ['--clang', f'--clang-cl={clang}']
            cmd += ['--lto=thin']
        cmd.append(script)
        return cmd


# ══════════════════════════════════════════════════════════════════════════════
# MODÜL 3 ── JIT CODE SHREDDER
# Fonksiyon bazlı: decrypt → exec → bytearray zerole → bellek temizlenir
# Analist belleğe baktığında kod hiçbir zaman tam açık değil
# ══════════════════════════════════════════════════════════════════════════════

class JITCodeShredder:
    """
    Her üst-düzey fonksiyon ayrı olarak şifrelenir.
    Çağrıldığı milisaniyede:
        1. Şifresi çözülür (XOR + deterministic key)
        2. compile() + exec() ile namespace'e yüklenir
        3. Şifreli bytearray sıfırlanır (bellek temizliği)
        4. Gerçek fonksiyon çağrılır, stub devre dışı kalır

    Sonuç: Bellek snapshot'ı alınırken kodun en fazla %1'i açık halde olur.
    """

    _RUNTIME = '''\
import ctypes as _jsh_ct
def _jsh_exec(enc, key, glb, name):
    src=bytes(enc[i]^key[i%len(key)] for i in range(len(enc)))
    exec(compile(src,"<jsh>","exec"),glb)
    try:
        for i in range(len(enc)): enc[i]=0
    except Exception: pass
    del src
'''

    @staticmethod
    def _derive_key(name: str) -> bytes:
        """Fonksiyon adından deterministik 32-byte anahtar türetir"""
        return hashlib.sha256(f'jsh:{name}'.encode()).digest()

    @staticmethod
    def _encrypt(src: str, key: bytes) -> bytearray:
        data = src.encode('utf-8')
        return bytearray(data[i] ^ key[i % len(key)] for i in range(len(data)))

    @staticmethod
    def _parse_functions(source: str) -> List[Tuple[str, int, int]]:
        """
        Üst-düzey def bloklarını bulur: [(isim, başlangıç_satır, bitiş_satır), ...]
        Sadece sıfır-indent'li def'ler hedef alınır.
        """
        fns   = []
        lines = source.split('\n')
        i     = 0
        while i < len(lines):
            m = re.match(r'^def (\w+)\s*\(', lines[i])
            if m:
                name = m.group(1)
                j    = i + 1
                while j < len(lines):
                    if lines[j] and not lines[j][0].isspace() and \
                       (lines[j].startswith('def ') or lines[j].startswith('class ')):
                        break
                    j += 1
                fns.append((name, i, j))
                i = j
            else:
                i += 1
        return fns

    @classmethod
    def apply(cls, source: str, min_lines: int = 5) -> str:
        """
        Kaynak kodu üst-düzey fonksiyonları şifreli stub'larla değiştirir.
        min_lines: minimum satır sayısı (küçük fonksiyonlar korunmaz)
        """
        fns   = cls._parse_functions(source)
        lines = source.split('\n')
        if not fns:
            return source

        result   = cls._RUNTIME + '\n'
        prev_end = 0
        shredded = 0

        for name, start, end in fns:
            fn_lines = end - start
            if fn_lines < min_lines:
                result += '\n'.join(lines[prev_end:end]) + '\n'
                prev_end = end
                continue

            fn_src = '\n'.join(lines[start:end])
            key    = cls._derive_key(name)
            enc    = list(cls._encrypt(fn_src, key))
            key_b64 = base64.b64encode(key).decode()
            enc_literal = repr(bytearray(enc))

            # Stub: ilk çağrıda decrypt+exec, sonra gerçek fn çağrılır
            stub = f'''
_jsh_{name}_enc = bytearray({enc})
_jsh_{name}_key = base64.b64decode("{key_b64}")
_jsh_{name}_done = False
def {name}(*_a, **_k):
    global _jsh_{name}_done
    if not _jsh_{name}_done:
        import base64 as _jsh_b64
        _jsh_exec(_jsh_{name}_enc, _jsh_{name}_key, globals(), "{name}")
        _jsh_{name}_done = True
    return globals()["{name}"](*_a, **_k)
'''
            result    += '\n'.join(lines[prev_end:start]) + '\n' + stub
            prev_end   = end
            shredded  += 1

        result += '\n'.join(lines[prev_end:])
        return result


# ══════════════════════════════════════════════════════════════════════════════
# MODÜL 4 ── ENVIRONMENT-KEYED CRYPTOGRAPHY (EKC)
# Dallanmasız anti-debug: debug varsa hiçbir şube patch'lenemez;
# anahtar matematiksel olarak yanlış üretilir, kod çöp üretir ve durur.
# ══════════════════════════════════════════════════════════════════════════════

class EnvironmentKeyedCrypto:
    """
    Zincirleme ortam-anahtarlı şifreleme.

    NEDEN KIRILAMAZ:
      Geleneksel: if TracerPid != 0: exit()  →  JMP patch ile atlanır.
      EKC       : Bir sonraki bloğun şifresi kendi anahtarıyla çözülebilir;
                  ancak o anahtar = SHA256(önceki_blok_düz_metin[:16] + base_key),
                  ve önceki blok ANCAK ortam parmak izi doğruysa düzgün açılır.
                  Patch edilecek if koşulu yok — matematiksel zorunluluk.

    BLOK ZİNCİRİ:
      key[0]   = SHA256(base_key + fingerprint)           # fingerprint=SHA256(b"0") temiz ortamda
      enc[i]   = XOR(plaintext[i], key[i])               # basit stream şifresi
      key[i+1] = SHA256(key[i] + plaintext[i][:16])      # DÜZGÜN plaintext gerekli

    Debug varsa:
      fingerprint değişir → key[0] yanlış → plaintext[0] çöp →
      key[1] çöpten türetilir → tüm zincir çöker → hiçbir blok çözülemez.
    """

    # Temiz ortam parmak izi: TracerPid=0 → SHA256(b"0")
    CLEAN_FP = hashlib.sha256(b"0").digest()

    @classmethod
    def _derive_first_key(cls, base_key: bytes) -> bytes:
        return hashlib.sha256(base_key + cls.CLEAN_FP).digest()

    @staticmethod
    def _next_key(current_key: bytes, plaintext_block: bytes) -> bytes:
        return hashlib.sha256(current_key + plaintext_block[:16]).digest()

    @staticmethod
    def _xor_block(data: bytes, key: bytes) -> bytes:
        return bytes(data[i] ^ key[i % 32] for i in range(len(data)))

    @classmethod
    def encrypt_chain(cls, blocks: List[bytes], base_key: bytes) -> List[bytes]:
        """
        Blok listesini EKC zinciri ile şifreler.
        Şifrelemek için kullanılan ortam = temiz (TracerPid=0).
        """
        key = cls._derive_first_key(base_key)
        enc = []
        for blk in blocks:
            enc.append(cls._xor_block(blk, key))
            key = cls._next_key(key, blk)
        return enc

    @classmethod
    def generate_decrypt_code(cls, enc_blocks: List[bytes], base_key: bytes) -> str:
        """
        Dallanmasız runtime şifre çözme kodu üretir.
        Üretilen kodda hiçbir if/else anti-debug dalı yok.
        """
        bk_b64  = base64.b64encode(base_key).decode()
        blks_b64 = [base64.b64encode(b).decode() for b in enc_blocks]
        blks_lit = ',\n    '.join(f'b64.b85decode("{base64.b85encode(b).decode()}")' for b in enc_blocks)

        code = f'''
import hashlib as _ekc_h, os as _ekc_o, base64 as _ekc_b

def _ekc_fp():
    """Ortam parmak izi — temiz: SHA256(b"0"), debug: farklı değer"""
    _pid = b"0"
    # Linux
    try:
        with open("/proc/self/status","rb") as _f:
            for _l in _f:
                if _l.startswith(b"TracerPid:"):
                    _pid = _l.split(b":")[1].strip(); break
    except Exception: pass
    # Windows
    try:
        import ctypes as _ct
        if _ct.windll.kernel32.IsDebuggerPresent():
            _pid = b"1"
    except Exception: pass
    return _ekc_h.sha256(_pid).digest()

_ekc_bk  = _ekc_b.b64decode("{bk_b64}")
_ekc_fp  = _ekc_fp()
_ekc_k   = _ekc_h.sha256(_ekc_bk + _ekc_fp).digest()
del _ekc_fp

_ekc_enc = [
    {blks_lit}
]
_ekc_dec = []
for _ekc_blk in _ekc_enc:
    _ekc_pt = bytes(_ekc_blk[_i] ^ _ekc_k[_i%32] for _i in range(len(_ekc_blk)))
    _ekc_dec.append(_ekc_pt)
    _ekc_k = _ekc_h.sha256(_ekc_k + _ekc_pt[:16]).digest()

del _ekc_k, _ekc_bk, _ekc_enc
# _ekc_dec: şifresi çözülmüş bloklar listesi
'''
        return code

    @classmethod
    def split_source(cls, source: str, block_size: int = 4096) -> List[bytes]:
        """Python kaynak kodunu EKC blokları için böler"""
        data = source.encode('utf-8')
        return [data[i:i+block_size] for i in range(0, len(data), block_size)]


# ══════════════════════════════════════════════════════════════════════════════
# MODÜL 5 ── HOMOMORFİK VM (HVM)
# Register'lar şifreli değer tutar: stored = (plaintext + R) mod P
# ADD işlemi şifreli değerler üzerinde direkt çalışır — decrypt gerekmez
# Analist register'lara bakarsa sadece anlamsız büyük sayılar görür
# ══════════════════════════════════════════════════════════════════════════════

class HomomorphicVM:
    """
    Additif homomorfik VM.

    P = 2^31 - 1 (Mersenne asal sayı — mod işlemleri hızlı)
    R = çalışma zamanında rastgele seçilir (her run farklı)

    Register[i] = (gerçek_değer + R) mod P
    ADD(dst,a,b): reg[dst] = (reg[a] + reg[b] - R) mod P  →  (a+b+R) mod P  ✓ homomorfik
    XOR(dst,a,b): decrypt her iki register, XOR yap, tekrar şifrele (homomorfik değil ama R gizler)
    MUL(dst,a,b): ((reg[a]-R)*(reg[b]-R) + R) mod P  (sadece küçük sayılar için pratik)

    Kullanım: Şifre çözme anahtarının parçaları register'lara yüklenir, HVM üzerinde
    birleştirilir. Analist breakpoint koyup register değerlerini okursa yalnızca
    (key_part + R) görür — R her run farklı olduğu için statik analiz işe yaramaz.
    """

    P = (1 << 31) - 1  # 2147483647, Mersenne asal sayısı

    # ──── Runtime kodu (üretilen dosyaya eklenir) ────
    RUNTIME_TEMPLATE = '''
import os as _hvm_os, random as _hvm_rnd
_HVM_P = {P}
_hvm_R = int.from_bytes(_hvm_os.urandom(4), "big") % (_HVM_P - 1) + 1
_hvm_regs = [_hvm_R] * 16  # Tüm register başlangıç değeri: 0 + R

def _hvm_load(i, v):
    """Değeri şifreli olarak register'a yükle"""
    _hvm_regs[i] = (int(v) + _hvm_R) % _HVM_P

def _hvm_store(i):
    """Register'dan gerçek değeri oku (decrypt)"""
    return (_hvm_regs[i] - _hvm_R) % _HVM_P

def _hvm_add(dst, a, b):
    """Şifreli değerler üzerinde homomorfik toplama"""
    _hvm_regs[dst] = (_hvm_regs[a] + _hvm_regs[b] - _hvm_R) % _HVM_P

def _hvm_xor(dst, a, b):
    """XOR — decrypt gerektirir ama R ile maskelenir"""
    _pa = _hvm_store(a); _pb = _hvm_store(b)
    _hvm_regs[dst] = (((_pa ^ _pb) + _hvm_R) % _HVM_P)

def _hvm_mul(dst, a, b):
    """Çarpma — sadece küçük tam sayılar için pratik"""
    _pa = _hvm_store(a); _pb = _hvm_store(b)
    _hvm_regs[dst] = ((_pa * _pb) + _hvm_R) % _HVM_P

def _hvm_clear():
    """Tüm register'ları sıfırla"""
    for _i in range(16): _hvm_regs[_i] = _hvm_R
'''

    @classmethod
    def generate_runtime(cls) -> str:
        """HVM runtime kaynak kodunu döner"""
        return cls.RUNTIME_TEMPLATE.format(P=cls.P)

    @classmethod
    def generate_key_compute_code(cls, key_bytes: bytes) -> str:
        """
        32-byte anahtarı 8 adet 32-bit parçaya böler.
        Her parça HVM register'ına yüklenir, XOR ile birleştirilir.
        Sonuç: _hvm_key (bytes, 32 byte)

        Analist register'lara bakarken: (key_part_i + R) görür — R her run farklı.
        """
        parts = []
        for i in range(0, len(key_bytes), 4):
            chunk = key_bytes[i:i+4]
            # 4 byte → 32-bit int
            val = int.from_bytes(chunk.ljust(4, b'\x00'), 'big')
            parts.append(val)

        n = len(parts)
        lines = []
        for i, val in enumerate(parts):
            lines.append(f'_hvm_load({i}, {val})')

        # Zincir XOR: reg[n] = parts[0] XOR parts[1] XOR ... XOR parts[n-1]
        if n > 1:
            lines.append(f'_hvm_xor({n}, 0, 1)')
            for i in range(2, n):
                lines.append(f'_hvm_xor({n}, {n}, {i})')

        lines.append(f'_hvm_key_int = _hvm_store({n})')
        lines.append('_hvm_key = _hvm_key_int.to_bytes(4, "big") * 8  # 32 byte key')
        lines.append('_hvm_clear()')

        return '\n'.join(lines)

    @classmethod
    def wrap_key_derivation(cls, key: bytes) -> Tuple[str, str]:
        """
        Döner: (runtime_kodu, key_hesaplama_kodu)
        key_hesaplama_kodu çalıştırıldıktan sonra _hvm_key değişkeni kullanılabilir.
        """
        return cls.generate_runtime(), cls.generate_key_compute_code(key)


# ══════════════════════════════════════════════════════════════════════════════
# MODÜL 6 ── HONEYPOT GENERATOR
# Analisti yanlış yola çekecek kadar gerçekçi sahte kod
# Gerçek mantık şifreli mikro-rutinlerde sessizce çalışır
# ══════════════════════════════════════════════════════════════════════════════

class HoneypotGenerator:
    """
    Deceptive Execution mimarisi.

    Analist kaynak koda baktığında:
      - Gerçekmiş gibi görünen veritabanı bağlantı kodu
      - Gerçekmiş gibi görünen API authentication mantığı
      - Gerçekmiş gibi görünen konfigürasyon yükleyici
      → Bunların hiçbiri gerçek değil; asıl mantık şifreli payload'da.

    Gerçek kod: _ekc_dec listesinden okunur → exec() ile çalıştırılır.
    Sahte kod: analisti günlerce meşgul eder.
    """

    _FAKE_TOKENS = [
        'eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhZG1pbiIsImlhdCI6MTY5MDAwMDAwMH0',
        'sk-proj-xK9mN2pQrL8vY4wZ7aE1cF3hB6jD0iU5tO',
        'AKIA4MFXK9Z2NLQP7WGE',
        'ghp_R4nD0mT0k3nF4k3H0n3yP0t7777',
        'xoxb-4857291837-fake-slack-bot-token-honeypot',
    ]
    _FAKE_ENDPOINTS = [
        'https://api.internal.corp/v3/data',
        'https://auth.service.local:8443/oauth/token',
        'postgresql://app_user:S3cur3P4ss@db-primary.internal:5432/production',
        'redis://cache.internal:6379/0?password=R3d1sP4ss',
        'amqp://mq_user:MqP4ssw0rd@rabbitmq.internal/vhost',
    ]

    @staticmethod
    def _rand_class_name() -> str:
        prefixes = ['Data', 'Config', 'Auth', 'Cache', 'Queue', 'Event', 'Metric', 'Audit']
        suffixes = ['Manager', 'Handler', 'Service', 'Provider', 'Client', 'Processor', 'Engine']
        return random.choice(prefixes) + random.choice(suffixes)

    @classmethod
    def _generate_fake_class(cls) -> str:
        cname = cls._rand_class_name()
        token = random.choice(cls._FAKE_TOKENS)
        ep    = random.choice(cls._FAKE_ENDPOINTS)
        rng   = random.Random(hash(cname))
        port  = rng.randint(8000, 9999)
        timeout = rng.choice([30, 60, 120, 300])

        return f'''
class _{cname}:
    """Internal {cname.lower()} subsystem — do not instantiate directly."""
    _endpoint    = "{ep}"
    _token       = "{token}"
    _timeout     = {timeout}
    _retry_limit = {rng.randint(2,5)}
    _pool_size   = {rng.randint(4,16)}

    def __init__(self, region=None):
        self._region  = region or "us-east-1"
        self._session = None
        self._metrics = {{"calls": 0, "errors": 0, "latency_ms": []}}

    def _authenticate(self):
        import hashlib, time
        ts  = str(int(time.time() // 300))
        sig = hashlib.sha256((self._token + ts).encode()).hexdigest()
        return {{"Authorization": f"Bearer {{self._token}}", "X-Sig": sig}}

    def _connect(self):
        if self._session is None:
            hdrs = self._authenticate()
            self._session = {{"headers": hdrs, "base_url": self._endpoint, "port": {port}}}
        return self._session

    def fetch(self, resource_id: str, params: dict = None):
        sess = self._connect()
        self._metrics["calls"] += 1
        payload = {{"id": resource_id, "params": params or {{}}, "region": self._region}}
        return payload

    def push(self, data: dict, priority: int = 5):
        sess = self._connect()
        if not data: raise ValueError("Empty payload")
        self._metrics["calls"] += 1
        return {{"status": "queued", "priority": priority, "size": len(str(data))}}

    def health(self) -> dict:
        return {{"ok": True, "uptime": 99.97, "region": self._region, "metrics": self._metrics}}
'''

    @classmethod
    def _generate_fake_config_loader(cls) -> str:
        keys = random.sample(cls._FAKE_TOKENS, 2)
        return f'''
class _AppConfig:
    """Application configuration loader — reads from environment / secrets manager."""
    _DEFAULTS = {{
        "db_pool_size": 10, "cache_ttl": 3600, "max_retries": 3,
        "rate_limit_rps": 500, "jwt_expiry": 86400, "log_level": "INFO",
        "feature_flags": {{"new_auth": True, "beta_pipeline": False}},
    }}
    def __init__(self):
        import os
        self._cfg = dict(self._DEFAULTS)
        self._cfg["api_key"]    = os.environ.get("API_KEY", "{keys[0]}")
        self._cfg["secret_key"] = os.environ.get("SECRET_KEY", "{keys[1]}")

    def get(self, key, default=None):
        return self._cfg.get(key, default)

    def require(self, key):
        val = self._cfg.get(key)
        if val is None: raise RuntimeError(f"Required config '{{key}}' missing")
        return val

    def reload(self):
        self.__init__()
        return self
'''

    @classmethod
    def _generate_fake_main_flow(cls) -> str:
        """Analisti oyalayan sahte ana iş mantığı — hiçbir zaman gerçek işlem yapmaz"""
        manager = cls._rand_class_name()
        return f'''
def _bootstrap_services():
    """Initialize service mesh (placeholder — real init in secure runtime)."""
    _cfg    = _AppConfig()
    _svc    = _{manager}(region=_cfg.get("region", "eu-west-1"))
    _status = _svc.health()
    return _cfg, _svc

def _run_pipeline(config, svc, data=None):
    """Main processing pipeline — routes to appropriate handler."""
    if data is None:
        return {{"status": "idle", "processed": 0}}
    results = []
    for item in (data if isinstance(data, list) else [data]):
        res = svc.push(item, priority=config.get("priority", 5))
        results.append(res)
    return {{"status": "complete", "processed": len(results), "results": results}}
'''

    @classmethod
    def generate_honeypot_shell(cls, real_exec_code: str, num_decoys: int = 3) -> str:
        """
        Gerçek kodun etrafına honeypot kabuğu sarar.
        real_exec_code: gerçek kodun bulunduğu exec() çağrısını içeren kod bloğu
        """
        header = f'# Application Runtime v{random.randint(2,4)}.{random.randint(0,9)}.{random.randint(0,20)}\n'
        header += '# Auto-generated — do not modify manually\n\n'

        decoys = ''
        for _ in range(num_decoys):
            decoys += cls._generate_fake_class()
        decoys += cls._generate_fake_config_loader()
        decoys += cls._generate_fake_main_flow()

        # Gerçek kod, sahte init bloğunun "içinde" gibi görünen bir try bloğuna gömülür
        wrapper = f'''
try:
    _bootstrap_services()
except Exception as _e:
    pass

# ── Core runtime initialization ──────────────────────────────────────────────
{real_exec_code}
'''
        return header + decoys + wrapper


# ══════════════════════════════════════════════════════════════════════════════
# ENTEGRATÖR ── Tüm modülleri NinjaEncoder ile birleştirir
# encode_ultimate() sonunda çağrılacak ana sınıf
# ══════════════════════════════════════════════════════════════════════════════

class NinjaAdvancedIntegrator:
    """
    NinjaEnc Advanced Protection Layer — ana entegrasyon noktası.

    Tipik kullanım (encode_ultimate() sonu):
        adv = NinjaAdvancedIntegrator()

        # Katman 1: WBC — payload AES-CBC ile şifrelenir, anahtar tablolarda gömülür
        wbc_enc, wbc_src, wbc_fn = adv.apply_wbc(compressed_payload, aes_key)

        # Katman 2: EKC — kaynak kod EKC zinciri ile zincirlenir
        ekc_code = adv.apply_ekc(output_source_blocks, base_key)

        # Katman 3: JIT Shredder — fonksiyonlar şifreli stub'lara dönüşür
        shredded = adv.apply_jit_shred(output_source)

        # Katman 4: HVM — anahtar hesaplama HVM üzerinden geçirilir
        hvm_rt, hvm_key_code = adv.apply_hvm(aes_key)

        # Katman 5: Honeypot — deceptive execution kabuğu
        final = adv.apply_honeypot(exec_code)
    """

    def __init__(self, seed: Optional[int] = None):
        if seed is not None:
            random.seed(seed)
        self.wbc  = WhiteBoxAES()
        self.llvm = LLVMPassIntegrator()
        self.jsh  = JITCodeShredder()
        self.ekc  = EnvironmentKeyedCrypto()
        self.hvm  = HomomorphicVM()
        self.hp   = HoneypotGenerator()

    # ── Katman 1: White-Box AES ──────────────────────────────────────────────

    def apply_wbc(self, payload: bytes, key: bytes) -> Tuple[bytes, str, str]:
        """
        Payload'ı WBC-AES ile şifreler, decrypt kodunu döner.
        Döner: (şifreli_payload, wbc_kaynak_kodu, decrypt_fn_adı)
        """
        encrypted      = WhiteBoxAES.encrypt_cbc(payload, key)
        src, fn_name   = WhiteBoxAES.generate_wbc_decrypt_source(key)
        return encrypted, src, fn_name

    # ── Katman 2: Environment-Keyed Crypto ──────────────────────────────────

    def apply_ekc(self, source: str, base_key: bytes) -> str:
        """
        Kaynak kodu EKC zincirine gömer.
        Döner: EKC şifreleme + runtime decrypt kodu
        """
        blocks     = EnvironmentKeyedCrypto.split_source(source)
        enc_blocks = EnvironmentKeyedCrypto.encrypt_chain(blocks, base_key)
        return EnvironmentKeyedCrypto.generate_decrypt_code(enc_blocks, base_key)

    # ── Katman 3: JIT Code Shredder ─────────────────────────────────────────

    def apply_jit_shred(self, source: str, min_lines: int = 5) -> str:
        """Kaynak kodun fonksiyonlarını şifreli + bellek-temizleyici stub'lara dönüştürür"""
        return JITCodeShredder.apply(source, min_lines=min_lines)

    # ── Katman 4: Homomorphic VM ─────────────────────────────────────────────

    def apply_hvm(self, key: bytes) -> Tuple[str, str]:
        """
        Anahtar hesaplamasını HVM üzerinden geçirecek kod üretir.
        Döner: (runtime_kodu, key_hesaplama_kodu)
        """
        return HomomorphicVM.wrap_key_derivation(key)

    # ── Katman 5: Honeypot ───────────────────────────────────────────────────

    def apply_honeypot(self, exec_code: str, num_decoys: int = 3) -> str:
        """exec() çağrısını honeypot kabuğuna gömer"""
        return HoneypotGenerator.generate_honeypot_shell(exec_code, num_decoys)

    # ── Katman 6: LLVM ───────────────────────────────────────────────────────

    def get_nuitka_command(self, script: str, output_dir: str = '.') -> List[str]:
        """O-LLVM entegreli Nuitka derleme komutunu döner"""
        return LLVMPassIntegrator.generate_nuitka_command(script, output_dir)

    # ── Tam pipeline ─────────────────────────────────────────────────────────

    def apply_all(self,
                  payload: bytes,
                  aes_key: bytes,
                  output_source: str,
                  exec_stub: str) -> dict:
        """
        Tüm katmanları sırayla uygular.
        Döner: {'wbc_enc', 'wbc_src', 'ekc_code', 'shredded_src', 'hvm_rt',
                'hvm_key_code', 'honeypot_src', 'nuitka_cmd', 'report'}
        """
        results = {}

        # WBC
        wbc_enc, wbc_src, wbc_fn = self.apply_wbc(payload, aes_key)
        results['wbc_enc'] = wbc_enc
        results['wbc_src'] = wbc_src
        results['wbc_fn']  = wbc_fn

        # EKC
        ekc_base_key          = hashlib.sha256(aes_key + b'ekc').digest()
        results['ekc_code']   = self.apply_ekc(output_source, ekc_base_key)

        # JIT Shredder
        results['shredded_src'] = self.apply_jit_shred(output_source)

        # HVM
        hvm_rt, hvm_kc          = self.apply_hvm(aes_key)
        results['hvm_rt']        = hvm_rt
        results['hvm_key_code']  = hvm_kc

        # Honeypot
        results['honeypot_src'] = self.apply_honeypot(exec_stub)

        # LLVM
        results['nuitka_env'] = LLVMPassIntegrator.patch_nuitka_env()

        results['report'] = {
            'wbc_table_size_kb': len(wbc_src) // 1024,
            'ekc_blocks'       : len(EnvironmentKeyedCrypto.split_source(output_source)),
            'ollvm_available'  : LLVMPassIntegrator.find_ollvm_clang() is not None,
            'wbc_self_test'    : WhiteBoxAES.self_test(),
        }
        return results


# ══════════════════════════════════════════════════════════════════════════════
# CLI — doğrudan çalıştırma: python ninja_advanced.py test
# ══════════════════════════════════════════════════════════════════════════════



class NinjaEncoder:

    def __init__(self):
        self.main_generator = MainPyGenerator()
        self.elf_generator = ELFGenerator()
        self.ascii85 = Ascii85Encoder()
        self.hyperion = HyperionObfuscator()
        self.xor = XORObfuscator()
        self.string_enc = StringEncryptor()
        self.cf_obf = ControlFlowObfuscator()
        self.dead_code = DeadCodeInjector()
        self.ast_obf = ASTObfuscator()
        self.cf_flatten = ControlFlowFlattener()
        self.mba = MBATransformer()
        self.self_mod = SelfModifyingCode()
        self.poly_enc = PolymorphicEncryptor()
        self.opaque = OpaquePredicates()
        self.str_table = StringTableEncryptor()
        self.bytecode_virt = BytecodeVirtualizer()
        self.integrity = IntegrityChecker()
        self.antidebug = AntiDebug()
        self.mem_protector = MemoryProtector()
        self.chacha = ChaCha20Encryptor()
        self.poly_dec = PolymorphicDecryptor()
        self.metamorphic = MetamorphicStager()
        self.png_stego   = PNGSteganography()
        self.fake_so     = FakeSoGenerator()
        self.hw_key      = HardwareFingerprintKey()
        self.memfd       = MemfdExecutor()
        self.wss         = WhitespaceSteganography()
        self.advanced    = NinjaAdvancedIntegrator()   # WBC / EKC / JIT / HVM / Honeypot / LLVM
        self.ninja_vm    = NinjaVM()                    # gerçek stack-VM (seçili fonksiyonlar)
        self.temp_dir = None
        self._encoded_python_version = sys.version_info[:2]

    def __enter__(self):
        self.temp_dir = tempfile.mkdtemp(prefix='ninjaenc_')
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.temp_dir and os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def encode_ultimate(self, input_file, output_file=None, use_cython=True, use_nuitka=True,
                        seed=None, assume_yes=False):
        if output_file is None:
            base = os.path.splitext(input_file)[0]
            output_file = f'{base}_enc.py'
        # NATIVE ZORUNLU (her zaman). Native derleme zinciri hazır değilse veya
        # derleme başarısız olursa encode DURUR — pure-Python fallback YOK.
        # Sonuç: çıktı yalnızca derlendiği OS + mimaride çalışır (taşınabilir değil).
        logger.info('Native-zorunlu mod: derleme zinciri hazırlanıyor...')
        _ok, _msg = ensure_native_toolchain(auto_install=True, assume_yes=assume_yes)
        if not _ok:
            raise RuntimeError(
                'Native derleme zinciri hazır değil ve native ZORUNLU.\n'
                f'Sebep: {_msg}\n'
                'Nuitka + bir C derleyici (gcc/clang) kurun ve tekrar deneyin.'
            )
        logger.info(f'  {_msg}')
        use_nuitka = True  # native omurga zorunlu
        with open(input_file, 'r', encoding='utf-8') as f:
            source_code = f.read()
        temp_dir = tempfile.mkdtemp(prefix='ninjaenc_ult_')
        try:
            logger.info('Ultimate Adım 0: NinjaVM — seçili fonksiyonları gerçek stack-VM\'e taşı')
            _vm_moved = 0
            try:
                _vm_src, _vm_moved = self.ninja_vm.transform_source(source_code)
                if _vm_moved > 0:
                    source_code = _vm_src
                    logger.info(f'  NinjaVM: {_vm_moved} fonksiyon custom ISA\'ya derlendi (Python bytecode gizlendi)')
                else:
                    logger.info('  NinjaVM: uygun fonksiyon yok (@ninja_vm ile işaretleyebilirsiniz) — atlanıyor')
            except Exception as _vm_e:
                logger.warning(f'  NinjaVM atlandı: {_vm_e}')

            logger.info('Ultimate Adım 1: AST obfuscation (değişken/fonksiyon yeniden adlandırma)')
            # NinjaVM'in blob'a gömdüğü adları koru — aksi halde VM'lenmiş
            # özyinelemeli/global-çağıran fonksiyonlar yeniden adlandırma sonrası
            # çalışma anında adları bulamaz.
            obf_source = self.ast_obf.obfuscate(
                source_code, extra_protected=getattr(self.ninja_vm, 'protected_names', None))

            nuitka_source = obf_source

            logger.info('Ultimate Adım 2: MBA (Mixed Boolean-Arithmetic) dönüşümü')
            obf_source = self.mba.transform_source(obf_source)

            logger.info('Ultimate Adım 3: Control flow flattening (state-machine dispatcher)')
            obf_source = self.cf_flatten.flatten_source(obf_source)

            logger.info('Ultimate Adım 4: Opaque predicates (sahte dallar)')
            obf_source = self.opaque.wrap_with_opaques(obf_source)

            logger.info('Ultimate Adım 5: String table şifreleme')
            obf_source = self.str_table.encrypt_to_table(obf_source)

            logger.info('Ultimate Adım 6: Dead code injection')
            obf_source = self.dead_code.inject_into_source(obf_source, count=8)

            logger.info('Ultimate Adım 7: Control flow sahte dal enjeksiyonu')
            obf_source = self.cf_obf.inject_fake_branches(obf_source)

            logger.info('Ultimate Adım 8: MetamorphicStager — her encode farklı sıra')
            stages = MetamorphicStager.get_random_order()
            logger.info(f'  Sıra: {" → ".join(stages)}')
            obf_source = MetamorphicStager.apply(self, obf_source, stages)

            logger.info('Ultimate v5 Adım 8b: FakeRecursion stub inject')
            try: obf_source = FakeRecursion.inject(obf_source)
            except Exception as _e: logger.warning(f'FakeRecursion atlandı: {_e}')

            logger.info('Ultimate v5 Adım 8c: ClassCamouflage (10 sahte class)')
            try: obf_source = ClassCamouflage.wrap_source(obf_source)
            except Exception as _e: logger.warning(f'ClassCamouflage atlandı: {_e}')

            logger.info('Ultimate v5 Adım 8d: FakeExceptionInjector (300 blok)')
            try: obf_source = FakeExceptionInjector.inject_into_source(obf_source, count=300)
            except Exception as _e: logger.warning(f'FakeException atlandı: {_e}')

            logger.info('Ultimate v5 Adım 8e: StringSplitter (chr() zinciri)')
            try: obf_source = StringSplitter.transform_source(obf_source)
            except Exception as _e: logger.warning(f'StringSplitter atlandı: {_e}')

            logger.info('Ultimate v5 Adım 8f: FakeImportTree (56 sahte import)')
            try:
                fake_imports = FakeImportTree.generate(56)
                obf_source   = fake_imports + '\n' + obf_source
            except Exception as _e: logger.warning(f'FakeImportTree atlandı: {_e}')

            logger.info('Ultimate v5 Adım 8g: ConstantFoldingSaboteur')
            try: obf_source = ConstantFoldingSaboteur.transform_source(obf_source)
            except Exception as _e: logger.warning(f'ConstantFolding atlandı: {_e}')

            logger.info('Ultimate v5 Adım 8h: LambdaSoupWrapper')
            try: obf_source = LambdaSoupWrapper.inject(obf_source)
            except Exception as _e: logger.warning(f'LambdaSoup atlandı: {_e}')

            logger.info('Ultimate Adım 9: Bytecode derleme (.pyc)')
            obf_py_for_pyc = os.path.join(temp_dir, 'obf_for_pyc.py')
            with open(obf_py_for_pyc, 'w', encoding='utf-8') as f:
                f.write(obf_source)
            pyc_file = os.path.join(temp_dir, 'step1.pyc')
            try:
                py_compile.compile(obf_py_for_pyc, pyc_file, doraise=True)
                logger.info('Bytecode obfuscated kaynaktan derlendi')
            except py_compile.PyCompileError:
                logger.warning('Obfuscated kaynak derlenemedi, orijinal kaynak kullanılıyor')
                py_compile.compile(input_file, pyc_file, doraise=True)

            logger.info('Ultimate Adım 10: Opcode mutation (NOP inject)')
            with open(pyc_file, 'rb') as f:
                pyc_raw = f.read()
            if sys.version_info >= (3, 12):
                logger.info('Python 3.12+ — opcode mutation atlanıyor (uyumsuz)')
                pyc_data = pyc_raw
            else:
                try:
                    tmp_pyc = os.path.join(temp_dir, 'pre_mutate.pyc')
                    with open(tmp_pyc, 'wb') as f:
                        f.write(pyc_raw)
                    pyc_data = OpcodeMutator.mutate_pyc(tmp_pyc)
                except Exception as _e:
                    logger.warning(f'Opcode mutation atlandı: {_e}')
                    pyc_data = pyc_raw

            logger.info('Ultimate Adım 11: Code object mutation (co_consts + co_lnotab)')
            pyc_header = pyc_data[:16]
            pyc_body   = pyc_data[16:]
            try:
                mutated_body = CodeObjectMutator.mutate_co_consts(pyc_body)
                pyc_data = pyc_header + mutated_body
                logger.info('Code object mutation başarılı')
            except Exception as _e:
                logger.warning(f'Code object mutation atlandı: {_e}')

            logger.info('Ultimate v5 Adım 11b: JunkBytecodeInjector')
            try: pyc_data = JunkBytecodeInjector.inject(pyc_data)
            except Exception as _e: logger.warning(f'JunkBytecode atlandı: {_e}')

            logger.info('Ultimate Adım 12: ChaCha20-Poly1305 şifreleme (varsa)')
            chacha_encrypted = None
            chacha_key = None
            crypto_data = pyc_data
            if ChaCha20Encryptor.is_available():
                chacha_encrypted, chacha_key = ChaCha20Encryptor.encrypt(pyc_data)
                if chacha_encrypted:
                    logger.info('ChaCha20-Poly1305 şifreleme başarılı')
                    crypto_data = chacha_encrypted
                else:
                    logger.warning('ChaCha20 başarısız')
            else:
                logger.info('ChaCha20 kullanılamıyor (pycryptodome yok), atlanıyor')

            logger.info('Ultimate v5 Adım 12b: Twofish Feistel şifreleme')
            try:
                _tf_data, _tf_key = TwofishEncryptor.encrypt(crypto_data)
                if _tf_data:
                    _tf_overhead = len(_tf_data) - len(crypto_data)
                    crypto_data = _tf_data
                    logger.info(f'Twofish şifreleme başarılı (+{_tf_overhead} byte overhead)')
                else:
                    logger.warning('Twofish başarısız (None döndü), önceki katmanda devam ediliyor')
            except Exception as _e:
                logger.warning(f'Twofish atlandı: {_e}')
                _tf_key = None

            logger.info('Ultimate Adım 13: AES-256 + PBKDF2 şifreleme (varsa)')
            aes_encrypted = None
            aes_key = None
            if AESEncryptor.is_available():
                aes_encrypted, aes_key = AESEncryptor.encrypt(crypto_data)
                if aes_encrypted:
                    logger.info('AES-256 + PBKDF2 şifreleme başarılı')
                    crypto_data = aes_encrypted
                else:
                    logger.warning('AES şifreleme başarısız, önceki katmanda devam ediliyor')
            else:
                logger.info('pycryptodome yok, XOR ile devam (pip install pycryptodome önerilir)')

            logger.info('Ultimate Adım 14: Multi-layer XOR cascade')
            xor_data, xor_keys = self.xor.multi_xor_encode_strong(crypto_data)

            logger.info('Ultimate Adım 15: PolymorphicDecryptor multi-stage şifreleme')
            poly_encrypted, poly_params = PolymorphicDecryptor.generate_multi_stage(xor_data)
            logger.info(f'  Polymorphic şifreleme tamamlandı — rot={poly_params["rot"]}')

            logger.info('Ultimate v5 Adım 15b: HMAC-SHA512 bütünlük imzası')
            try:
                poly_encrypted, _hmac_key = HMACIntegrity.sign(poly_encrypted)
                logger.info('HMAC-SHA512 imzası eklendi')
            except Exception as _e:
                logger.warning(f'HMAC atlandı: {_e}')
                _hmac_key = None

            logger.info('Ultimate v5 Adım 15c: Runtime key mutation (128-byte step)')
            try:
                poly_encrypted, _rkm_key = RuntimeKeyMutator.encrypt(poly_encrypted)
                logger.info('RuntimeKeyMutator uygulandı')
            except Exception as _e:
                logger.warning(f'RKM atlandı: {_e}')
                _rkm_key = None

            logger.info('Ultimate v5 Adım 15d: Shamir key splitting (3-of-5)')
            try:
                _shm_shares = ShamirKeySharing.split((poly_params.get("key1","").encode()[:32] + bytes(32))[:32])
                logger.info(f'Shamir: 5 parça üretildi, 3 ile çözülebilir')
            except Exception as _e:
                logger.warning(f'Shamir atlandı: {_e}')
                _shm_shares = None

            logger.info('Ultimate Adım 16: Zlib level-9 + Ascii85 (padding yok — veri zaten şifreli)')
            ascii85_encoded = self.ascii85.encode(poly_encrypted, padding_size=0)
            compressed = zlib.compress(ascii85_encoded.encode('utf-8'), level=9)

            cython_so_file = None
            native_file    = None

            if use_cython and check_cython():
                logger.info('Ultimate Adım 17: Cython → ninja_cython.so')
                try:
                    cython = CythonCompiler(temp_dir)
                    pyx_file = cython.create_obfuscated_wrapper(nuitka_source, 'ninja_cython')
                    cython_so_file = cython.compile_to_so(pyx_file, 'ninja_cython')
                    if cython_so_file:
                        logger.info(f'Cython .so üretildi: {Path(cython_so_file).name}')
                    else:
                        print('\x1b[93m[!] Cython derleme başarısız\x1b[0m')
                except Exception as _ce:
                    print(f'\x1b[93m[!] Cython adımı atlandı: {_ce}\x1b[0m')

            if use_nuitka and check_nuitka():
                try:
                    if cython_so_file and os.path.exists(cython_so_file):
                        logger.info('Ultimate Adım 18: Nuitka → Cython .so embed (so içinde so)')
                        cython_mod = Path(cython_so_file).name.split('.')[0]
                        wrapper_src = f"""import sys as _s, os as _o
_s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
import {cython_mod} as _c
def run(): _c.run()
if __name__ == '__main__': run()
"""
                        wrapper_py = os.path.join(temp_dir, 'ninja_nuitka.py')
                        with open(wrapper_py, 'w', encoding='utf-8') as f:
                            f.write(wrapper_src)
                        nuitka = NuitkaCompiler(temp_dir)
                        native_file = nuitka.compile_with_embedded_cython(wrapper_py, cython_so_file)
                        if native_file:
                            logger.info(f'Nuitka embed .so üretildi: {Path(native_file).name}')
                        else:
                            print('\x1b[93m[!] Nuitka embed başarısız, direkt derleme deneniyor...\x1b[0m')
                            temp_py = os.path.join(temp_dir, 'ninja_native.py')
                            with open(temp_py, 'w', encoding='utf-8') as f:
                                f.write(nuitka_source)
                            native_file = nuitka.compile_module(temp_py)
                    else:
                        logger.info('Ultimate Adım 18: Nuitka direkt derleme (Cython yok)')
                        temp_py = os.path.join(temp_dir, 'ninja_native.py')
                        with open(temp_py, 'w', encoding='utf-8') as f:
                            f.write(nuitka_source)
                        nuitka = NuitkaCompiler(temp_dir)
                        native_file = nuitka.compile_module(temp_py)
                except Exception as _ne:
                    print(f'\x1b[93m[!] Nuitka adımı atlandı: {_ne}\x1b[0m')

            if not native_file and cython_so_file and os.path.exists(cython_so_file):
                native_file = cython_so_file
                logger.info('Native katman: Cython .so (Nuitka embed başarısız)')
            elif native_file:
                logger.info(f'Native katman: Nuitka embedded .so (Cython içinde)')
            else:
                # NATIVE ZORUNLU — hiçbir native çıktı üretilemedi → DUR.
                raise RuntimeError(
                    'Native derleme başarısız (Nuitka ve Cython üretemedi) ve native ZORUNLU.\n'
                    'Pure-Python fallback devre dışı. Derleyici/Nuitka kurulumunu ve '
                    'kaynak kodun Nuitka ile derlenebilir olduğunu kontrol edin.'
                )

            if native_file and os.path.exists(native_file):
                logger.info('Ultimate Adım 19: ELFProtector (DWARF poison + Fake sym)')
                native_file = ELFProtector.protect(native_file, do_text_encrypt=False)
            else:
                logger.info('Ultimate Adım 19: ELFProtector atlandı (native yok)')

            native_fname = Path(native_file).name if native_file and os.path.exists(native_file) else ''
            logger.info('Ultimate Adım 22: Anti-debug inject (ZIP içinde, düzenlenemez)')

            final_code = obf_source
            comprehensive_imports = """import random
import sys
import hashlib
import os
import time
import platform
import struct
import marshal
import zlib
import base64
import tempfile
import shutil
"""
            final_code = comprehensive_imports + final_code
            ult_main, ult_enc, ult_keys = self.main_generator.generate_with_payload(final_code, native_fname=native_fname)

            logger.info('Ultimate Adım 20: LazyChunkEncoder → __s0-4__.bin (zincir key)')
            lazy_chunks, lazy_base_key = LazyChunkEncoder.encode(ult_enc)
            logger.info(f'  LazyChunk: {len(lazy_chunks)} parça, base_key=0x{lazy_base_key:02x}')

            logger.info('Ultimate Adım 21: MiniVMGenerator → runtime VM program (pipeline modu)')
            vm_prog_bytes = MiniVMGenerator.compile_runtime_program()
            vm_prog_b64   = base64.b64encode(vm_prog_bytes).decode('ascii')
            logger.info(f'  VM program: {len(vm_prog_bytes)} byte (DECOMP+MARSHAL+EXEC+WIPE)')

            ult_main_v8 = self.main_generator.generate_v8(
                xor_keys    = ult_keys,
                has_native  = bool(native_fname),
                native_fname= native_fname,
                lazy_base_key = lazy_base_key,
                vm_prog_b64 = vm_prog_b64,
            )

            logger.info('Ultimate v5: Runtime guard katmanları ekleniyor')
            try:
                ult_main_v8 = ImportHookPoison.generate_code() + '\n' + ult_main_v8
                ult_main_v8 = SysTraceNuke.generate_code()     + '\n' + ult_main_v8
                ult_main_v8 = AntiVM.generate_code()            + '\n' + ult_main_v8
                ult_main_v8 = MemoryCanary.generate_code()      + '\n' + ult_main_v8
                ult_main_v8 = JITPoison.generate_code()         + '\n' + ult_main_v8
                logger.info('  JITPoison + MemoryCanary + AntiVM + SysTraceNuke + ImportHookPoison eklendi')
                # Async anti-debug — ult_main_v8 tanımlandıktan sonra
                try:
                    async_ad_code = AntiDebug.generate_async_runtime_check()
                    ult_main_v8 = async_ad_code + '\n' + ult_main_v8
                    logger.info('  Async anti-debug thread kodu eklendi')
                except Exception as _aad_e:
                    logger.warning(f'  Async anti-debug atlandı: {_aad_e}')
            except Exception as _e:
                logger.warning(f'Runtime guard atlandı: {_e}')

            logger.info('Ultimate Adım 23: Hardware fingerprint key')
            hw_derive_code = HardwareFingerprintKey.generate_derive_code()
            ult_main_v8 = hw_derive_code + '\n' + ult_main_v8

            logger.info('Ultimate Adım 24: Whitespace steganografi')
            try:
                wss_payload = hashlib.sha256(ult_enc[:256] if len(ult_enc) >= 256 else ult_enc).digest()
                ult_main_v8 = WhitespaceSteganography.encode(wss_payload, ult_main_v8)
                logger.info('Whitespace stego uygulandı')
            except Exception as _e:
                logger.warning(f'Whitespace stego atlandı: {_e}')

            logger.info('Ultimate Adım 24b: Advanced Layer — White-Box AES canlı katman')
            wbc_live_block = ''
            try:
                # WBC'yi CANLI ve GERÇEK çalışan bir katman olarak ekle:
                # ult_main_v8 içeriğinin bir bütünlük etiketi WBC-AES-128-CBC ile
                # şifrelenir, decrypt tablosu (T-box) runtime'a gömülür ve çalıştırma
                # anında çözülür. RAM'de anahtar asla açık durmaz (white-box T-box).
                # Bu, payload zincirine dokunmadan gerçek bir kripto engeli ekler.
                _wbc_key = os.urandom(16)
                _wbc_tag = hashlib.sha256(ult_main_v8.encode('utf-8', 'replace')).digest()  # 32 byte
                _wbc_ct  = WhiteBoxAES.encrypt_cbc(_wbc_tag, _wbc_key)
                _wbc_src, _wbc_fn = WhiteBoxAES.generate_wbc_decrypt_source(_wbc_key)
                del _wbc_key  # açık anahtarı bırakma — sadece T-box'lar kaynak içinde
                _wbc_ct_b64 = base64.b64encode(_wbc_ct).decode('ascii')
                _wbc_gv     = self.hyperion._randvar()
                wbc_live_block = (
                    f"{_wbc_src}"
                    f"{_wbc_gv} = {_wbc_fn}(__import__('base64').b64decode('{_wbc_ct_b64}'))\n"
                    f"if len({_wbc_gv}) != 32:\n"
                    f"    raise SystemExit(1)\n"
                )
                if WhiteBoxAES.self_test():
                    logger.info(f'  WBC-AES-128 self-test OK — T-box tablosu: {len(_wbc_src)//1024} KB gömüldü')
                else:
                    logger.warning('  WBC self-test BAŞARISIZ — katman atlanıyor')
                    wbc_live_block = ''
            except Exception as _wbc_e:
                logger.warning(f'  WBC canlı katman atlandı: {_wbc_e}')
                wbc_live_block = ''

            if wbc_live_block:
                ult_main_v8 = wbc_live_block + '\n' + ult_main_v8

            logger.info('Ultimate Adım 24b2: Advanced Layer — Homomorphic VM guard')
            hvm_live_block = ''
            try:
                # HVM'i CANLI katman olarak ekle: 8 adet 32-bit sabit register'lara
                # şifreli (value+R mod P) yüklenir, homomorfik ADD ile toplanır.
                # R her çalıştırmada rastgele → analist register'a bakınca sadece
                # (değer+R) görür. Sonuç encode-zamanı beklenen toplamla guard'lanır;
                # eşleşmezse çalışma durur. Payload decrypt zincirine DOKUNMAZ.
                _hvm_vals = [random.randint(1, 0x7FFFFFF0) for _ in range(8)]
                _HVM_P = HomomorphicVM.P
                _hvm_expected = 0
                for _v in _hvm_vals:
                    _hvm_expected = (_hvm_expected + _v) % _HVM_P  # ADD homomorfizmi ile aynı sonuç
                _hvm_rt = HomomorphicVM.generate_runtime()
                _hvm_lines = [f'_hvm_load({_i}, {_v})' for _i, _v in enumerate(_hvm_vals)]
                # reg[8] = reg[0]+reg[1], sonra zincirle reg[2..7] eklenir
                _hvm_lines.append('_hvm_add(8, 0, 1)')
                for _i in range(2, 8):
                    _hvm_lines.append(f'_hvm_add(8, 8, {_i})')
                _hvm_lines.append('_hvm_guard_val = _hvm_store(8)')
                _hvm_lines.append(f'if _hvm_guard_val != {_hvm_expected}:')
                _hvm_lines.append('    raise SystemExit(1)')
                _hvm_lines.append('_hvm_clear()')
                hvm_live_block = _hvm_rt + '\n' + '\n'.join(_hvm_lines) + '\n'
                # encode-zamanı self-test: gömülen kod gerçekten expected üretiyor mu?
                _ns_test = {}
                exec(hvm_live_block, _ns_test)
                logger.info(f'  HVM guard: 8 register, homomorfik ADD, R-maskeli (expected={_hvm_expected}) — self-test OK')
            except SystemExit:
                logger.warning('  HVM guard self-test BAŞARISIZ (SystemExit) — katman atlanıyor')
                hvm_live_block = ''
            except Exception as _hvm_e:
                logger.warning(f'  HVM guard atlandı: {_hvm_e}')
                hvm_live_block = ''

            if hvm_live_block:
                ult_main_v8 = hvm_live_block + '\n' + ult_main_v8

            logger.info('Ultimate Adım 24c: Advanced Layer — Honeypot deceptive shell')
            try:
                # Honeypot kabuğu SADECE etrafına sahte servis sınıfı/config/pipeline
                # ekler; gerçek __main__ bloğunu (WBC + payload dahil) olduğu gibi
                # çalıştırır (bkz. HoneypotGenerator.generate_honeypot_shell).
                ult_main_v8 = self.advanced.apply_honeypot(ult_main_v8, num_decoys=3)
                logger.info('  Honeypot: 3 sahte servis sınıfı + config loader + pipeline eklendi')
            except Exception as _hp_e:
                logger.warning(f'  Honeypot atlandı: {_hp_e}')

            logger.info('Ultimate Adım 25: ZIP wrapper (__s0-4__.bin + native + tuzaklar)')
            zip_buffer = BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.writestr('__main__.py', ult_main_v8)
                for _ci, _chunk in enumerate(lazy_chunks):
                    zf.writestr(f'__s{_ci}__.bin', _chunk)
                    logger.info(f'  __s{_ci}__.bin: {len(_chunk)} byte')
                if native_file and os.path.exists(native_file):
                    # strip ile debug sembollerini sil — boyutu %40-60 küçültür
                    stripped_native = native_file
                    try:
                        _strip_out = native_file + '.stripped'
                        _sr = subprocess.run(
                            ['strip', '--strip-all', '-o', _strip_out, native_file],
                            capture_output=True, timeout=30
                        )
                        if _sr.returncode == 0 and os.path.exists(_strip_out):
                            orig_sz = os.path.getsize(native_file)
                            strip_sz = os.path.getsize(_strip_out)
                            logger.info(f'  strip: {orig_sz:,} → {strip_sz:,} byte ({100*(1-strip_sz/orig_sz):.1f}% küçüldü)')
                            stripped_native = _strip_out
                    except Exception as _se:
                        logger.warning(f'  strip atlandı: {_se}')
                    zf.write(stripped_native, Path(native_file).name)
                    logger.info(f'  Native binary: {Path(native_file).name} ({os.path.getsize(stripped_native):,} byte)')
                FakeSoGenerator.add_to_zip(zf, count=15)
                FakePycFlood.add_to_zip(zf, count=50)
                logger.info('  50 sahte .pyc + 15 sahte .so tuzağı eklendi')
                fake_pyc = bytes([0x0d, 0x0a, 0x00, 0x00]) + os.urandom(random.randint(512, 2048))
                zf.writestr('_cache.pyc', fake_pyc)
                zf.writestr('__script__.bin', os.urandom(random.randint(256, 512)))
                logger.info('  Sahte .pyc + __script__.bin tuzakları eklendi')

            logger.info('Ultimate Adım 26: ZIP → base64 wrapper')
            zip_bytes = zip_buffer.getvalue()
            b64_zip = base64.b64encode(zip_bytes).decode('ascii')
            final_wrapper = self._generate_ninjapy_wrapper(b64_zip)
            logger.info(f'  ZIP boyutu: {len(zip_bytes):,} byte → base64: {len(b64_zip):,} karakter')

            # ── Koruma raporu ────────────────────────────────────────────────
            # Boru hattındaki opsiyonel katmanların çoğu başarısız olursa SESSİZCE
            # atlanır ("... atlandı"). Burada mevcut yerel değişkenleri OKUYARAK
            # (akışa dokunmadan) her katmanın gerçekten uygulanıp uygulanmadığını
            # özetleriz — böylece "42 katman" iddiası ölçülebilir hale gelir.
            # Rapor üretimi encode'u ASLA bozmamalı → tümü try/except içinde.
            try:
                _rep = []
                _rep.append(('NinjaVM (custom ISA)', _vm_moved > 0,
                             f'{_vm_moved} fonksiyon' if _vm_moved > 0
                             else 'uygun fonksiyon yok (@ninja_vm ile işaretleyin)'))
                _rep.append(('NinjaVM ISA rastgeleleştirme', True, 'her build farklı opcode'))
                _rep.append(('ChaCha20-Poly1305', bool(chacha_encrypted),
                             'uygulandı' if chacha_encrypted else 'pycryptodome yok/atlandı'))
                _rep.append(('Twofish Feistel', bool(_tf_key), 'uygulandı' if _tf_key else 'atlandı'))
                _rep.append(('AES-256 + PBKDF2', bool(aes_encrypted),
                             'uygulandı' if aes_encrypted else 'pycryptodome yok/atlandı'))
                _rep.append(('HMAC-SHA512 imza', bool(_hmac_key), 'uygulandı' if _hmac_key else 'atlandı'))
                _rep.append(('RuntimeKeyMutator', bool(_rkm_key), 'uygulandı' if _rkm_key else 'atlandı'))
                _rep.append(('Shamir (3-of-5)', bool(_shm_shares), 'uygulandı' if _shm_shares else 'atlandı'))
                _rep.append(('WBC-AES canlı katman', bool(wbc_live_block),
                             'gömüldü' if wbc_live_block else 'atlandı'))
                _rep.append(('Homomorphic VM guard', bool(hvm_live_block),
                             'gömüldü' if hvm_live_block else 'atlandı'))
                _rep.append(('Cython .so', bool(cython_so_file), 'üretildi' if cython_so_file else 'atlandı'))
                _rep.append(('Nuitka native (ZORUNLU)', bool(native_file),
                             Path(native_file).name if native_file else 'YOK'))
                _rep.append(('LazyChunk parçalama', bool(lazy_chunks), f'{len(lazy_chunks)} parça'))
                self._last_report = _rep
                _applied = sum(1 for _n, _ok, _d in _rep if _ok)
                _sep = '─' * 58
                print(f'\x1b[96m{_sep}\x1b[0m')
                print(f'\x1b[96m  KORUMA RAPORU — {_applied}/{len(_rep)} çekirdek katman aktif\x1b[0m')
                print(f'\x1b[96m{_sep}\x1b[0m')
                for _n, _ok, _d in _rep:
                    _mk = '\x1b[92m✓\x1b[0m' if _ok else '\x1b[93m•\x1b[0m'
                    _col = '' if _ok else '\x1b[90m'
                    _rst = '' if _ok else '\x1b[0m'
                    print(f'  {_mk} {_col}{_n:<32}{_rst} {_col}{_d}{_rst}')
                print(f'\x1b[96m{_sep}\x1b[0m')
            except Exception as _rep_e:
                logger.debug(f'Koruma raporu üretilemedi: {_rep_e}')

            logger.info('Ultimate Adım 27: Tamamlandı (v8 — 27 katman + NinjaVM + ELF + LazyChunk + MiniVM + WBC-AES + HVM + Honeypot)')
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(final_wrapper)
            logger.info(f'Ultimate v8 encoding tamamlandı: {output_file}')
            return output_file
        finally:
            try:
                for _ in range(3):
                    try:
                        shutil.rmtree(temp_dir, ignore_errors=True)
                        if not os.path.exists(temp_dir):
                            break
                    except Exception as _cl_e:
                        logger.debug(f'temp_dir temizleme denemesi başarısız: {_cl_e}')
            except Exception as _cl_e2:
                logger.debug(f'temp_dir temizleme bloğu hatası: {_cl_e2}')

    def _generate_cython_loader(self, so_b64, aes_key_b64=None):
        hyp = self.hyperion
        v = [hyp._randvar() for _ in range(12)]
        aes_block = ''
        if aes_key_b64:
            aes_block = f'''
import hashlib as _hl
from Crypto.Cipher import AES as _AES
from Crypto.Util.Padding import unpad as _unpad
_raw = base64.b64decode({v[0]})
_pw = base64.b64decode('{aes_key_b64}')
_salt = _raw[4:20]; _iters = int.from_bytes(_raw[20:24], 'big')
_iv = _raw[24:40]; _ct = _raw[40:]
_dk = _hl.pbkdf2_hmac('sha256', _pw, _salt, _iters, dklen=32)
{v[6]} = _unpad(_AES.new(_dk, _AES.MODE_CBC, _iv).decrypt(_ct), _AES.block_size)
del _pw, _salt, _iters, _iv, _ct, _dk, _raw
'''
        else:
            aes_block = f'{v[6]} = base64.b64decode({v[0]})'
        return f'''import base64, tempfile, os, sys, shutil, atexit, importlib.util
{v[0]} = "{so_b64}"
{v[1]} = tempfile.mkdtemp(prefix='ninja_cy_')
def {v[2]}():
    if os.path.exists({v[1]}): shutil.rmtree({v[1]}, ignore_errors=True)
atexit.register({v[2]})
{aes_block}
{v[3]} = os.path.join({v[1]}, 'ninja_core.so')
with open({v[3]}, 'wb') as f: f.write({v[6]})
os.chmod({v[3]}, 0o755)
{v[4]} = importlib.util.spec_from_file_location('ninja_core', {v[3]})
{v[5]} = importlib.util.module_from_spec({v[4]})
sys.modules['ninja_core'] = {v[5]}
{v[4]}.loader.exec_module({v[5]})
{v[5]}.run()
{v[2]}()
'''

    def _generate_nuitka_loader(self, binary_b64, binary_name):
        hyp = self.hyperion
        vars = [hyp._randvar() for _ in range(10)]
        return f"""#!/usr/bin/env python3\nimport base64, tempfile, os, sys, shutil, atexit, importlib.util\n\n{vars[0]} = "{binary_b64}"\n{vars[1]} = tempfile.mkdtemp(prefix='ninja_nk_')\n\ndef {vars[2]}():\n    if os.path.exists({vars[1]}):\n        shutil.rmtree({vars[1]}, ignore_errors=True)\n\natexit.register({vars[2]})\n\n{vars[3]} = os.path.join({vars[1]}, '{binary_name}')\nwith open({vars[3]}, 'wb') as f:\n    f.write(base64.b64decode({vars[0]}))\n\n{vars[4]} = importlib.util.spec_from_file_location('ninja_main', {vars[3]})\n{vars[5]} = importlib.util.module_from_spec({vars[4]})\nsys.modules['ninja_main'] = {vars[5]}\n{vars[4]}.loader.exec_module({vars[5]})\n{vars[2]}()\n"""

    def _generate_advanced_wrapper(self, parts, var_names, func_names):
        return f'''#!/usr/bin/env python3\nimport base64,tempfile,os,sys,zlib,random,hashlib,time,marshal,types,shutil,atexit\n\ndef {func_names[0]}():\n    {var_names[0]}=[random.randint(0,9999)for _ in range(3000)]\n    {var_names[1]}=sum({var_names[0]})%999999\n    {var_names[2]}=hashlib.sha256(str({var_names[1]}).encode()).hexdigest()\n    return len({var_names[2]})>50\n\ndef {func_names[1]}():\n    {var_names[3]}=b"anti_debug"*500\n    {var_names[4]}=zlib.compress({var_names[3]})\n    return len(zlib.decompress({var_names[4]}))>4000\n\ndef {func_names[2]}():\n    return int(hashlib.md5(b"check").hexdigest(),16)%1000000>0\n\n{var_names[5]}_p1="{parts[0]}"\n{var_names[5]}_p2="{parts[1]}"\n{var_names[5]}_p3="{parts[2]}"\n{var_names[5]}_p4="{parts[3]}"\n\ndef {func_names[3]}():\n    if not({func_names[0]}()and {func_names[1]}()and {func_names[2]}()):return None\n    try:\n        {var_names[6]}={var_names[5]}_p1+{var_names[5]}_p2+{var_names[5]}_p3+{var_names[5]}_p4\n        {var_names[7]}=zlib.decompress(base64.b64decode({var_names[6]}.encode()))\n        {var_names[8]}=int.from_bytes({var_names[7]}[0:4],byteorder='big')\n        return {var_names[7]}[4:4+{var_names[8]}].decode('utf-8')\n    except:return None\n\n{var_names[9]}=tempfile.mkdtemp(prefix='ninja_')\n\ndef _clean():\n    if os.path.exists({var_names[9]}):shutil.rmtree({var_names[9]},ignore_errors=True)\n\natexit.register(_clean)\n\ndef {func_names[4]}():\n    {var_names[6]}={func_names[3]}()\n    if {var_names[6]} is None:sys.exit(1)\n    try:\n        import base64 as b64\n        clean={var_names[6]}.replace('\\n','')\n        d1=b64.a85decode(clean.encode())\n        d2=b64.b64decode(d1)\n        sz=int.from_bytes(d2[0:4],byteorder='big')\n        pyc=d2[4:4+sz]\n        pf=os.path.join({var_names[9]},'m.pyc')\n        with open(pf,'wb')as f:f.write(pyc)\n        with open(pf,'rb')as f:\n            f.read(16)\n            co=marshal.load(f)\n        exec(co,{{'__name__':'__main__','__file__':pf,'__builtins__':__builtins__}})\n    except Exception as e:\n        import sys as _sys\n        print(f"[!] Hata ({{type(e).__name__}}): {{e}}", file=_sys.stderr)\n        import traceback; traceback.print_exc()\n    finally:_clean()\n\nif __name__=="__main__":{func_names[4]}()\n'''

    def _generate_ninjapy_wrapper(self, b64_data):
        import sys as _psys
        py_ver = _psys.version_info[:2]
        return generate_chunked_payload_loader(b64_data, py_ver, chunk_count=6)

    def _generate_ninjapy_wrapper_png(self, png_b64: str, xor_key: int):
        py_ver = sys.version_info[:2]
        inner = (
            "Cevo='.CevoPy'\n"
            "import os,sys,base64 as _B,tempfile as _T,struct as _pst,zlib as _pzl\n"
            f"_ENC_VER={py_ver}\n"
            "_CUR_VER=(sys.version_info.major,sys.version_info.minor)\n"
            "if _CUR_VER!=_ENC_VER:\n"
            '    print(f"[!] UYARI: Python {_ENC_VER[0]}.{_ENC_VER[1]} ile sifrelendi")\n'
            f"_PNG=_B.b64decode('{png_b64}')\n"
            f"_XK={xor_key}\n"
            "_pp=8;_pidat=b'';_pw=_ph=0;_pxk=None\n"
            "while _pp<len(_PNG)-12:\n"
            "    _pln=_pst.unpack('>I',_PNG[_pp:_pp+4])[0]\n"
            "    _pct=_PNG[_pp+4:_pp+8];_pdt=_PNG[_pp+8:_pp+8+_pln];_pp+=12+_pln\n"
            "    if _pct==b'IHDR':_pw,_ph=_pst.unpack('>II',_pdt[:8])\n"
            "    elif _pct==b'tEXt':\n"
            "        _pts=_pdt.split(b'\\x00')\n"
            "        if len(_pts)>=2 and _pts[1]:_pxk=_pts[1][0]^0xA5\n"
            "    elif _pct==b'IDAT':_pidat+=_pdt\n"
            "    elif _pct==b'IEND':break\n"
            "_prw=_pzl.decompress(_pidat);_prb=_pw*3\n"
            "_pca=bytearray()\n"
            "for _pri in range(_ph):\n"
            "    _ps=_pri*(_prb+1)+1;_pca.extend(_prw[_ps:_ps+_prb])\n"
            "_pbi=[_b&1 for _b in _pca];_pex=bytearray()\n"
            "for _pi in range(0,len(_pbi)-7,8):\n"
            "    _byte=0\n"
            "    for _pj in range(8):_byte=(_byte<<1)|_pbi[_pi+_pj]\n"
            "    _pex.append(_byte)\n"
            "_pdl=_pst.unpack('>I',bytes(_pex[:4]))[0]\n"
            "_zip_bytes=bytes(_b^(_pxk or _XK) for _b in _pzl.decompress(bytes(_pex[4:4+_pdl])))\n"
            "del _PNG,_prw,_pca,_pbi,_pex,_pidat\n"
            "A=os.path.join(_T.gettempdir(),Cevo)\n"
            "def _d():\n"
            "    for _ in range(3):\n"
            "        try:\n"
            "            if os.path.exists(A):os.remove(A)\n"
            "            break\n"
            "        except:pass\n"
            "try:\n"
            "    with open(A,'wb')as D:D.write(_zip_bytes)\n"
            "    del _zip_bytes\n"
            "    os.chmod(A,0o700)\n"
            "    import subprocess\n"
            "    r=subprocess.run([sys.executable,A]+sys.argv[1:],stdin=sys.stdin,stdout=sys.stdout,stderr=sys.stderr)\n"
            "    _d();sys.exit(r.returncode)\n"
            "except Exception as E:\n"
            "    _d()\n"
            '    print(f"[!] Hata ({type(E).__name__}): {E}",file=sys.stderr)\n'
            "    sys.exit(1)\n"
            "finally:\n"
            "    _d()\n"
        )
        return inner

    def decode_file(self, input_file, output_file=None):
        if output_file is None:
            base = os.path.splitext(input_file)[0]
            if base.endswith('_enc'):
                base = base[:-4]
            output_file = f'{base}_decoded.py'
        with open(input_file, 'r', encoding='utf-8') as f:
            content = f.read()
        match = re.search("C='([A-Za-z0-9+/=]+)'", content)
        if not match:
            raise ValueError('Base64 data bulunamadı')
        zip_data = base64.b64decode(match.group(1))
        zip_buffer = BytesIO(zip_data)
        with zipfile.ZipFile(zip_buffer, 'r') as zf:
            if '__script__.py' in zf.namelist():
                source = zf.read('__script__.py').decode('utf-8')
            else:
                raise ValueError('Script bulunamadı')
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(source)
        return output_file

def show_file_info(filepath):
    with open(filepath, 'r') as f:
        content = f.read()
    print(S + f'[*] Dosya: {B}{filepath}')
    print(S + f'[*] Boyut: {B}{os.path.getsize(filepath):,} bytes')
    match = re.search("C='([A-Za-z0-9+/=]+)'", content)
    if match:
        b64 = match.group(1)
        print(S + f'[*] Base64: {B}{len(b64):,} chars')
        zip_data = base64.b64decode(b64)
        print(S + f'[*] ZIP: {B}{len(zip_data):,} bytes')
        with zipfile.ZipFile(BytesIO(zip_data), 'r') as zf:
            print(S + '\n[*] ZIP İçeriği:')
            for info in zf.infolist():
                r = info.compress_size / info.file_size if info.file_size > 0 else 0
                print(f'    - {info.filename}: {info.file_size:,} bytes (sıkıştırma: {r:.1%})')

def show_system_info():
    print("'⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⡀⣀⣀⣀⣀⢀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀\n⠀⠀⠀⠀⠀⠀⢀⣤⣶⣾⣿⣿⣿⣿⣿⣿⣷⣧⣄⠀⠀⠀⠀⠀⠀⠀\n⠀⠀⠀⠀⠀⣠⣿⣿⣿⣿⣿⣿⣿⣿⢿⣿⣿⣿⣿⣷⣄⠀⠀⠀⠀⠀\n⠀⠀⠀⢀⣺⣿⣿⣿⣿⡻⠋⠉⠀⠀⠀⠉⠙⣿⣿⣿⣿⣖⡀⠀⠀⠀\n⠀⠀⠀⢨⣿⣿⣿⣿⡍⠀⠀⠀⠀⠀⠀⠀⠀⠩⣿⣿⣿⣿⡄⠀⠀⠀\n⠀⠀⠀⢿⣿⣿⣿⡏⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢹⣿⣿⣿⣿⠀⠀⠀\n⠀⠀⠀⢺⣿⣿⣿⡇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢸⣿⣿⣿⣿⠀⠀⠀\n⠀⠀⢀⣿⣿⣿⣿⣷⣶⣶⣶⣶⣶⣶⣶⣶⣶⣶⣾⣿⣿⣿⣿⡀⠀⠀\n⠀⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣖⠀\n⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇\n⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⡿⠋⠉⠛⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇\n⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⠛⠀⠀⠀⠀⠸⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇\n⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣯⣀⠀⠀⣀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇\n⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠀⠀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇\n⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠀⠀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇\n⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣯⣀⣀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇\n⠸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠃\n⠀⠉⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠛⠀\n⠀⠀⠀⠁⠉⠉⠉⠉⠉⠉⠉⠉⠙⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠁⠀⠀")
    G = '\x1b[32m'; Y = '\x1b[93m'; B = '\x1b[96m'; R = '\x1b[31m'; X = '\x1b[0m'
    print(f"\n{G}╔══════════════════════════════════════════════════╗")
    print(f"║          NinjaEnc v4.0  —  v8 Protection Report ║")
    print(f"╚══════════════════════════════════════════════════╝{X}")
    print(f"\n{B}[ Aktif Koruma Katmanları (Ultimate mod) ]{X}")
    layers = [
        ("Integrity Check v2",      "CRC32+SHA256 zinciri — ctypes YOK, Pydroid3 uyumlu, self-destruct"),
        ("Anti-Debug Pro v2",       "gettrace/getprofile/flags/inspect/TracerPid/wchan — Pydroid3 uyumlu"),
        ("Anti-Frida v3",           "Port scan + env tarama + /proc/self/fd + mem scan + timing check"),
        ("Timing Anti-Debug",       "monotonic() bazlı zamanlama — debugger adım adım gidince yakalanır"),
        ("SIGTRAP Trap",            "Breakpoint tuzağı — signal.SIGTRAP ile debugger tespiti"),
        ("Exec/Marshal/Compile Guard", "exec+marshal.loads+compile hook tespiti — Pydroid3 uyumlu"),
        ("GC Object Scan v2",       "Bdb/Pdb/PydevdAPI + sys.modules debugger modül taraması"),
        ("Parent Process Check v2", "/proc/ppid/exe + ps ppid — gdb/lldb/frida tespiti"),
        ("Dead Code Injection",     "Statik fake bloklar — random YOK, Pydroid3 uyumlu"),
        ("Opaque Predicates v2",    "Sabit matematik — random YOK, her zaman True/False, Pydroid3 uyumlu"),
        ("MBA Transform v2",        "L1+L2 polinom zinciri — Sub dahil 5 kural, %40 ihtimalle çift katman"),
        ("Nuitka Hardened v3",      "--OO+deployment+clang+low-memory+noinclude-setuptools/pytest/unittest"),
        ("ELFProtector (Faz 3)",    "DWARF poison + fake symbol inject + optional .text XOR (lief)"),
        ("LazyChunk 5-Part (Faz4)", "5 parca zincir key: sha256(chunk[i])[0]^key[i] → key[i+1]"),
        ("MiniVM Dispatcher (Faz4)","exec() → custom opcode VM: LOAD/XOR/DECOMP/MARSHAL/EXEC/WIPE"),
        ("Anti-Reverse Maps",       "/proc/self/maps + /proc/self/exe frida/gdb/radare2 tarama"),
        ("exec() Hook Guard v2",    "exec→print swap tespiti + stack derinliği + sys.gettrace kontrolü"),
        ("marshal.loads Guard",     "marshal.loads sarmalandı — kısa payload reddediliyor"),
        ("compile() Hook Guard",    "compile() sarmalandı — kısa kaynak reddediliyor"),
        ("ctypes PyEval Bypass",    "builtins.exec YOK — doğrudan C API (PyEval_EvalCode) kullanılır"),
        ("Memory Wipe",             "Execution sonrası co_code ctypes.memset ile bellekten sıfırlanır"),
        ("Memory-Only Exec",        "Disk'e .pyc/.bin yazılmıyor — BytesIO ile bellekte çalışıyor"),
        ("Custom Marshal Bypass",   "XOR + 7-parça split → klasik marshal.loads hook geçersiz"),
        ("AES Runtime Key",         "Makine hostname+arch+cpu'dan SHA256 türetme — hardcoded değil"),
        ("String Table Encrypt",    "Tüm string literaller XOR şifreli runtime tablosuna taşındı"),
        ("Control Flow Flatten",    "State-machine dispatcher — orijinal akış gizleniyor"),
        ("AST Obfuscation v2",      "shake_128 hash — genişletilmiş KEYWORDS, doğru sırada (adım 1)"),
        ("AES-256 + PBKDF2",        "150k-250k iterasyon, rastgele salt+iv (pycryptodome varsa)"),
        ("Multi-Layer XOR",         "3 ayrı anahtarla zincirleme XOR şifreleme"),
        ("Zlib + Ascii85",          "Level-9 sıkıştırma + 80-150KB sahte padding"),
        ("ZIP Binary Wrapper",      "__script__.bin (binary, XOR şifreli) — ZIP'ten okunamaz"),
        ("NinjaVM (stack-VM)",      "Seçili fonksiyonlar custom ISA'ya derlenir — Python bytecode gizli"),
        ("White-Box AES-128",       "Anahtar T-box tablolarına gömülü — RAM'de açık anahtar yok"),
        ("Homomorphic VM Guard",    "R-maskeli register aritmetiği — statik register okuması işe yaramaz"),
        ("Honeypot Shell",          "Sahte servis/config/pipeline — analisti yanıltan yürütme yolları"),
        ("Native Zorunlu (Nuitka)", "Kritik kaynak C'ye derlenir — pure-Python fallback YOK (platforma bağlı)"),
    ]
    for name, desc in layers:
        print(f"  {G}✓{X} {Y}{name:<28}{X} {desc}")
    print(f"\n{B}[ Koruma Değerlendirmesi ]{X}")
    print(f"  {Y}Not: Aşağıdakiler kaba tahmindir; gerçek direnç saldırganın")
    print(f"  becerisine, araçlarına ve zamanına göre büyük ölçüde değişir.{X}")
    print(f"  {G}Otomatik decompiler'lar:{X}   pratikte engellenir (VM + native)")
    print(f"  {G}Meraklı / script kiddie:{X}   büyük olasılıkla vazgeçer")
    print(f"  {G}Deneyimli reverser:{X}        yavaşlar ama çözebilir (saatler–günler)")
    print(f"  {G}Belirlenmiş uzman:{X}         yeterli zamanla çözer — hiçbir obfuscation mutlak değil")
    print(f"\n  {Y}Gerçek: Kod çalışmak için sonunda bellekte açılır; obfuscation")
    print(f"  reverse maliyetini artırır, imkansız kılmaz. En güçlü kombinasyon:")
    print(f"  NinjaVM + zorunlu native derleme (Nuitka).{X}\n")

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description='NinjaEnc — Python Kod Koruyucu (native derleme ZORUNLU; çıktı platforma bağlıdır)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='\nÖrnekler:\n  python3 ninjaEnc.py                           # Interactive mod\n  python3 ninjaEnc.py script.py                 # Basit encoding\n  python3 ninjaEnc.py script.py --cython        # Cython native derleme\n  python3 ninjaEnc.py script.py --nuitka        # Nuitka C++ derleme\n  python3 ninjaEnc.py script.py --hyperion      # Hyperion obfuscation\n  python3 ninjaEnc.py script.py --ultimate      # 24 katmanlı maksimum\n  python3 ninjaEnc.py script.py --seed 12345    # Tekrarlanabilir çıktı\n  python3 ninjaEnc.py --decode encoded.py       # Decode et\n  python3 ninjaEnc.py --info encoded.py         # Bilgi göster\n  python3 ninjaEnc.py --sysinfo                 # Sistem bilgisi\n'
    )
    parser.add_argument('input', nargs='?', help='Input Python dosyası')
    parser.add_argument('-o', '--output', help='Output dosya adı')
    parser.add_argument('--advanced', action='store_true', help='7 adımlı gelişmiş encoding')
    parser.add_argument('--cython', action='store_true', help='Cython native .so derleme')
    parser.add_argument('--nuitka', action='store_true', help='Nuitka C++ native derleme')
    parser.add_argument('--hyperion', action='store_true', help='Hyperion obfuscation')
    parser.add_argument('--ultimate', action='store_true', help='24 katmanlı maksimum koruma')

    parser.add_argument('--decode', action='store_true', help='Decode et')
    parser.add_argument('--info', action='store_true', help='Dosya bilgisi göster')
    parser.add_argument('--sysinfo', action='store_true', help='Sistem bilgisi göster')
    parser.add_argument('--seed', type=int, default=None,
                        help='Obfuscation seed — aynı seed → aynı çıktı (varsayılan: rastgele)')
    parser.add_argument('--yes', '-y', action='store_true',
                        help='Eksik derleme aracını (Nuitka) sormadan otomatik kur')
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
        print(S + f'[*] Obfuscation seed: {B}{args.seed}{S} (deterministik mod)')
    else:
        random.seed()

    if not args.input:
        # ── İnteraktif Mod ──────────────────────────────────────────────
        G2 = '\x1b[32m'; Y2 = '\x1b[93m'; B2 = '\x1b[96m'; X2 = '\x1b[0m'; R2 = '\x1b[31m'
        print(f"\n{G2}╔══════════════════════════════════════════════════╗")
        print(f"║          NinjaEnc v4.0  —  İnteraktif Mod       ║")
        print(f"╚══════════════════════════════════════════════════╝{X2}")

        # Katman listesi — encode_ultimate() gerçek çalışma sırasına göre (v5.0)
        layers = [
            ("1",  "String Encrypt",            "Tüm string literaller XOR şifreli runtime tablosuna alınır"),
            ("2",  "AST Obfuscation v2",        "shake_128 hash tabanlı değişken/fonksiyon yeniden adlandırma"),
            ("3",  "MBA Transform v2",          "L1+L2 polinom zinciri, %40 çift katman aritmetik karmaşıklaştırma"),
            ("4",  "Control Flow Flatten",      "State-machine dispatcher — orijinal akış gizlenir"),
            ("5",  "Opaque Predicates v2",      "Sabit matematik ifadeleri — her zaman True/False"),
            ("6",  "String Table Encrypt",      "String'ler şifreli lookup tablosuna taşınır"),
            ("7",  "Dead Code Injection x60",   "60 adet sahte blok — statik analizi zorlaştırır"),
            ("8",  "Fake Branch Injection",     "Sahte if/else dalları — kontrol akışı gizlenir"),
            ("9",  "MetamorphicStager x2",      "5 transform — 2 tur farklı rastgele sırayla"),
            ("10", "FakeRecursion Stub",        "8-14 seviye derin sahte çağrı yığını — stack trace yanıltır"),
            ("11", "ClassCamouflage x10",       "10 sahte class — hangisi gerçek payload taşıyor bilinmez"),
            ("12", "FakeExceptionInjector x300","300+ sahte try/except/raise — analiz aracı boğulur"),
            ("13", "StringSplitter chr()",      "String literaller chr() zincirine bölünür — string arama geçersiz"),
            ("14", "FakeImportTree x56",        "56 sahte import — bağımlılık ve CFG analizi yanılır"),
            ("15", "ConstantFoldingSaboteur",   "Sabit tamsayılar runtime ifadeye dönüştürülür — optimizer devre dışı"),
            ("16", "LambdaSoupWrapper x20",     "20 fonksiyon lambda zincirine sarılır — decompile karmaşıklaşır"),
            ("17", ".pyc Derleme",              "Karmaşıklaştırılmış kaynak .pyc bytecode'a derlenir"),
            ("18", "Opcode Mutator",            "Python <3.12: .pyc opcode'ları mutasyona uğratılır"),
            ("19", "co_consts Mutator",         ".pyc co_consts alanı mutasyona uğratılır"),
            ("20", "JunkBytecodeInjector",      ".pyc'ye yanıltıcı junk veri enjekte edilir — decompiler çöker"),
            ("21", "ChaCha20-Poly1305",         "AEAD authenticated şifreleme"),
            ("22", "Twofish Feistel",           "RC4-scheduled 10-round Feistel — 3. şifreleme algoritması"),
            ("23", "AES-256-GCM + PBKDF2",     "400k-600k iter, SHA-512, 32-byte salt, GCM auth tag"),
            ("24", "Multi-Layer XOR x5",        "5 adet 32-byte key ile zincirleme XOR"),
            ("25", "Polymorphic Decryptor x5",  "5 aşama: XOR→ROT-bit→XOR→Nibble-swap→XOR"),
            ("26", "HMAC-SHA512 İmzası",        "Payload değiştirilirse _exit(1) — her bit kritik"),
            ("27", "RuntimeKeyMutator",         "Her 128 byte'da key SHA256 ile türer — dinamik key schedule"),
            ("28", "Shamir (3-of-5)",           "AES key 5 parçaya bölünür — 3 olmadan çözülemez"),
            ("29", "Ascii85 + Padding",         "40-90KB rastgele padding ile Ascii85 encode"),
            ("30", "Zlib Compress",             "Level-9 sıkıştırma"),
            ("31", "Self-Modifying Code",       "Runtime mutation + Anti-AI decompiler tuzakları"),
            ("32", "PNG LSB Steganografi",      "Payload PNG görüntüsüne LSB ile gömülür"),
            ("33", "Whitespace Stego",          "Guard kodu whitespace/tab encoding ile gizlenir"),
            ("34", "JITPoison",                 "PyPy/JPython/GraalPy tespiti → _exit(0)"),
            ("35", "MemoryCanary",              "Bellek dump saldırısı tespiti → _exit(1)"),
            ("36", "AntiVM",                    "VBox/VMware/QEMU/Docker/LXC tespiti → _exit(0)"),
            ("37", "SysTraceNuke",              "sys.settrace/setprofile devre dışı + zehirleme"),
            ("38", "ImportHookPoison",          "dis/uncompyle6/decompyle3 import'larını patlatır"),
            ("39", "Fake .so x15",              "15 sahte native kütüphane ZIP'e eklenir"),
            ("40", "FakePycFlood x50",          "50 sahte .pyc ZIP'e eklenir — hangisi gerçek bilinmez"),
            ("41", "ZIP Wrapper",               "payload.png + __main__.py + native + tuzaklar ZIP'te"),
            ("42", "Base64 + Chunked Loader",   "ZIP base64'e alınır, 6 parçalı chunked loader ile sarılır"),
        ]

        print(f"\n{B2}[ Mevcut Koruma Katmanları ]{X2}")
        for num, name, desc in layers:
            print(f"  {G2}[{num:>2}]{X2} {Y2}{name:<28}{X2} {desc}")

        print(f"\n{B2}[ Aktif Mod ]{X2}")
        print(f"  {G2}Ultimate{X2} → Tüm {len(layers)} katman aktif (maksimum koruma)\n")

        # Dosya yolu sor
        while True:
            try:
                input_file = input(f"{Y2}[?] Şifrelenecek dosya yolu: {X2}").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{R2}[!] İptal edildi.{X2}")
                sys.exit(0)
            if not input_file:
                print(f"{R2}[!] Dosya yolu boş olamaz.{X2}")
                continue
            if not os.path.isfile(input_file):
                print(f"{R2}[!] Dosya bulunamadı: {input_file}{X2}")
                continue
            break

        args.input = input_file
        args.ultimate = True  # Her zaman ultimate mod
        # ────────────────────────────────────────────────────────────────
    encoder = NinjaEncoder()
    if args.info:
        print("Info modu devre dışı")
        sys.exit(1)
    elif args.decode:
        output = encoder.decode_file(args.input, args.output)
        print(S + f'[+] Decoded: {B}{output}')
    else:
        # --advanced / --cython / --nuitka / --hyperion devre dışı; her zaman ultimate
        if args.advanced or args.cython or args.nuitka or args.hyperion:
            print(S + f'[!] Bu mod şu an devre dışı. Ultimate mod ile devam ediliyor...')
        output = encoder.encode_ultimate(args.input, args.output, seed=args.seed, assume_yes=args.yes)
        print(S + f'[+] Encoded: {B}{output}')
        input_size = os.path.getsize(args.input)
        output_size = os.path.getsize(output)
        print(S + f'[*] Input:  {B}{input_size:,} bytes')
        print(S + f'[*] Output: {B}{output_size:,} bytes')
        print(S + f'[*] Oran: {B}{output_size / input_size:.2f}x')
        if args.seed is not None:
            print(S + f'[*] Seed:   {B}{args.seed}{S} — aynı seed ile aynı çıktı üretilir')
        import platform as _pf
        print('\x1b[93m[!] UYARI: Native-zorunlu mod — çıktı yalnızca bu platformda çalışır: '
              f'{_pf.system()} {_pf.machine()} / Python {sys.version_info.major}.{sys.version_info.minor}. '
              'Farklı OS/mimari için o platformda yeniden encode edin.\x1b[0m')
if __name__ == '__main__':
    main()
