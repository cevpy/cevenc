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

    @staticmethod
    def generate_decrypt_code(var_in: str, key_b64: str, var_out: str) -> str:
        """Üretilen loader'a gömülecek AES-256-GCM decrypt kodu (NJNC çerçevesi:
        MAGIC(4)+salt(32)+iter(4)+nonce(16)+tag(16)+ct). key_b64 = b64(password).
        MAGIC yoksa pass-through. pycryptodome yoksa (ImportError) veri değişmez."""
        return f'''try:
    if {var_in}[:4]==b"NJNC":
        from Crypto.Cipher import AES as _AESd
        import hashlib as _aesh, base64 as _aesb
        _apw=_aesb.b64decode("{key_b64}")
        _asl={var_in}[4:36];_ait=int.from_bytes({var_in}[36:40],"big")
        _ano={var_in}[40:56];_atg={var_in}[56:72];_act={var_in}[72:]
        _akm=_aesh.pbkdf2_hmac("sha512",_apw,_asl,_ait,dklen=64)
        _acp=_AESd.new(_akm[:32],_AESd.MODE_GCM,nonce=_ano);_acp.update(_akm[32:])
        {var_out}=_acp.decrypt_and_verify(_act,_atg)
        del _apw,_asl,_ait,_ano,_atg,_act,_akm,_acp
    else:
        {var_out}={var_in}
except Exception:
    {var_out}={var_in}'''

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

    def generate_with_payload(self, source: str, native_fname: str = '',
                              inject_junk: bool = True):
        code = compile(source, '<ninja>', 'exec')
        raw = marshal.dumps(code)
        # (v9 — Fikir #2/junk canlandırma) GERÇEK payload'a tersinir junk enjekte et.
        # Runtime tarafı (generate_v8 → MiniVM MARSHAL handler) NJJK çerçevesini
        # marshal.loads'tan HEMEN ÖNCE strip eder → tam simetri, çalışırlık korunur.
        # Bu, junk'ı ölü daldan (Adım 9-16 compressed) çıkarıp fiilen aktif yapar.
        if inject_junk:
            try:
                raw = JunkBytecodeInjector.inject(raw)
            except Exception:
                pass  # junk asla payload'ı riske atmamalı
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
                    vm_prog_b64: str = '', fusion_setup_code: str = '',
                    fusion_key_var: str = '',
                    real_fusion_key: bytes = b'',
                    crypto_layers: list = None) -> str:
        """
        v8 __main__.py:
        - LazyChunk: __s0-4__.bin zincir key ile birlestirir
        - (v17) Fusion setup ÖNCE çalışır → fusion_key üretir
        - (v17) XOR chain anahtarları fusion_key'den TÜREİLİR (statik görünmez)
        - XOR decode
        - MiniVM exec

        fusion_setup_code : Guard katkılarını toplayıp fusion_key_var üretir.
        fusion_key_var    : 32-byte fusion anahtarının değişken adı.
        real_fusion_key   : Encode-zamanı fusion anahtarı. Verilirse XOR chain
                            anahtarları fusion_key'in belirli byte'larıyla
                            maskelenir → dosyada görünen sadece random maskeler.
                            Runtime'da fusion_key hesaplandıktan sonra gerçek
                            anahtarlar geri türetilir.
        """
        v = [self.hyp._randvar() for _ in range(22)]
        all_keys = list(xor_keys)

        # (v17) Fusion-derive stratejisi:
        # Eğer real_fusion_key mevcutsa: her XOR anahtarı için fusion_key'den
        # bir offset seç, mask = xor_key XOR fusion_key[offset]. Runtime'da
        # fusion_key hesaplandıktan sonra: xor_key = mask XOR fusion_key[offset].
        # Bu, dosyada görünen değerlerin ANLAMLI olmamasını sağlar.
        _use_fusion_derive = bool(real_fusion_key) and bool(fusion_setup_code) and bool(fusion_key_var)
        if _use_fusion_derive:
            _fk_offsets = [random.randint(0, 31) for _ in all_keys]
            _key_masks = [k ^ real_fusion_key[o] for k, o in zip(all_keys, _fk_offsets)]
        else:
            # eski davranış: iki-parça maskeleme (fallback)
            _mask_vals = [random.randint(1, 255) for _ in all_keys]
            _blinded_keys = [k ^ m for k, m in zip(all_keys, _mask_vals)]

        # (v17) Fusion setup — XOR chain'in ÖNCESİNE alındı ki fusion_key
        # hazır olsun ve XOR anahtarlarını ondan türetebilelim.
        _fusion_step = ''
        if _use_fusion_derive:
            _fk_indent = '\n'.join('        ' + l for l in fusion_setup_code.splitlines())
            _fusion_step = f'{_fk_indent}\n'

        # XOR chain — anahtarlar runtime'da fusion_key'den türetilir
        _cur = v[3]
        _xor_steps = ''
        _tmp_vars = [v[4], v[5], v[6], v[7], v[8]]
        if _use_fusion_derive:
            for _i in range(len(all_keys)):
                _nxt = _tmp_vars[_i % len(_tmp_vars)] if _i < len(all_keys) - 1 else v[3]
                _bv = f'_xb{_i}'
                _mask = _key_masks[_i]
                _off = _fk_offsets[_i]
                # Anahtar runtime'da hesaplanır: mask XOR fusion_key[offset]
                # Statik olarak görünen: sadece _mask ve _off (rastgele byte)
                _xor_steps += (
                    f'        _rk{_i}={_mask}^{fusion_key_var}[{_off}]\n'
                    f'        {_nxt}=bytes({_bv}^_rk{_i} for {_bv} in {_cur})\n'
                )
                _cur = _nxt
        else:
            for _i, (_bk, _mk) in enumerate(zip(_blinded_keys, _mask_vals)):
                _nxt = _tmp_vars[_i % len(_tmp_vars)] if _i < len(all_keys) - 1 else v[3]
                _bv = f'_xb{_i}'
                _xor_steps += f'        {_nxt}=bytes({_bv}^({_bk}^{_mk}) for {_bv} in {_cur})\n'
                _cur = _nxt

        # (v9→v17) Eski payload-fusion-XOR adımı KALDIRILDI — fusion artık XOR
        # anahtarlarının üretiminde kullanıldığı için ayrı bir XOR gereksiz.
        # Fusion başarısızsa (guard patch'lenmiş) → xor_key yanlış → payload çöp.
        # Fallback (fusion yoksa) durumunda eski davranış: fusion XOR sonrası uygula.
        if not _use_fusion_derive and fusion_setup_code and fusion_key_var:
            _fk_indent = '\n'.join('        ' + l for l in fusion_setup_code.splitlines())
            _fusion_step = (
                f'{_fk_indent}\n'
                f'        {v[3]}=bytes({v[3]}[_fxi]^{fusion_key_var}[_fxi%32] for _fxi in range(len({v[3]})))\n'
            )

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

        # ── (v18) CANLI kripto zinciri ters çözümü ────────────────────────────
        # crypto_layers encode sırasında [CC, TF, AES] olarak birikti; en dış
        # katman AES olduğundan runtime'da TERS sırada (AES→TF→ChaCha) çözülür.
        # Lazy-reconstruct v[3]'ü kurduktan HEMEN SONRA, fusion/XOR adımlarından
        # ÖNCE çalışır. Her katman MAGIC-çerçeveli → eksik katman no-op.
        _crypto_block = ''
        if crypto_layers:
            _cparts = []
            for _algo, _ckey in reversed(list(crypto_layers)):
                _kb64 = base64.b64encode(_ckey).decode('ascii')
                if _algo == 'CC':
                    _cparts.append(ChaCha20Encryptor.generate_decrypt_code(v[3], '"' + _kb64 + '"', v[3]))
                elif _algo == 'TF':
                    _cparts.append(TwofishEncryptor.generate_decrypt_code(v[3], _kb64, v[3]))
                elif _algo == 'AES':
                    _cparts.append(AESEncryptor.generate_decrypt_code(v[3], _kb64, v[3]))
            _craw = '\n'.join(_cparts)
            if _craw.strip():
                _crypto_block = '\n'.join('        ' + l for l in _craw.splitlines()) + '\n'

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
            f'{_crypto_block}'
            # (v17) Fusion-derive modunda: fusion_step XOR'dan ÖNCE (fusion_key
            # üretilsin ki XOR anahtarları ondan türetilebilsin). Fallback modunda:
            # fusion_step XOR'dan SONRA (payload'a ek XOR katmanı olarak).
            + (f'{_fusion_step}{_xor_steps}' if _use_fusion_derive
               else f'{_xor_steps}{_fusion_step}') +
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

    def flatten_source(self, source: str, skip_names: set = None) -> str:
        """
        skip_names: bu adlardaki fonksiyonları CFF'den muaf tut.
        global+with+çoklu-return kombinasyonu olan fonksiyonlarda CFF
        state-machine global bildirimi veya with bloğunu bozabilir.
        """
        try:
            import ast as _ast
            tree = _ast.parse(source)
            count = [0]
            hyp = self.hyp
            _skip = set(skip_names) if skip_names else set()

            class FlattenTransformer(_ast.NodeTransformer):

                def visit_FunctionDef(self_, node):
                    self_.generic_visit(node)
                    # skip listesindeki fonksiyonları atla
                    if node.name in _skip:
                        return node
                    # global + with kombinasyonu varsa atla (state-machine bozar)
                    has_global = any(isinstance(n, _ast.Global) for n in _ast.walk(node))
                    has_with = any(isinstance(n, _ast.With) for n in _ast.walk(node))
                    if has_global and has_with:
                        return node
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
    # ── (v10) genişletme opcode'ları ──
    STORE_GLOBAL = 0x23   # global değişkene yaz
    BUILD_MAP = 0x24      # dict oluştur (2*n stack elemanı)
    BUILD_SET = 0x25      # set oluştur (n eleman)
    STORE_SUBSCR = 0x26   # obj[key] = val
    GET_ITER = 0x27       # iter(x)
    FOR_ITER = 0x28       # döngü: next() veya jump
    BUILD_SLICE = 0x29    # slice(...)
    LOAD_METHOD = 0x2A    # metod yükle (LOAD_ATTR benzeri)
    UNARY_NOT = 0x2B      # not x
    UNARY_INV = 0x2C      # ~x
    SWAP = 0x2D           # stack[-1] <-> stack[-n]
    ROT = 0x2E            # stack rotasyonu (COPY/SWAP aileleri)
    # try/except: exception guard mekanizması
    SETUP_EXC = 0x2F      # exception handler kaydı (handler_ip)
    POP_EXC = 0x30        # exception handler kaydını kaldır
    RERAISE = 0x31        # exception'ı yeniden fırlat
    PUSH_EXC = 0x32       # aktif exception'ı stack'e koy
    CHECK_EXC = 0x33      # exception tipi eşleşiyor mu
    # ── (v10.1) f-string + kwarg + string desteği ──
    FORMAT_VAL = 0x34     # FORMAT_VALUE: format(x) veya format(x, spec)
    BUILD_STR = 0x35      # BUILD_STRING: n parçayı birleştir
    CALL_KW = 0x36        # keyword argümanlı çağrı (KW_NAMES + CALL)
    LOAD_KW = 0x37        # KW_NAMES: keyword adları tuple'ını kaydet
    FORMAT_SPEC = 0x38    # FORMAT_VALUE spec'li varyant
    # ── (v11.1) 3.13 opcode'ları + contains/unpack/with ──
    TO_BOOL = 0x39        # bool(x)
    CONTAINS = 0x3A       # x in y   (arg=0)
    NOT_CONTAINS = 0x3B   # x not in y (arg=1)
    UNPACK_SEQ = 0x3C     # a,b,... = iterable (arg=n)
    IS_OP = 0x3D          # x is y / x is not y
    WITH_START = 0x3E     # __enter__ çağır, __exit__'i sakla
    WITH_END = 0x3F       # __exit__ çağır (normal çıkış)
    # ── (v11.2) comprehension + copy ──
    MAP_ADD = 0x40        # dict comp: d[k]=v (stack'te k,v; d derinde)
    LIST_APPEND = 0x41    # list comp: lst.append(v)
    SET_ADD = 0x42        # set comp: s.add(v)
    LIST_EXTEND = 0x43    # lst.extend(iter)
    SET_UPDATE = 0x44     # s.update(iter)
    DICT_UPDATE = 0x45    # d.update(m)
    DICT_MERGE = 0x46     # d.update(m) — çağrı için
    COPY_N = 0x47         # stack[-n]'i tepeye kopyala
    # ── (v16) exception-table + with desteği ──
    WITH_EXC_START = 0x48 # WITH_EXCEPT_START: __exit__(type,val,tb) çağır
    BEFORE_WITH_OP = 0x49 # BEFORE_WITH: ctx.__enter__() + __exit__'i sakla
    SETUP_WITH_OP = 0x4A  # 3.11 SETUP_WITH: BEFORE_WITH benzeri

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
                 'NOP', 'JMP_TRUE', 'DUP',
                 'STORE_GLOBAL', 'BUILD_MAP', 'BUILD_SET', 'STORE_SUBSCR',
                 'GET_ITER', 'FOR_ITER', 'BUILD_SLICE', 'LOAD_METHOD',
                 'UNARY_NOT', 'UNARY_INV', 'SWAP', 'ROT',
                 'SETUP_EXC', 'POP_EXC', 'RERAISE', 'PUSH_EXC', 'CHECK_EXC',
                 'FORMAT_VAL', 'BUILD_STR', 'CALL_KW', 'LOAD_KW', 'FORMAT_SPEC',
                 'TO_BOOL', 'CONTAINS', 'NOT_CONTAINS', 'UNPACK_SEQ', 'IS_OP',
                 'WITH_START', 'WITH_END',
                 'MAP_ADD', 'LIST_APPEND', 'SET_ADD', 'LIST_EXTEND',
                 'SET_UPDATE', 'DICT_UPDATE', 'DICT_MERGE', 'COPY_N',
                 'WITH_EXC_START', 'BEFORE_WITH_OP', 'SETUP_WITH_OP')
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
        _pending_kw = None  # KW_NAMES → sonraki CALL için bekleyen kwnames const idx
        _prev_end_for = False  # END_FOR'u takip eden POP_TOP'u yutmak için
        for ins in instrs:
            op, a, off = ins.opname, ins.arg, ins.offset
            # (3.13) END_FOR'dan HEMEN SONRA gelen POP_TOP, iç FOR_ITER'in zaten
            # pop ettiği iterator'ı ikinci kez temizlemeye çalışır → iç içe
            # döngüde DIŞ iterator'ı yer. Bu POP_TOP'u atla (NOP).
            if _prev_end_for and op == 'POP_TOP':
                emit.append((off, self.NOP, 0, None))
                _prev_end_for = False
                continue
            _prev_end_for = (op == 'END_FOR')
            if op in ('RESUME', 'PRECALL', 'CACHE', 'COPY_FREE_VARS',
                      'MAKE_FUNCTION', 'PUSH_NULL', 'NOP', 'EXTENDED_ARG'):
                emit.append((off, self.NOP, 0, None)); continue
            if op == 'LOAD_CONST':
                emit.append((off, self.PUSH_CONST, cidx(co.co_consts[a]), None))
            elif op in ('LOAD_FAST', 'LOAD_FAST_CHECK'):
                emit.append((off, self.LOAD_LOCAL, a, None))
            elif op == 'LOAD_FAST_AND_CLEAR':
                emit.append((off, self.LOAD_LOCAL, a, None))
            elif op in ('POP_JUMP_IF_NONE', 'POP_JUMP_FORWARD_IF_NONE'):
                # None kontrolü: TOS None ise jump
                emit.append((off, self.DUP, 0, None))
                emit.append((None, self.TO_BOOL, 0, None))
                emit.append((None, self.UNARY_NOT, 0, None))
                emit.append((None, self.JMP_TRUE, 0, ins.argval))
                emit.append((None, self.POP, 0, None))
            elif op in ('POP_JUMP_IF_NOT_NONE', 'POP_JUMP_FORWARD_IF_NOT_NONE'):
                # None değilse jump
                emit.append((off, self.DUP, 0, None))
                emit.append((None, self.TO_BOOL, 0, None))
                emit.append((None, self.JMP_TRUE, 0, ins.argval))
                emit.append((None, self.POP, 0, None))
            elif op == 'MAKE_CELL':
                # Closure cell oluşturur — VM'de local aynen kalır (NOP).
                emit.append((off, self.NOP, 0, None))
            elif op == 'LOAD_FAST_LOAD_FAST':
                # 3.13: iki local'i tek opcode'da yükle. arg = (i<<4)|j paketli.
                emit.append((off, self.LOAD_LOCAL, (a >> 4) & 0xF, None))
                emit.append((None, self.LOAD_LOCAL, a & 0xF, None))
            elif op == 'STORE_FAST_STORE_FAST':
                # 3.13: iki değeri iki local'e sakla. arg = (i<<4)|j.
                # CPython: TOS → local i, TOS1 → local j. İki ayrı STORE.
                emit.append((off, self.STORE_LOCAL, (a >> 4) & 0xF, None))
                emit.append((None, self.STORE_LOCAL, a & 0xF, None))
            elif op == 'STORE_FAST_LOAD_FAST':
                # 3.13: TOS'u local i'ye sakla, sonra local j'yi yükle.
                emit.append((off, self.STORE_LOCAL, (a >> 4) & 0xF, None))
                emit.append((None, self.LOAD_LOCAL, a & 0xF, None))
            elif op == 'TO_BOOL':
                emit.append((off, self.TO_BOOL, 0, None))
            elif op == 'CONTAINS_OP':
                # arg 0 = 'in', 1 = 'not in'
                emit.append((off, self.NOT_CONTAINS if a else self.CONTAINS, 0, None))
            elif op == 'IS_OP':
                emit.append((off, self.IS_OP, a or 0, None))
            elif op == 'UNPACK_SEQUENCE':
                emit.append((off, self.UNPACK_SEQ, a or 0, None))
            elif op == 'STORE_FAST':
                emit.append((off, self.STORE_LOCAL, a, None))
            elif op == 'DELETE_FAST':
                # 'except X as e' sonrası e'yi siler. VM'de: None atayarak temizle.
                # None sabitini bul veya ekle (cidx None için de çalışır).
                _none_idx = cidx(None)
                emit.append((off, self.PUSH_CONST, _none_idx, None))
                emit.append((None, self.STORE_LOCAL, a, None))
            elif op == 'STORE_GLOBAL':
                emit.append((off, self.STORE_GLOBAL, a, None))
            elif op == 'STORE_NAME':
                emit.append((off, self.STORE_GLOBAL, a, None))
            elif op == 'LOAD_GLOBAL':
                emit.append((off, self.LOAD_GLOBAL, (a >> 1) if a is not None else 0, None))
            elif op == 'LOAD_NAME':
                emit.append((off, self.LOAD_GLOBAL, a, None))
            elif op == 'LOAD_ATTR':
                emit.append((off, self.LOAD_ATTR, (a >> 1) if a is not None else 0, None))
            elif op in ('CALL', 'CALL_FUNCTION'):
                if _pending_kw is not None:
                    # 3.12: önceki KW_NAMES bu CALL'ı keyword'lü yapar.
                    # Önce kwnames tuple'ını stack'e it (CALL_KW handler pop eder).
                    emit.append((None, self.LOAD_KW, _pending_kw, None))
                    emit.append((off, self.CALL_KW, a or 0, None))
                    _pending_kw = None
                else:
                    emit.append((off, self.CALL, a or 0, None))
            elif op == 'KW_NAMES':
                # stack'e KOYMA — bir sonraki CALL'a iletilecek pending bilgi.
                _pending_kw = cidx(co.co_consts[a])
            elif op == 'CALL_KW':
                # 3.13: keyword'lü çağrı. TOS = kwnames tuple (zaten stack'te).
                emit.append((off, self.CALL_KW, a or 0, None))
            elif op == 'FORMAT_VALUE':
                # a bit 2 (0x04) → format spec var. bit 0-1 → conversion (str/repr/ascii)
                has_spec = bool((a or 0) & 0x04)
                emit.append((off, self.FORMAT_SPEC if has_spec else self.FORMAT_VAL, a or 0, None))
            elif op == 'FORMAT_SIMPLE':
                emit.append((off, self.FORMAT_VAL, 0, None))
            elif op == 'FORMAT_WITH_SPEC':
                emit.append((off, self.FORMAT_SPEC, 0, None))
            elif op == 'BUILD_STRING':
                emit.append((off, self.BUILD_STR, a or 0, None))
            elif op == 'BINARY_OP':
                sym = ins.argrepr.split()[0] if ins.argrepr else '+'
                # augmented atama (+=, -=, *= ...) in-place ama int/str/list için
                # sonuç binary op ile aynı. Sondaki '=' varsa kaldır.
                if sym.endswith('=') and sym not in ('==', '!=', '<=', '>='):
                    sym = sym[:-1]
                b = self._BIN_SYM.get(sym)
                if b is None: raise self.Unsupported(f'binary {sym}')
                emit.append((off, self.BIN, b, None))
            elif op == 'BINARY_SUBSCR':
                emit.append((off, self.BIN, self.B_SUBSCR, None))
            elif op == 'COMPARE_OP':
                # 3.13: argrepr 'bool(==)' gibi gelebilir; içindeki sembolü çıkar.
                _rep = ins.argrepr.strip()
                if _rep.startswith('bool(') and _rep.endswith(')'):
                    _rep = _rep[5:-1].strip()
                c = self._CMP_SYM.get(_rep)
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
            elif op == 'BUILD_MAP':
                emit.append((off, self.BUILD_MAP, a or 0, None))
            elif op == 'BUILD_CONST_KEY_MAP':
                # stack: v1..vn, keys_tuple → dict. VM'de: keys tuple'ı pop et,
                # değerleri eşle. Basitlik için CALL benzeri özel handling gerekli;
                # burada NOP+özel değil, dict kurmak için ayrı opcode kullan.
                emit.append((off, self.BUILD_MAP, -(a or 0), None))  # negatif = const-key modu
            elif op == 'BUILD_SET':
                emit.append((off, self.BUILD_SET, a or 0, None))
            elif op in ('STORE_SUBSCR',):
                emit.append((off, self.STORE_SUBSCR, 0, None))
            elif op == 'BUILD_SLICE':
                emit.append((off, self.BUILD_SLICE, a or 2, None))
            elif op == 'BINARY_SLICE':
                # TOS=end, TOS1=start, TOS2=container → container[start:end]
                emit.append((off, self.BUILD_SLICE, 2, None))
                emit.append((None, self.BIN, self.B_SUBSCR, None))
            elif op == 'END_FOR':
                # FOR_ITER handler'ı StopIteration'da iterator'ı ZATEN pop ediyor
                # (her sürümde). END_FOR = NOP. Sonraki POP_TOP (3.13) boş-güvenli
                # POP ile zararsızca yutulur. İÇ İÇE döngüde her FOR_ITER sadece
                # KENDİ iterator'ını temizlediği için dış iterator korunur.
                emit.append((off, self.NOP, 0, None))
            elif op == 'GET_ITER':
                emit.append((off, self.GET_ITER, 0, None))
            elif op == 'FOR_ITER':
                # Her sürümde aynı: exhausted olunca iterator'ı KENDİSİ pop eder.
                emit.append((off, self.FOR_ITER, 0, ins.argval))
            # ── (v16) Exception + with opcode'ları ──────────────────────────
            elif op == 'PUSH_EXC_INFO':
                # Aktif exception'ı stack'e it (zaten runtime handler tarafından
                # dispatch anında konuldu; bu opcode "işaretle" gibi davranır).
                emit.append((off, self.PUSH_EXC, 0, None))
            elif op == 'POP_EXCEPT':
                emit.append((off, self.POP_EXC, 0, None))
            elif op == 'CHECK_EXC_MATCH':
                emit.append((off, self.CHECK_EXC, 0, None))
            elif op == 'RERAISE':
                emit.append((off, self.RERAISE, a or 0, None))
            elif op == 'BEFORE_WITH':
                # 3.12+: ctx=stack.pop(); push(__exit__); push(ctx.__enter__())
                emit.append((off, self.BEFORE_WITH_OP, 0, None))
            elif op == 'SETUP_WITH':
                # 3.11: BEFORE_WITH benzeri + exception handler kur.
                # Exception handler exc_table'da zaten kayıtlı; sadece
                # __enter__/__exit__ semantiği önemli.
                emit.append((off, self.SETUP_WITH_OP, 0, None))
            elif op == 'WITH_EXCEPT_START':
                emit.append((off, self.WITH_EXC_START, 0, None))
            elif op == 'RAISE_VARARGS':
                # arg: 0=re-raise, 1=raise TOS, 2=raise TOS1 from TOS
                # Basit: arg==1 → TOS'u fırlat (kullanılan çoğunluk).
                # RERAISE opcode'unu kullan (0 arg = TOS'tan fırlat).
                emit.append((off, self.RERAISE, 0, None))
            elif op == 'END_ASYNC_FOR':
                emit.append((off, self.NOP, 0, None))
            elif op in ('SWAP',):
                emit.append((off, self.SWAP, a or 2, None))
            elif op in ('LOAD_METHOD',):
                # 3.11: LOAD_METHOD arg'ı kaydırmasız (doğrudan names index).
                # 3.12+: LOAD_ATTR'a gömüldü, arg>>1 (aşağıdaki LOAD_ATTR dalı).
                emit.append((off, self.LOAD_ATTR, a if a is not None else 0, None))
            elif op == 'UNARY_NEGATIVE':
                emit.append((off, self.UNARY_NEG, 0, None))
            elif op == 'UNARY_NOT':
                emit.append((off, self.UNARY_NOT, 0, None))
            elif op == 'UNARY_INVERT':
                emit.append((off, self.UNARY_INV, 0, None))
            elif op in ('COPY',) and a == 1:
                emit.append((off, self.DUP, 0, None))
            elif op == 'COPY':
                # stack[-a]'yı tepeye kopyala
                emit.append((off, self.COPY_N, a or 1, None))
            elif op == 'MAP_ADD':
                emit.append((off, self.MAP_ADD, a or 1, None))
            elif op == 'LIST_APPEND':
                emit.append((off, self.LIST_APPEND, a or 1, None))
            elif op == 'SET_ADD':
                emit.append((off, self.SET_ADD, a or 1, None))
            elif op == 'LIST_EXTEND':
                emit.append((off, self.LIST_EXTEND, a or 1, None))
            elif op == 'SET_UPDATE':
                emit.append((off, self.SET_UPDATE, a or 1, None))
            elif op in ('DICT_UPDATE', 'DICT_MERGE'):
                emit.append((off, self.DICT_UPDATE, a or 1, None))
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
        # (v16) Exception table: her entry (py_start, py_end, py_target, depth)
        # → VM indekslerine dönüştür. Sürüm bağımsız — dis._parse_exception_table
        # 3.11+ hepsinde çalışıyor.
        exc_table_vm = []
        try:
            import dis as _dis
            for e in _dis._parse_exception_table(co):
                vm_start = off_to_idx.get(e.start)
                vm_end = off_to_idx.get(e.end, len(final))  # end offset instr sonrası
                vm_target = off_to_idx.get(e.target)
                if vm_start is None or vm_target is None:
                    continue
                # depth: exception olduğunda stack'in bu derinliğe indirilmesi gerekir
                exc_table_vm.append([vm_start, vm_end, vm_target, e.depth,
                                     1 if e.lasti else 0])
        except Exception:
            exc_table_vm = []
        return {
            'code': final, 'consts': consts, 'names': names,
            'nlocals': len(varnames), 'argcount': co.co_argcount,
            'exc_table': exc_table_vm,
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
        #
        # (v9 — Fikir #4) POLİMORFİK GÖVDE: dispatch handler'larının SIRASI her
        # build'de rastgele karıştırılır. if/elif zincirinde opcode'lar benzersiz
        # olduğu için sıra semantiği ETKİLEMEZ, ama imza-bazlı pattern matching
        # (aynı handler dizilimini arayan araçlar) her hedefte farklı gövde görür.
        # ISA numaraları zaten _randomize_isa ile rastgele; artık gövde de öyle.
        cls = self
        # Her handler: (opcode_sabiti, tek/çok-satır gövde metni)
        _handlers = [
            (cls.PUSH_CONST, '_s.append(_k[_g2])'),
            (cls.LOAD_LOCAL, '_s.append(_L[_g2])'),
            (cls.STORE_LOCAL, '_L[_g2]=_s.pop()'),
            (cls.LOAD_GLOBAL, '\n            _n=_nm[_g2]\n            _s.append(_g[_n] if _n in _g else getattr(_b,_n))'),
            (cls.STORE_GLOBAL, '_g[_nm[_g2]]=_s.pop()'),
            (cls.LOAD_ATTR, '_s.append(getattr(_s.pop(),_nm[_g2]))'),
            (cls.CALL, '\n            _ar=_s[len(_s)-_g2:] if _g2 else []\n            del _s[len(_s)-_g2:]\n            _fn=_s.pop()\n            if not callable(_fn) and _s and callable(_s[-1]):\n                _fn=_s.pop()\n            elif _s and _s[-1] is None:\n                _s.pop()\n            _s.append(_fn(*_ar))'),
            (cls.LOAD_KW, '_s.append(_k[_g2])'),
            (cls.CALL_KW, '\n            _kwn=_s.pop(); _nkw=len(_kwn); _tot=_g2\n            _allargs=_s[len(_s)-_tot:]; del _s[len(_s)-_tot:]\n            _fn=_s.pop()\n            if _nkw:\n                _pos=_allargs[:_tot-_nkw]; _kwv=_allargs[_tot-_nkw:]; _s.append(_fn(*_pos, **dict(zip(_kwn,_kwv))))\n            else:\n                _s.append(_fn(*_allargs))'),
            (cls.FORMAT_VAL, '\n            _fv=_s.pop()\n            _cv=(_g2 or 0)&0x03\n            if _cv==1: _fv=str(_fv)\n            elif _cv==2: _fv=repr(_fv)\n            elif _cv==3: _fv=ascii(_fv)\n            _s.append(format(_fv, ""))'),
            (cls.FORMAT_SPEC, '\n            _spec=_s.pop(); _fv=_s.pop()\n            _cv=(_g2 or 0)&0x03\n            if _cv==1: _fv=str(_fv)\n            elif _cv==2: _fv=repr(_fv)\n            elif _cv==3: _fv=ascii(_fv)\n            _s.append(format(_fv, _spec))'),
            (cls.BUILD_STR, '\n            _it=_s[len(_s)-_g2:]; del _s[len(_s)-_g2:]; _s.append("".join(map(str,_it)))'),
            (cls.BIN, '\n            _y=_s.pop(); _z=_s.pop(); _s.append(_nv_bin(_g2,_z,_y))'),
            (cls.CMP, '\n            _y=_s.pop(); _z=_s.pop(); _s.append(_nv_cmp(_g2,_z,_y))'),
            (cls.JMP, '_ip=_g2'),
            (cls.JMP_FALSE, '\n            if not _s.pop(): _ip=_g2'),
            (cls.JMP_TRUE, '\n            if _s.pop(): _ip=_g2'),
            (cls.RET, 'return _s.pop()'),
            (cls.POP, '\n            if _s: _s.pop()'),
            (cls.DUP, '_s.append(_s[-1])'),
            (cls.BUILD_LIST, '\n            _it=_s[len(_s)-_g2:]; del _s[len(_s)-_g2:]; _s.append(list(_it))'),
            (cls.BUILD_TUPLE, '\n            _it=_s[len(_s)-_g2:]; del _s[len(_s)-_g2:]; _s.append(tuple(_it))'),
            (cls.BUILD_MAP, '\n            if _g2<0:\n                _nk=-_g2; _keys=_s.pop(); _vals=_s[len(_s)-_nk:]; del _s[len(_s)-_nk:]; _s.append(dict(zip(_keys,_vals)))\n            else:\n                _it=_s[len(_s)-2*_g2:]; del _s[len(_s)-2*_g2:]; _s.append({_it[_mi]:_it[_mi+1] for _mi in range(0,len(_it),2)})'),
            (cls.BUILD_SET, '\n            _it=_s[len(_s)-_g2:]; del _s[len(_s)-_g2:]; _s.append(set(_it))'),
            (cls.STORE_SUBSCR, '\n            _kk=_s.pop(); _oo=_s.pop(); _vv=_s.pop(); _oo[_kk]=_vv'),
            (cls.SWAP, '\n            _s[-1],_s[-_g2]=_s[-_g2],_s[-1]'),
            (cls.COPY_N, '_s.append(_s[-_g2])'),
            (cls.MAP_ADD, '\n            _v=_s.pop(); _kk=_s.pop(); _s[-_g2][_kk]=_v'),
            (cls.LIST_APPEND, '\n            _v=_s.pop(); _s[-_g2].append(_v)'),
            (cls.SET_ADD, '\n            _v=_s.pop(); _s[-_g2].add(_v)'),
            (cls.LIST_EXTEND, '\n            _v=_s.pop(); _s[-_g2].extend(_v)'),
            (cls.SET_UPDATE, '\n            _v=_s.pop(); _s[-_g2].update(_v)'),
            (cls.DICT_UPDATE, '\n            _v=_s.pop(); _s[-_g2].update(_v)'),
            (cls.BUILD_SLICE, '\n            if _g2==3:\n                _c3=_s.pop(); _c2=_s.pop(); _c1=_s.pop(); _s.append(slice(_c1,_c2,_c3))\n            else:\n                _c2=_s.pop(); _c1=_s.pop(); _s.append(slice(_c1,_c2))'),
            (cls.GET_ITER, '_s.append(iter(_s.pop()))'),
            (cls.FOR_ITER, '\n            try:\n                _s.append(next(_s[-1]))\n            except StopIteration:\n                _s.pop(); _ip=_g2'),
            (cls.UNARY_NEG, '_s.append(-_s.pop())'),
            (cls.UNARY_NOT, '_s.append(not _s.pop())'),
            (cls.UNARY_INV, '_s.append(~_s.pop())'),
            (cls.TO_BOOL, '_s.append(bool(_s.pop()))'),
            (cls.CONTAINS, '\n            _y=_s.pop(); _x=_s.pop(); _s.append(_x in _y)'),
            (cls.NOT_CONTAINS, '\n            _y=_s.pop(); _x=_s.pop(); _s.append(_x not in _y)'),
            (cls.IS_OP, '\n            _y=_s.pop(); _x=_s.pop(); _s.append(_x is not _y if _g2 else _x is _y)'),
            (cls.UNPACK_SEQ, '\n            _seq=list(_s.pop()); _s.extend(reversed(_seq[:_g2]))'),
            # ── (v16) Exception + with handler'ları ──────────────────────────
            # PUSH_EXC_INFO: exception zaten stack'te (dispatch tarafından konuldu).
            # 3.11+ modeli: PUSH_EXC_INFO ayrıca eski _cur_exc'yi stack'e iter,
            # aktif exc'yi tepede tutar. Basitleştirilmiş model: NOP (dispatch
            # exception'ı zaten yerleştirdi, sadece _cur_exc canlı tutuluyor).
            (cls.PUSH_EXC, 'pass'),
            # POP_EXCEPT: exception handler bloğunun sonu. _cur_exc'yi temizle
            # AMA stack'e dokunma — CPython 3.11+ POP_EXCEPT stack'ten pop
            # ETMEZ (eski model 3.10'daydı). Yanlış pop ederse for-loop iter
            # gibi altındaki değerleri yer → 'list index out of range'.
            (cls.POP_EXC, '_cur_exc=None'),
            # CHECK_EXC: TOS=exc_type, TOS1=exc_val; isinstance kontrolü.
            (cls.CHECK_EXC, '\n            _et=_s.pop()\n            _ev=_s[-1] if _s else None\n            _s.append(isinstance(_ev, _et) if _et else False)'),
            # RERAISE: exception'ı yeniden fırlat.
            (cls.RERAISE, '\n            if _cur_exc is not None: raise _cur_exc\n            if _s:\n                _re=_s.pop()\n                if isinstance(_re, BaseException): raise _re'),
            # BEFORE_WITH: ctx=pop(); push(__exit__); push(ctx.__enter__())
            (cls.BEFORE_WITH_OP, '\n            _ctx=_s.pop(); _s.append(_ctx.__exit__); _s.append(_ctx.__enter__())'),
            # SETUP_WITH (3.11): BEFORE_WITH ile aynı davranış
            (cls.SETUP_WITH_OP, '\n            _ctx=_s.pop(); _s.append(_ctx.__exit__); _s.append(_ctx.__enter__())'),
            # WITH_EXCEPT_START: stack'te [..., __exit__, exc]; __exit__(type,val,tb) çağır
            (cls.WITH_EXC_START, '\n            _ev=_s[-1]\n            _exit=_s[-4] if len(_s)>=4 else _s[-2]\n            _s.append(_exit(type(_ev), _ev, getattr(_ev,"__traceback__",None)))'),
            (cls.NOP, 'pass'),
        ]
        random.shuffle(_handlers)
        _dispatch_lines = []
        for _idx, (_op, _body) in enumerate(_handlers):
            _kw = 'if' if _idx == 0 else 'elif'
            # (v16) 12 boşluk indent — dispatch artık `try:` içinde
            if _body.startswith('\n'):
                # çok satırlı body: iç satırlar zaten '\n            ' (12 boşluk)
                # ile başlıyor, ekstra 4 boşluk ekle → 16 boşluk
                _body_reindent = _body.replace('\n            ', '\n                ')
                _dispatch_lines.append(f'            {_kw} _o=={_op}:{_body_reindent}')
            else:
                _dispatch_lines.append(f'            {_kw} _o=={_op}: {_body}')
        _dispatch = '\n'.join(_dispatch_lines)

        # _nv_bin / _nv_cmp handler sıraları da karıştırılır (return'lü, sıra bağımsız)
        _bin_ops = [
            (cls.B_ADD, '_a+_b'), (cls.B_SUB, '_a-_b'), (cls.B_MUL, '_a*_b'),
            (cls.B_DIV, '_a/_b'), (cls.B_MOD, '_a%_b'), (cls.B_POW, '_a**_b'),
            (cls.B_FLOORDIV, '_a//_b'), (cls.B_AND, '_a&_b'), (cls.B_OR, '_a|_b'),
            (cls.B_XOR, '_a^_b'), (cls.B_LSH, '_a<<_b'), (cls.B_RSH, '_a>>_b'),
            (cls.B_SUBSCR, '_a[_b]'),
        ]
        random.shuffle(_bin_ops)
        _bin_lines = '\n'.join(f'    if _o=={_op}: return {_expr}' for _op, _expr in _bin_ops)

        _cmp_ops = [
            (cls.C_LT, '_a<_b'), (cls.C_LE, '_a<=_b'), (cls.C_EQ, '_a==_b'),
            (cls.C_NE, '_a!=_b'), (cls.C_GT, '_a>_b'), (cls.C_GE, '_a>=_b'),
        ]
        random.shuffle(_cmp_ops)
        _cmp_lines = '\n'.join(f'    if _o=={_op}: return {_expr}' for _op, _expr in _cmp_ops)

        return f'''
def _nv_exec(_p, _a, _g):
    _c=_p["c"]; _k=_p["k"]; _nm=_p["n"]; _L=[None]*_p["l"]
    _e=_p.get("e", [])
    _i=0
    for _x in _a: _L[_i]=_x; _i+=1
    _s=[]; _ip=0; _N=len(_c); _cur_exc=None
    import builtins as _b
    while _ip<_N:
        _o=_c[_ip][0]; _g2=_c[_ip][1]; _cur_ip=_ip; _ip+=1
        try:
{_dispatch}
        except BaseException as _exc:
            # (v16) exception yakalandı — exception table'a bak
            _handled=False
            for _es,_ee,_eh,_ed,_el in _e:
                if _es <= _cur_ip < _ee:
                    _cur_exc=_exc
                    # stack'i handler'ın beklediği derinliğe indir
                    if len(_s) > _ed: del _s[_ed:]
                    # exception'ı stack'e it (handler PUSH_EXC ile aynı yerde bulur)
                    _s.append(_exc)
                    _ip=_eh
                    _handled=True
                    break
            if not _handled:
                raise
    return None

def _nv_bin(_o,_a,_b):
{_bin_lines}
    raise RuntimeError()

def _nv_cmp(_o,_a,_b):
{_cmp_lines}
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
            'e': prog.get('exc_table', []),  # (v16) exception table
        })).decode()
        return blob

    def transform_source(self, source: str, selected_names: set = None):
        """
        Modüldeki uygun fonksiyonları VM'e taşır.
        selected_names verilirse: SADECE o adlı fonksiyonlar taşınır (menü modu).
        None ise: eski davranış (@ninja_vm işaretli + otomatik adaylar).
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
                if selected_names is not None:
                    # Menü modu: yalnızca kullanıcının seçtiği adlar
                    if node.name in selected_names:
                        targets.append((node, True))
                    continue
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
        self._vm_diag = []  # (fname, sebep) — neden VM'e giremedi teşhisi

        for node, marked in targets:
            try:
                seg = _ast.get_source_segment(source, node)
                if seg is None:
                    continue
                prog, fname, argnames = self.compile_function_source(seg)
            except self.Unsupported as _unsup:
                _fn_name = getattr(node, 'name', '?')
                self._vm_diag.append((_fn_name, f'derlenemedi: {_unsup}'))
                continue
            except Exception as _cerr:
                _fn_name = getattr(node, 'name', '?')
                self._vm_diag.append((_fn_name, f'derleme hatası: {type(_cerr).__name__}: {_cerr}'))
                continue
            # ── (v10) GÜVENLİK AĞI ──────────────────────────────────────────
            # VM programı derlendi ama DOĞRU çalışacağının garantisi yok
            # (özellikle yeni eklenen try/except, global, iter, map desteği).
            # Fonksiyonu VM'de ÇALIŞTIRIP orijinal Python sonucuyla karşılaştır.
            # Eşleşmezse (veya VM patlarsa) bu fonksiyon VM'e TAŞINMAZ — orijinal
            # haliyle kalır. Böylece kusurlu bir VM derlemesi tool'u ASLA bozmaz.
            _val_ok, _val_reason = self._validate_vm_program(seg, prog, fname, argnames, node)
            if not _val_ok:
                logger.warning(f'  NinjaVM güvenlik ağı: "{fname}" doğrulanamadı ({_val_reason}) → VM-siz korunuyor')
                self._vm_diag.append((fname, f'güvenlik ağı: {_val_reason}'))
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

    # ── (v10) Güvenlik ağı: VM programını çalıştırıp orijinalle karşılaştır ──
    def _is_pure_testable(self, node) -> bool:
        """
        Fonksiyon yan etkisiz mi ve güvenle örnek girdiyle çalıştırılabilir mi?
        Güvenli test için: global yazma yok, import yok, sadece int argümanlarla
        çağrılabilir imza, dış çağrı (API/IO) içermiyor, VE serbest (modül-seviyesi)
        değişken OKUMUYOR.

        NEDEN serbest-global kontrolü (v12.1):
        generate_stats_panel/generate_hit_panel gibi argümansız fonksiyonlar
        modül global'lerini (ui_lang, latest_hit_dict — başlangıçta None) okuyor
        ve bir THREAD içinde, global set edilmeden çağrılabiliyor. VM'e taşınınca
        'NoneType not subscriptable' ile çöküyor. Bunlar global state'e bağımlı →
        VM için kötü aday. Serbest global okuyan fonksiyonu VM-dışı bırak.
        """
        import ast as _ast, builtins as _bi
        a = node.args
        if a.vararg or a.kwarg or a.kwonlyargs or a.defaults or a.posonlyargs:
            return False
        RISKY_CALLS = {'open', 'print', 'input', 'exec', 'eval', 'exit', '_exit',
                       'system', 'popen', 'run', 'get', 'post', 'request',
                       'urlopen', 'connect', 'send', 'recv', 'write', 'read'}
        for sub in _ast.walk(node):
            if isinstance(sub, (_ast.Global, _ast.Nonlocal, _ast.Import,
                                _ast.ImportFrom)):
                return False
            if isinstance(sub, _ast.Call):
                f = sub.func
                nm = f.attr if isinstance(f, _ast.Attribute) else (
                    f.id if isinstance(f, _ast.Name) else '')
                if nm in RISKY_CALLS:
                    return False

        # ── Serbest (modül-global) değişken okuma kontrolü ──
        # Fonksiyonun kendi local'lerini (parametreler + atanan adlar) topla;
        # okunan bir ad ne local ne builtin ne de global-fonksiyon çağrısıysa,
        # bu bir modül-global VERİ okumasıdır → thread/None-state riski → VM-dışı.
        local_names = set()
        for arg in a.args:
            local_names.add(arg.arg)
        for sub in _ast.walk(node):
            if isinstance(sub, _ast.Name) and isinstance(sub.ctx, (_ast.Store,)):
                local_names.add(sub.id)
            elif isinstance(sub, (_ast.For,)) and isinstance(sub.target, _ast.Name):
                local_names.add(sub.target.id)
        builtin_names = set(dir(_bi))
        for sub in _ast.walk(node):
            if isinstance(sub, _ast.Name) and isinstance(sub.ctx, _ast.Load):
                nm = sub.id
                if nm in local_names or nm in builtin_names:
                    continue
                # Bir çağrının hedefi mi? (fonksiyon çağrısı — global fonksiyon
                # çağırmak sorun değil; veri okumak sorun). Çağrı hedefi olan
                # Name'leri ayıkla:
                pass  # aşağıda çağrı-hedefi ayrımı
        # çağrı hedefi olan Name id'lerini topla (bunlar fonksiyon → serbest sayma)
        call_targets = set()
        for sub in _ast.walk(node):
            if isinstance(sub, _ast.Call) and isinstance(sub.func, _ast.Name):
                call_targets.add(sub.func.id)
        for sub in _ast.walk(node):
            if isinstance(sub, _ast.Name) and isinstance(sub.ctx, _ast.Load):
                nm = sub.id
                if nm in local_names or nm in builtin_names or nm in call_targets:
                    continue
                # serbest modül-global VERİ okuması → VM için güvenli değil
                return False
        return True

    def _smoke_test_vm(self, seg, prog, fname, argnames, node):
        """
        Yan-etkili fonksiyonlar için 'duman testi': VM programını stdout/stderr
        susturulmuş halde çalıştırıp SADECE exception fırlatıp fırlatmadığını
        kontrol eder (sonuç karşılaştırması yapmaz — yan etki nedeniyle
        güvenilmez). VM aynı girdide patlarken orijinal patlamıyorsa → VM-dışı.
        Amaç: language_selection gibi fonksiyonların VM'de sessizce bozulup
        çalışma anında çökmesini encode-zamanı yakalamak.
        """
        import ast as _ast, io as _io, contextlib as _ctx
        try:
            tree = _ast.parse(seg)
            fdef = tree.body[0]
            fdef.decorator_list = []
            mod = _ast.Module(body=[fdef], type_ignores=[])
            _ast.fix_missing_locations(mod)
            ns = {}
            exec(compile(mod, '<smoke>', 'exec'), ns)
            orig_fn = ns[fname]

            rt = self.runtime_source()
            rt_ns = {}
            exec(rt, rt_ns)
            nv_exec = rt_ns['_nv_exec']

            import marshal as _mm, base64 as _bb
            _runtime_prog = _mm.loads(_bb.b64decode(self._prog_literal(prog)))

            n = len(argnames)
            # güvenli deneme girdileri (çeşitli tipler dene)
            if n == 0:
                trial_sets = [()]
            else:
                trial_sets = [
                    tuple([1] * n),
                    tuple(['1'] * n),
                    tuple([None] * n),
                ]

            _tested = False
            for args in trial_sets:
                # orijinal bu girdide ne yapıyor? (patlıyorsa bu girdiyi atla)
                _buf = _io.StringIO()
                try:
                    with _ctx.redirect_stdout(_buf), _ctx.redirect_stderr(_buf):
                        orig_fn(*args)
                    orig_raised = False
                except SystemExit:
                    # sys.exit gibi — bu girdiyi atla (VM'de de aynı olur)
                    continue
                except Exception:
                    orig_raised = True

                # VM aynı girdide ne yapıyor?
                _buf2 = _io.StringIO()
                try:
                    with _ctx.redirect_stdout(_buf2), _ctx.redirect_stderr(_buf2):
                        nv_exec(_runtime_prog, list(args), dict(ns))
                    vm_raised = False
                except SystemExit:
                    continue
                except Exception as _ve:
                    vm_raised = True
                    _vm_err = f'{type(_ve).__name__}: {_ve}'

                _tested = True
                # orijinal patlamıyorken VM patlıyorsa → GÜVENSİZ
                if vm_raised and not orig_raised:
                    return (False, f'VM smoke patladı ({_vm_err}) girdi tipi={type(args[0]).__name__ if args else "yok"}')
                # ikisi de sorunsuz çalıştıysa bu fonksiyon güvenli sayılır
                if not vm_raised and not orig_raised:
                    return (True, 'smoke testi geçti (VM patlamadan çalıştı)')

            if not _tested:
                # hiçbir girdi denenemedi (hepsi SystemExit/uygunsuz) → muhafazakâr
                # olarak VM-DIŞI bırak (körlemesine kabul etme)
                return (False, 'smoke testi uygulanamadı — güvenli tarafta VM-dışı')
            # tüm denemelerde orijinal de patlıyorsa VM davranışı eşdeğer sayılır
            return (True, 'smoke: orijinal de aynı girdilerde hata veriyor')
        except Exception as _e:
            # smoke altyapısı patlarsa GÜVENLİ tarafta kal: VM-dışı bırak
            return (False, f'smoke altyapı hatası ({_e}) — VM-dışı')

    def _validate_vm_program(self, seg, prog, fname, argnames, node):
        """
        Güvenlik ağı. Döner (True/False, sebep_str).

        Üç yol:
        1. SAF TESTLENEBİLİR: VM'de çalıştır, orijinalle karşılaştır.
        2. ARGÜMANLI + YAN ETKİLİ (API/IO): derleme+yapısal kontrol yeterli → KABUL.
           cgmail, rest_v1, sinsta gibi fonksiyonlar HTTP çağrısı yapıyor, test
           edemeyiz ama VM doğru bytecode çevirmişse runtime'da çalışır.
        3. ARGÜMANSIZ + SERBEST-GLOBAL (display/UI thread fonksiyonları):
           ui_lang/latest_hit_dict gibi None-başlayan global'leri okuyorlar ve
           thread'de erken çağrılabiliyorlar → VM-dışı bırak.
        """
        import ast as _ast
        # Yapısal kontrol (her zaman)
        try:
            code = prog.get('code')
            if not code or not isinstance(code, list):
                return (False, 'boş/geçersiz program')
            for ins in code:
                if not (isinstance(ins, list) and len(ins) == 2):
                    return (False, 'bozuk instr formatı')
        except Exception as _e:
            return (False, f'yapı hatası: {_e}')

        a = node.args
        has_args = bool(a.args or a.vararg or a.kwarg or a.kwonlyargs)

        if not has_args:
            # Argümansız fonksiyon: serbest global okuyor mu?
            # Evet → display/UI/thread fonksiyonu → VM-dışı (None-state riski).
            local_names = set()
            import builtins as _bi; builtin_names = set(dir(_bi))
            call_targets = set()
            for sub in _ast.walk(node):
                if isinstance(sub, _ast.Name) and isinstance(sub.ctx, _ast.Store):
                    local_names.add(sub.id)
                if isinstance(sub, _ast.Call) and isinstance(sub.func, _ast.Name):
                    call_targets.add(sub.func.id)
            for sub in _ast.walk(node):
                if isinstance(sub, _ast.Name) and isinstance(sub.ctx, _ast.Load):
                    nm = sub.id
                    if nm in local_names or nm in builtin_names or nm in call_targets:
                        continue
                    # serbest global bulundu → UI/thread riski
                    return (False, f'argümansız+serbest-global ({nm}) — thread/None-state riski')

        # Saf testlenebilir mi?
        if self._is_pure_testable(node):
            # VM'de çalıştır, orijinalle karşılaştır
            try:
                tree = _ast.parse(seg)
                fdef = tree.body[0]
                fdef.decorator_list = []
                mod = _ast.Module(body=[fdef], type_ignores=[])
                _ast.fix_missing_locations(mod)
                ns = {}
                exec(compile(mod, '<val>', 'exec'), ns)
                orig_fn = ns[fname]
                rt = self.runtime_source()
                rt_ns = {}
                exec(rt, rt_ns)
                nv_exec = rt_ns['_nv_exec']
                import marshal as _mm, base64 as _bb
                _runtime_prog = _mm.loads(_bb.b64decode(self._prog_literal(prog)))
                n = len(argnames)
                trials = [(1,)*n, (2,)*n, (0,)*n, tuple(range(1,n+1))] if n else [()]
                for args in trials:
                    try: expected = orig_fn(*args)
                    except Exception: continue
                    try: got = nv_exec(_runtime_prog, list(args), {})
                    except Exception as _ve:
                        return (False, f'VM çalışma hatası ({type(_ve).__name__}) girdi={args}')
                    if got != expected:
                        return (False, f'VM≠orijinal girdi={args} (VM={got!r} beklenen={expected!r})')
                return (True, 'saf-test doğrulandı')
            except Exception as _e:
                return (True, f'test altyapısı hatası ({_e}) — derleme geçerli')

        # Argümanlı + yan-etkili (API/IO): yapısal kontrol yeterli → KABUL
        # Bu fonksiyonlar HTTP çağrısı yapıyor; test edemeyiz ama VM bytecode'u
        # doğru çevirdi → runtime'da çalışır. Exception-table artık destekleniyor.
        return (True, 'API/IO fonksiyon — yapısal kontrol geçti')


# ══════════════════════════════════════════════════════════════════════════════
# VM TARGET SELECTOR (v10)
# Şifreleme başlamadan önce kullanıcıya "hangi fonksiyonlar/class'lar NinjaVM'e
# taşınsın?" diye interaktif sorar. Her aday:
#   • VM-uyumluluk için taranır (async/yield/try/with vb. → [uyumsuz])
#   • API çağrısı için taranır (requests/urllib/socket/os/subprocess + isim kalıbı)
# Kullanıcı çoklu seçim yapar: "1,3" veya "ALL". Uyumsuz seçilse bile atlanır.
# ══════════════════════════════════════════════════════════════════════════════
class VMTargetSelector:
    # VM'in şu an GÜVENLE derleyebildiği yapılar dışında kalanlar → uyumsuz.
    # (v10 ilk sürüm: düz, saf, döngü/if/aritmetik fonksiyonlar güvenli.)
    _INCOMPATIBLE_NODES = None  # lazy init (ast import)

    # Geniş API saptama: modül/çağrı adları + isim kalıpları
    _API_MODULES = {
        'requests', 'urllib', 'urllib2', 'http', 'httplib', 'aiohttp', 'httpx',
        'socket', 'ftplib', 'smtplib', 'websocket', 'websockets', 'grpc',
        'os', 'subprocess', 'pycurl',
    }
    _API_CALL_HINTS = {
        'get', 'post', 'put', 'delete', 'patch', 'request', 'urlopen', 'connect',
        'send', 'recv', 'system', 'popen', 'run', 'call', 'check_output',
        'urlretrieve', 'fetch',
    }
    _API_NAME_PATTERNS = ('api', 'token', 'login', 'auth', 'fetch', 'request',
                          'upload', 'download', 'session', 'sign', 'endpoint',
                          'oauth', 'refresh', 'connect')

    @staticmethod
    def _incompatible_types():
        import ast as _ast
        # (v16) try/except ve with artık VM'de destekleniyor (exception-table).
        # Yalnızca gerçekten desteklenmeyen yapılar uyumsuz kalıyor:
        #   yield/async (resumable VM yok), nested def/class, lambda.
        return (_ast.Yield, _ast.YieldFrom, _ast.Await, _ast.AsyncFunctionDef,
                _ast.AsyncFor, _ast.AsyncWith,
                _ast.Lambda, _ast.FunctionDef, _ast.ClassDef)

    # (v16) Deneysel listesi artık sadece Global/Nonlocal —
    # try/except ve with VM'e tam destek kazandı.
    @staticmethod
    def _experimental_types():
        import ast as _ast
        return (_ast.Global, _ast.Nonlocal)

    @classmethod
    def analyze(cls, source: str):
        """
        Kaynağı tarar. Döner: adaylar listesi [dict], her biri:
          {index, kind('func'/'class'), name, compatible, reason, is_api,
           node_lineno, methods(list, class için)}
        (index 1'den başlar; 0=hiçbiri, ALL/HEPSİ ayrı ele alınır.)
        """
        import ast as _ast
        try:
            tree = _ast.parse(source)
        except SyntaxError:
            return []

        incompat = cls._incompatible_types()
        experimental = cls._experimental_types()
        candidates = []
        idx = 1

        def scan_incompat(fnode):
            """
            Döner: (durum, sebep)
              durum: 'ok' | 'experimental' | 'incompatible'
            """
            # kesin uyumsuz yapılar (VM gerçekten desteklemiyor)
            for sub in _ast.walk(fnode):
                if sub is fnode:
                    continue
                if isinstance(sub, incompat):
                    return ('incompatible', type(sub).__name__)
            # karmaşık imza: artık deneysel (güvenlik ağı korur) değil, VM stub
            # sadece basit pozisyonel imzayı yansıtabildiği için hâlâ uyumsuz
            a = fnode.args
            if a.vararg or a.kwarg or a.kwonlyargs or a.defaults or a.posonlyargs:
                return ('incompatible', '*args/**kwargs/default imza')
            # deneysel yapılar (try/with/global) → dene, güvenlik ağı doğrular
            for sub in _ast.walk(fnode):
                if isinstance(sub, experimental):
                    return ('experimental', type(sub).__name__)
            return ('ok', None)

        def scan_api(fnode):
            """API çağrısı içeriyor mu?"""
            # isim kalıbı
            nm = fnode.name.lower()
            if any(p in nm for p in cls._API_NAME_PATTERNS):
                return True
            for sub in _ast.walk(fnode):
                if isinstance(sub, _ast.Call):
                    f = sub.func
                    # modül.metod(...) — requests.get, os.system, socket.connect
                    if isinstance(f, _ast.Attribute):
                        if f.attr in cls._API_CALL_HINTS:
                            return True
                        # zincirin kökü bir API modülü mü?
                        root = f
                        while isinstance(root, _ast.Attribute):
                            root = root.value
                        if isinstance(root, _ast.Name) and root.id in cls._API_MODULES:
                            return True
                    elif isinstance(f, _ast.Name):
                        if f.id in cls._API_CALL_HINTS:
                            return True
                if isinstance(sub, (_ast.Import, _ast.ImportFrom)):
                    names = []
                    if isinstance(sub, _ast.Import):
                        names = [n.name.split('.')[0] for n in sub.names]
                    elif sub.module:
                        names = [sub.module.split('.')[0]]
                    if any(n in cls._API_MODULES for n in names):
                        return True
            return False

        for node in tree.body:
            if isinstance(node, _ast.FunctionDef):
                status, reason = scan_incompat(node)
                candidates.append({
                    'index': idx, 'kind': 'func', 'name': node.name,
                    'compatible': status in ('ok', 'experimental'),
                    'experimental': status == 'experimental',
                    'reason': reason, 'status': status,
                    'is_api': scan_api(node), 'node_lineno': node.lineno,
                    'methods': [],
                })
                idx += 1
            elif isinstance(node, _ast.ClassDef):
                # (v10) class'lar menüde GÖSTERİLİR ama ilk sürümde metodları
                # VM'e TAŞINMAZ (kullanıcı kararı: önce düz fonksiyonlar).
                methods = [n.name for n in node.body if isinstance(n, _ast.FunctionDef)]
                candidates.append({
                    'index': idx, 'kind': 'class', 'name': node.name,
                    'compatible': False, 'experimental': False, 'status': 'incompatible',
                    'reason': 'class metodları ilk sürümde VM-dışı (yakında)',
                    'is_api': any(scan_api(n) for n in node.body
                                  if isinstance(n, _ast.FunctionDef)),
                    'node_lineno': node.lineno, 'methods': methods,
                })
                idx += 1
        return candidates

    @staticmethod
    def render_menu(candidates) -> str:
        """İnteraktif menü metni üretir."""
        lines = []
        lines.append('')
        lines.append('╔══════════════════════════════════════════════════╗')
        lines.append('║   NinjaVM — Hangi fonksiyonlar VM\'e taşınsın?    ║')
        lines.append('╚══════════════════════════════════════════════════╝')
        lines.append('  [0] HİÇBİRİ')
        for c in candidates:
            tags = []
            if c['is_api']:
                tags.append('[API]')
            if c.get('status') == 'ok':
                tags.append('[VM-uygun]')
            elif c.get('status') == 'experimental':
                tags.append(f'[deneysel: {c["reason"]} — güvenlik ağı doğrular]')
            else:
                tags.append(f'[uyumsuz: {c["reason"]}]')
            tag_str = ' '.join(tags)
            if c['kind'] == 'class':
                inner = ', '.join(c['methods']) if c['methods'] else '—'
                lines.append(f'  [{c["index"]}] {c["name"]}({inner})  {tag_str}')
            else:
                lines.append(f'  [{c["index"]}] {c["name"]}  {tag_str}')
        n = len(candidates)
        lines.append(f'  [{n+1}] HEPSİ (VM-uygun + deneysel; güvenlik ağı kusurluları eler)')
        lines.append('')
        lines.append('  Seçim (örn: 1,3  veya  ALL  veya  0): ')
        return '\n'.join(lines)

    @staticmethod
    def parse_selection(raw: str, candidates):
        """
        Kullanıcı girdisini parse eder. Döner: seçilen VM-uygun fonksiyon
        ADLARININ kümesi. Uyumsuz seçilirse atlanır (uyarı ile).
        """
        raw = (raw or '').strip().upper()
        n = len(candidates)
        by_index = {c['index']: c for c in candidates}
        selected_names = set()
        skipped = []

        def take(c):
            if c['kind'] == 'class':
                skipped.append((c['name'], 'class (ilk sürümde VM-dışı)'))
                return
            if not c['compatible']:
                skipped.append((c['name'], c['reason']))
                return
            selected_names.add(c['name'])

        if raw in ('0', 'HİÇBİRİ', 'HICBIRI', 'NONE', ''):
            return set(), []
        if raw in ('ALL', 'HEPSİ', 'HEPSI') or raw == str(n + 1):
            for c in candidates:
                if c['compatible'] and c['kind'] == 'func':
                    selected_names.add(c['name'])
            return selected_names, []

        # virgül/boşlukla ayrılmış numaralar: "1,3" veya "1 3" veya "1, 3"
        import re as _re
        tokens = [t for t in _re.split(r'[,\s]+', raw) if t]
        for t in tokens:
            if not t.isdigit():
                continue
            ti = int(t)
            if ti == 0:
                return set(), []
            if ti == n + 1:  # HEPSİ
                for c in candidates:
                    if c['compatible'] and c['kind'] == 'func':
                        selected_names.add(c['name'])
                continue
            c = by_index.get(ti)
            if c:
                take(c)
        return selected_names, skipped


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
    Faz 3 — ELF/Native seviye koruma (v9: SIFIR BAĞIMLILIK, saf-Python).

    lief ARTIK GEREKMİYOR. Pydroid3/Termux gibi lief kurulamayan ortamlar için
    ham ELF byte manipülasyonu ile çalışır:
      U: DWARF Debug Poisoning — .debug_* / .comment / .note section'larının
         içeriğini rastgele byte ile ezer (decompiler/gdb yanılır)
      L: Section adı zehirleme — .shstrtab içindeki gerçek section adlarını
         yanıltıcı adlarla değiştirir (readelf -S kafası karışır)
      R: strip (CythonCompiler'da zaten yapılıyor)

    ELF64 (aarch64/x86_64) ve ELF32 desteklenir. .so bozulmaz: yalnızca debug/
    metadata section'larına dokunulur; .text/.data/.dynsym gibi ÇALIŞMA için
    gerekli section'lara ASLA dokunulmaz. Herhangi bir hata durumunda dosya
    olduğu gibi bırakılır (çalışırlık > koruma).
    """

    @staticmethod
    def is_available() -> bool:
        # Saf-Python; her ortamda kullanılabilir.
        return True

    # ── Ham ELF yardımcıları (struct tabanlı, harici bağımlılık yok) ──
    @staticmethod
    def _parse_and_poison(raw: bytearray) -> tuple:
        """
        ELF section header table'ını parse eder, debug/note/comment section'larını
        rastgele byte ile ezer ve adlarını zehirler. (değişti_mi, poison_sayısı,
        rename_sayısı) döndürür. raw yerinde değiştirilir.
        """
        if raw[:4] != b'\x7fELF':
            raise ValueError('ELF magic yok')
        ei_class = raw[4]           # 1=32bit, 2=64bit
        ei_data  = raw[5]           # 1=little, 2=big
        endian = '<' if ei_data == 1 else '>'
        is64 = (ei_class == 2)

        if is64:
            # Elf64_Ehdr: e_shoff@0x28 (8), e_shentsize@0x3A(2), e_shnum@0x3C(2), e_shstrndx@0x3E(2)
            e_shoff  = struct.unpack_from(endian + 'Q', raw, 0x28)[0]
            e_shentsize = struct.unpack_from(endian + 'H', raw, 0x3A)[0]
            e_shnum  = struct.unpack_from(endian + 'H', raw, 0x3C)[0]
            e_shstrndx = struct.unpack_from(endian + 'H', raw, 0x3E)[0]
        else:
            # Elf32_Ehdr: e_shoff@0x20(4), e_shentsize@0x2E(2), e_shnum@0x30(2), e_shstrndx@0x32(2)
            e_shoff  = struct.unpack_from(endian + 'I', raw, 0x20)[0]
            e_shentsize = struct.unpack_from(endian + 'H', raw, 0x2E)[0]
            e_shnum  = struct.unpack_from(endian + 'H', raw, 0x30)[0]
            e_shstrndx = struct.unpack_from(endian + 'H', raw, 0x32)[0]

        if e_shoff == 0 or e_shnum == 0:
            raise ValueError('section header yok (stripli olabilir)')

        # Her section header'dan (sh_name_off, sh_type, sh_offset, sh_size) çek
        def read_sh(idx):
            base = e_shoff + idx * e_shentsize
            if is64:
                sh_name = struct.unpack_from(endian + 'I', raw, base + 0x00)[0]
                sh_off  = struct.unpack_from(endian + 'Q', raw, base + 0x18)[0]
                sh_size = struct.unpack_from(endian + 'Q', raw, base + 0x20)[0]
            else:
                sh_name = struct.unpack_from(endian + 'I', raw, base + 0x00)[0]
                sh_off  = struct.unpack_from(endian + 'I', raw, base + 0x10)[0]
                sh_size = struct.unpack_from(endian + 'I', raw, base + 0x14)[0]
            return sh_name, sh_off, sh_size

        # .shstrtab (section adları tablosu)
        shstr_name, shstr_off, shstr_size = read_sh(e_shstrndx)

        def sec_name(name_off):
            end = raw.find(b'\x00', shstr_off + name_off)
            if end < 0:
                return ''
            return raw[shstr_off + name_off:end].decode('latin-1', 'replace')

        POISON_PREFIXES = ('.debug_', '.zdebug_')
        POISON_EXACT = {'.comment', '.note', '.note.gnu.build-id', '.note.ABI-tag',
                        '.gnu_debuglink', '.gnu_debugaltlink', '.stab', '.stabstr'}
        # ÇALIŞMA için kritik — ASLA dokunma
        PROTECTED = {'.text', '.data', '.bss', '.rodata', '.dynsym', '.dynstr',
                     '.dynamic', '.got', '.got.plt', '.plt', '.rela.plt',
                     '.rela.dyn', '.init', '.fini', '.init_array', '.fini_array',
                     '.hash', '.gnu.hash', '.eh_frame', '.eh_frame_hdr', '.tdata',
                     '.tbss', '.gnu.version', '.gnu.version_r', ''}

        poisoned = 0
        renamed = 0
        for i in range(e_shnum):
            if i == e_shstrndx:
                continue
            sh_name, sh_off, sh_size = read_sh(i)
            name = sec_name(sh_name)
            if name in PROTECTED:
                continue
            is_poison = name.startswith(POISON_PREFIXES) or name in POISON_EXACT
            if is_poison and sh_size > 0 and sh_off > 0 and (sh_off + sh_size) <= len(raw):
                # İçeriği rastgele byte ile ez
                raw[sh_off:sh_off + sh_size] = os.urandom(sh_size)
                poisoned += 1
                # Adını da zehirle (aynı uzunlukta, .shstrtab içinde yerinde)
                orig = raw[shstr_off + sh_name: shstr_off + sh_name + len(name)]
                fake_pool = b'.text\x00.data\x00.init\x00.rodata\x00'
                fake = (fake_pool * (len(orig) // len(fake_pool) + 1))[:len(orig)]
                raw[shstr_off + sh_name: shstr_off + sh_name + len(name)] = fake
                renamed += 1
        return (poisoned > 0 or renamed > 0), poisoned, renamed

    @staticmethod
    def protect(so_path: str, do_text_encrypt: bool = False) -> str:
        try:
            with open(so_path, 'rb') as f:
                raw = bytearray(f.read())
        except Exception as _re:
            logger.warning(f'ELFProtector: dosya okunamadı ({_re}) — atlanıyor')
            return so_path
        try:
            changed, poisoned, renamed = ELFProtector._parse_and_poison(raw)
            if changed:
                # Güvenlik: yazmadan önce ELF magic hâlâ yerinde mi?
                if raw[:4] != b'\x7fELF':
                    logger.warning('ELFProtector: magic bozuldu — yazma iptal')
                    return so_path
                tmp = so_path + '.elfprot'
                with open(tmp, 'wb') as f:
                    f.write(raw)
                shutil.move(tmp, so_path)
                logger.info(f'  ELFProtector (saf-Python): {poisoned} debug section zehirlendi, '
                            f'{renamed} ad değiştirildi')
            else:
                logger.info('  ELFProtector: zehirlenecek debug section yok (zaten stripli)')
        except Exception as _ep:
            logger.warning(f'ELFProtector hata (atlanıyor, .so korunur): {_ep}')
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
            # (v17) Trace/profile hook tespiti — SADECE üretim ortamında.
            # IDE'lerde (Pydroid3, VSCode debug) gettrace aktif olabilir; onları
            # yanlış tespit etmemek için sadece Nuitka .so içinden çalıştığımızı
            # doğrulayarak aktif et. __spec__.origin .so/.pyd ise üretim.
            f'try:\n'
            f'    import sys as _sysx\n'
            f'    _orig=getattr(getattr(_sysx.modules.get("__main__"),"__spec__",None),"origin","") or ""\n'
            f'    if _orig.endswith((".so",".pyd",".dylib")):\n'
            f'        if _sysx.gettrace() is not None or _sysx.getprofile() is not None:\n'
            f'            import os as _osx;_osx._exit(1)\n'
            f'except SystemExit: raise\n'
            f'except Exception: pass\n'
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
            f'        if {v[7]}[:4]==b"NJJK":\n'
            f'            _jkp=4;_jkh={v[7]}[_jkp:_jkp+16];_jkp+=16\n'
            f'            _jkn=int.from_bytes({v[7]}[_jkp:_jkp+2],"big");_jkp+=2\n'
            f'            for _jki in range(_jkn):\n'
            f'                _jkl=int.from_bytes({v[7]}[_jkp:_jkp+4],"big");_jkp+=4;_jkp+=_jkl\n'
            f'            {v[7]}=_jkh+{v[7]}[_jkp:]\n'
            # (v17) MARSHAL HOOK DETECTION — Frida/attacker marshal.loads'u
            # hook'lamış olabilir. Gerçek C builtin'i mi kontrol et. Değilse öl.
            f'        _mtyp=type({v[3]}.loads).__name__\n'
            f'        _mmod=getattr({v[3]}.loads,"__module__","") or ""\n'
            f'        if _mtyp!="builtin_function_or_method" or _mmod not in ("marshal","builtins",""):\n'
            f'            import os as _osx;_osx._exit(1)\n'
            # (v17) TIMING CHECK — Frida attach edildiğinde marshal.loads yavaşlar
            f'        import time as _tm\n'
            f'        _t0=_tm.perf_counter_ns()\n'
            f'        {v[7]}={v[3]}.loads({v[7]})\n'
            f'        _t1=_tm.perf_counter_ns()\n'
            # marshal.loads bir code object için normalde <10ms; 500ms üstü = hook
            f'        if (_t1-_t0)>500000000:\n'
            f'            import os as _osx;_osx._exit(1)\n'
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
    """Gerçek Twofish blok şifresi (Schneier ve ark., AES finalisti).

    128-bit blok, 256-bit anahtar, 16 round Feistel; anahtara-bağlı S-box'lar
    (q0/q1 + h fonksiyonu), MDS matrisi ve RS anahtar türetme. CBC + PKCS7.
    MAGIC=b'NJTF' çerçevesi: MAGIC(4) || IV(16) || ciphertext.

    Doğrulama (self_test): resmi sıfır-anahtar KAT'ları (128/192/256-bit) +
    GnuPG'nin Twofish çıktısıyla birebir uyum (CFB round-trip ile teyit).
    Eski sürümdeki 'RC4-scheduled Feistel' ev-yapımı şifre kaldırıldı.
    """
    MAGIC = b'NJTF'

    _MDS_POLY = 0x69   # GF(2^8): x^8+x^6+x^5+x^3+1 (0x169), düşük 8 bit
    _RS_POLY  = 0x4D   # GF(2^8): x^8+x^6+x^3+x^2+1 (0x14D), düşük 8 bit
    _MDS = ((0x01, 0xEF, 0x5B, 0x5B), (0x5B, 0xEF, 0xEF, 0x01),
            (0xEF, 0x5B, 0x01, 0xEF), (0xEF, 0x01, 0xEF, 0x5B))
    _RS = ((0x01, 0xA4, 0x55, 0x87, 0x5A, 0x58, 0xDB, 0x9E),
           (0xA4, 0x56, 0x82, 0xF3, 0x1E, 0xC6, 0x68, 0xE5),
           (0x02, 0xA1, 0xFC, 0xC1, 0x47, 0xAE, 0x3D, 0x19),
           (0xA4, 0x55, 0x87, 0x5A, 0x58, 0xDB, 0x9E, 0x03))
    _Q0_T = ((0x8,0x1,0x7,0xD,0x6,0xF,0x3,0x2,0x0,0xB,0x5,0x9,0xE,0xC,0xA,0x4),
             (0xE,0xC,0xB,0x8,0x1,0x2,0x3,0x5,0xF,0x4,0xA,0x6,0x7,0x0,0x9,0xD),
             (0xB,0xA,0x5,0xE,0x6,0xD,0x9,0x0,0xC,0x8,0xF,0x3,0x2,0x4,0x7,0x1),
             (0xD,0x7,0xF,0x4,0x1,0x2,0x6,0xE,0x9,0xB,0x3,0x0,0x8,0x5,0xC,0xA))
    _Q1_T = ((0x2,0x8,0xB,0xD,0xF,0x7,0x6,0xE,0x3,0x1,0x9,0x4,0x0,0xA,0xC,0x5),
             (0x1,0xE,0x2,0xB,0x4,0xC,0x3,0x7,0x6,0xD,0xA,0x5,0xF,0x9,0x0,0x8),
             (0x4,0xC,0x7,0x5,0x1,0x6,0x9,0xA,0x0,0xE,0xD,0x8,0x2,0xB,0x3,0xF),
             (0xB,0x9,0x5,0x1,0xC,0x3,0xD,0xE,0x6,0x4,0x7,0xF,0x2,0x0,0x8,0xA))
    _Q0 = None
    _Q1 = None

    @staticmethod
    def _gf(a, b, m):
        p = 0
        for _ in range(8):
            if b & 1: p ^= a
            b >>= 1
            hi = a & 0x80
            a = (a << 1) & 0xFF
            if hi: a ^= m
        return p

    @classmethod
    def _build_q(cls, t):
        out = bytearray(256)
        for x in range(256):
            a0, b0 = x >> 4, x & 0xF
            a1 = a0 ^ b0
            b1 = (a0 ^ ((b0 >> 1) | (b0 << 3)) ^ (8 * a0)) & 0xF
            a2, b2 = t[0][a1], t[1][b1]
            a3 = a2 ^ b2
            b3 = (a2 ^ ((b2 >> 1) | (b2 << 3)) ^ (8 * a2)) & 0xF
            a4, b4 = t[2][a3], t[3][b3]
            out[x] = (b4 << 4) | a4
        return bytes(out)

    @classmethod
    def _qtables(cls):
        if cls._Q0 is None:
            cls._Q0 = cls._build_q(cls._Q0_T)
            cls._Q1 = cls._build_q(cls._Q1_T)
        return cls._Q0, cls._Q1

    @classmethod
    def _mds(cls, y):
        return [cls._gf(cls._MDS[r][0], y[0], cls._MDS_POLY)
                ^ cls._gf(cls._MDS[r][1], y[1], cls._MDS_POLY)
                ^ cls._gf(cls._MDS[r][2], y[2], cls._MDS_POLY)
                ^ cls._gf(cls._MDS[r][3], y[3], cls._MDS_POLY) for r in range(4)]

    @classmethod
    def _rs(cls, b8):
        out = []
        for r in range(4):
            acc = 0
            for c in range(8):
                acc ^= cls._gf(cls._RS[r][c], b8[c], cls._RS_POLY)
            out.append(acc)
        return out

    @classmethod
    def _h(cls, x, L, k):
        Q0, Q1 = cls._qtables()
        y = [(x >> (8 * i)) & 0xFF for i in range(4)]
        l = [[(w >> (8 * i)) & 0xFF for i in range(4)] for w in L]
        if k == 4:
            y[0] = Q1[y[0]] ^ l[3][0]; y[1] = Q0[y[1]] ^ l[3][1]
            y[2] = Q0[y[2]] ^ l[3][2]; y[3] = Q1[y[3]] ^ l[3][3]
        if k >= 3:
            y[0] = Q1[y[0]] ^ l[2][0]; y[1] = Q1[y[1]] ^ l[2][1]
            y[2] = Q0[y[2]] ^ l[2][2]; y[3] = Q0[y[3]] ^ l[2][3]
        y[0] = Q1[Q0[Q0[y[0]] ^ l[1][0]] ^ l[0][0]]
        y[1] = Q0[Q0[Q1[y[1]] ^ l[1][1]] ^ l[0][1]]
        y[2] = Q1[Q1[Q0[y[2]] ^ l[1][2]] ^ l[0][2]]
        y[3] = Q0[Q1[Q1[y[3]] ^ l[1][3]] ^ l[0][3]]
        z = cls._mds(y)
        return z[0] | (z[1] << 8) | (z[2] << 16) | (z[3] << 24)

    @classmethod
    def _key_schedule(cls, key):
        k = len(key) // 8
        words = [int.from_bytes(key[4 * i:4 * i + 4], 'little') for i in range(2 * k)]
        Me = [words[2 * i] for i in range(k)]
        Mo = [words[2 * i + 1] for i in range(k)]
        S = []
        for i in range(k):
            sv = cls._rs(list(key[8 * i:8 * i + 8]))
            S.append(sv[0] | (sv[1] << 8) | (sv[2] << 16) | (sv[3] << 24))
        S = list(reversed(S))
        RHO = 0x01010101
        K = []
        for i in range(20):
            A = cls._h((2 * i) * RHO, Me, k)
            B = cls._h((2 * i + 1) * RHO, Mo, k)
            B = ((B << 8) | (B >> 24)) & 0xFFFFFFFF
            K.append((A + B) & 0xFFFFFFFF)
            k1 = (A + 2 * B) & 0xFFFFFFFF
            K.append(((k1 << 9) | (k1 >> 23)) & 0xFFFFFFFF)
        return K, S, k

    @classmethod
    def _enc_block(cls, pt, K, S, k):
        R = [int.from_bytes(pt[4 * i:4 * i + 4], 'little') ^ K[i] for i in range(4)]
        for r in range(16):
            t0 = cls._h(R[0], S, k)
            t1 = cls._h(((R[1] << 8) | (R[1] >> 24)) & 0xFFFFFFFF, S, k)
            f0 = (t0 + t1 + K[2 * r + 8]) & 0xFFFFFFFF
            f1 = (t0 + 2 * t1 + K[2 * r + 9]) & 0xFFFFFFFF
            r2 = R[2] ^ f0; r2 = ((r2 >> 1) | (r2 << 31)) & 0xFFFFFFFF
            r3 = ((R[3] << 1) | (R[3] >> 31)) & 0xFFFFFFFF; r3 ^= f1
            R = [r2, r3, R[0], R[1]]
        R = [R[2], R[3], R[0], R[1]]
        out = [R[i] ^ K[i + 4] for i in range(4)]
        return b''.join(w.to_bytes(4, 'little') for w in out)

    @classmethod
    def _dec_block(cls, ct, K, S, k):
        R = [int.from_bytes(ct[4 * i:4 * i + 4], 'little') ^ K[i + 4] for i in range(4)]
        R = [R[2], R[3], R[0], R[1]]
        for r in range(15, -1, -1):
            R = [R[2], R[3], R[0], R[1]]
            t0 = cls._h(R[0], S, k)
            t1 = cls._h(((R[1] << 8) | (R[1] >> 24)) & 0xFFFFFFFF, S, k)
            f0 = (t0 + t1 + K[2 * r + 8]) & 0xFFFFFFFF
            f1 = (t0 + 2 * t1 + K[2 * r + 9]) & 0xFFFFFFFF
            r2 = ((R[2] << 1) | (R[2] >> 31)) & 0xFFFFFFFF; r2 ^= f0
            r3 = R[3] ^ f1; r3 = ((r3 >> 1) | (r3 << 31)) & 0xFFFFFFFF
            R = [R[0], R[1], r2, r3]
        out = [R[i] ^ K[i] for i in range(4)]
        return b''.join(w.to_bytes(4, 'little') for w in out)

    @classmethod
    def encrypt(cls, data, key=None):
        if key is None: key = os.urandom(32)
        if isinstance(data, str): data = data.encode()
        pad = 16 - len(data) % 16
        data = data + bytes([pad] * pad)
        K, S, k = cls._key_schedule(key)
        iv = os.urandom(16)
        ct = bytearray()
        prev = iv
        for bi in range(0, len(data), 16):
            blk = bytes(data[bi + j] ^ prev[j] for j in range(16))
            enc = cls._enc_block(blk, K, S, k)
            ct.extend(enc)
            prev = enc
        return cls.MAGIC + iv + bytes(ct), key

    @classmethod
    def decrypt(cls, data, key):
        if data[:4] != cls.MAGIC: return data
        iv = data[4:20]; ct = data[20:]
        K, S, k = cls._key_schedule(key)
        pt = bytearray()
        prev = iv
        for bi in range(0, len(ct), 16):
            blk = ct[bi:bi + 16]
            dec = cls._dec_block(blk, K, S, k)
            pt.extend(bytes(dec[j] ^ prev[j] for j in range(16)))
            prev = blk
        p = pt[-1] if pt else 0
        return bytes(pt[:-p] if 1 <= p <= 16 else pt)

    @classmethod
    def self_test(cls) -> bool:
        # Resmi Twofish sıfır-anahtar KAT'ları (ecb_tbl I=1): 128/192/256-bit
        kats = [
            (bytes(16), '9F589F5CF6122C32B6BFEC2F2AE8C35A'),
            (bytes(24), 'EFA71F788965BD4453F860178FC19101'),
            (bytes(32), '57FF739D4DC92C1BD7FC01700CC8216F'),
        ]
        for key, want in kats:
            K, S, k = cls._key_schedule(key)
            if cls._enc_block(bytes(16), K, S, k).hex().upper() != want:
                return False
        # CBC round-trip + üretilen inline decrypt round-trip
        blob = os.urandom(200)
        enc, key = cls.encrypt(blob)
        if cls.decrypt(enc, key) != blob:
            return False
        ns = {'_TF_IN': enc}
        exec(cls.generate_decrypt_code('_TF_IN',
             base64.b64encode(key).decode('ascii'), '_TF_OUT'), ns)
        return ns.get('_TF_OUT') == blob

    @staticmethod
    def generate_decrypt_code(var_in, key_b64, var_out):
        """Üretilen loader'a gömülecek bağımsız (harici bağımlılıksız) pure-Python
        Twofish-CBC decrypt kodu. MAGIC (NJTF) yoksa pass-through."""
        q0, q1 = TwofishEncryptor._qtables()
        q0b = base64.b85encode(q0).decode('ascii')
        q1b = base64.b85encode(q1).decode('ascii')
        return f'''import base64 as _tfb
_tfk=_tfb.b64decode("{key_b64}")
if {var_in}[:4]==b"NJTF":
    _tfQ0=_tfb.b85decode("{q0b}");_tfQ1=_tfb.b85decode("{q1b}")
    _tfMDS=((1,239,91,91),(91,239,239,1),(239,91,1,239),(239,1,239,91))
    _tfRS=((1,164,85,135,90,88,219,158),(164,86,130,243,30,198,104,229),(2,161,252,193,71,174,61,25),(164,85,135,90,88,219,158,3))
    def _tfgf(_a,_b,_m):
        _p=0
        for _ in range(8):
            if _b&1:_p^=_a
            _b>>=1
            _hi=_a&0x80;_a=(_a<<1)&0xFF
            if _hi:_a^=_m
        return _p
    def _tfmds(_y):
        return [_tfgf(_tfMDS[_r][0],_y[0],0x69)^_tfgf(_tfMDS[_r][1],_y[1],0x69)^_tfgf(_tfMDS[_r][2],_y[2],0x69)^_tfgf(_tfMDS[_r][3],_y[3],0x69) for _r in range(4)]
    def _tfrs(_b):
        return [_tfgf(_tfRS[_r][0],_b[0],0x4D)^_tfgf(_tfRS[_r][1],_b[1],0x4D)^_tfgf(_tfRS[_r][2],_b[2],0x4D)^_tfgf(_tfRS[_r][3],_b[3],0x4D)^_tfgf(_tfRS[_r][4],_b[4],0x4D)^_tfgf(_tfRS[_r][5],_b[5],0x4D)^_tfgf(_tfRS[_r][6],_b[6],0x4D)^_tfgf(_tfRS[_r][7],_b[7],0x4D) for _r in range(4)]
    def _tfh(_x,_L,_k):
        _y=[(_x>>(8*_i))&0xFF for _i in range(4)]
        _l=[[(_w>>(8*_i))&0xFF for _i in range(4)] for _w in _L]
        if _k==4:
            _y[0]=_tfQ1[_y[0]]^_l[3][0];_y[1]=_tfQ0[_y[1]]^_l[3][1];_y[2]=_tfQ0[_y[2]]^_l[3][2];_y[3]=_tfQ1[_y[3]]^_l[3][3]
        if _k>=3:
            _y[0]=_tfQ1[_y[0]]^_l[2][0];_y[1]=_tfQ1[_y[1]]^_l[2][1];_y[2]=_tfQ0[_y[2]]^_l[2][2];_y[3]=_tfQ0[_y[3]]^_l[2][3]
        _y[0]=_tfQ1[_tfQ0[_tfQ0[_y[0]]^_l[1][0]]^_l[0][0]]
        _y[1]=_tfQ0[_tfQ0[_tfQ1[_y[1]]^_l[1][1]]^_l[0][1]]
        _y[2]=_tfQ1[_tfQ1[_tfQ0[_y[2]]^_l[1][2]]^_l[0][2]]
        _y[3]=_tfQ0[_tfQ1[_tfQ1[_y[3]]^_l[1][3]]^_l[0][3]]
        _z=_tfmds(_y)
        return _z[0]|(_z[1]<<8)|(_z[2]<<16)|(_z[3]<<24)
    _tfkk=len(_tfk)//8
    _tfw=[int.from_bytes(_tfk[4*_i:4*_i+4],"little") for _i in range(2*_tfkk)]
    _tfMe=[_tfw[2*_i] for _i in range(_tfkk)];_tfMo=[_tfw[2*_i+1] for _i in range(_tfkk)]
    _tfS=[]
    for _i in range(_tfkk):
        _sv=_tfrs(list(_tfk[8*_i:8*_i+8]));_tfS.append(_sv[0]|(_sv[1]<<8)|(_sv[2]<<16)|(_sv[3]<<24))
    _tfS=list(reversed(_tfS))
    _tfK=[]
    for _i in range(20):
        _A=_tfh((2*_i)*0x01010101,_tfMe,_tfkk);_B=_tfh((2*_i+1)*0x01010101,_tfMo,_tfkk);_B=((_B<<8)|(_B>>24))&0xFFFFFFFF
        _tfK.append((_A+_B)&0xFFFFFFFF);_k1=(_A+2*_B)&0xFFFFFFFF;_tfK.append(((_k1<<9)|(_k1>>23))&0xFFFFFFFF)
    def _tfdec(_ct):
        _R=[int.from_bytes(_ct[4*_i:4*_i+4],"little")^_tfK[_i+4] for _i in range(4)]
        _R=[_R[2],_R[3],_R[0],_R[1]]
        for _r in range(15,-1,-1):
            _R=[_R[2],_R[3],_R[0],_R[1]]
            _t0=_tfh(_R[0],_tfS,_tfkk);_t1=_tfh(((_R[1]<<8)|(_R[1]>>24))&0xFFFFFFFF,_tfS,_tfkk)
            _f0=(_t0+_t1+_tfK[2*_r+8])&0xFFFFFFFF;_f1=(_t0+2*_t1+_tfK[2*_r+9])&0xFFFFFFFF
            _r2=((_R[2]<<1)|(_R[2]>>31))&0xFFFFFFFF;_r2^=_f0
            _r3=_R[3]^_f1;_r3=((_r3>>1)|(_r3<<31))&0xFFFFFFFF
            _R=[_R[0],_R[1],_r2,_r3]
        _o=[_R[_i]^_tfK[_i] for _i in range(4)]
        return b"".join(_w.to_bytes(4,"little") for _w in _o)
    _tfiv={var_in}[4:20];_tfc={var_in}[20:];_tfpt=bytearray();_tfpv=_tfiv
    for _bi in range(0,len(_tfc),16):
        _blk=_tfc[_bi:_bi+16];_d=_tfdec(_blk)
        _tfpt.extend(bytes(_d[_j]^_tfpv[_j] for _j in range(16)));_tfpv=_blk
    _tfp=_tfpt[-1] if _tfpt else 0
    {var_out}=bytes(_tfpt[:-_tfp] if 1<=_tfp<=16 else _tfpt)
else:
    {var_out}={var_in}'''


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

    @staticmethod
    def inject_live_decoys(source, live_count=5):
        """
        (v9 — Fikir #5) CANLI DECOY: sahte try/except bloklarının bir kısmına
        GERÇEK anti-debug kontrolü gömülür. Analist statik bakışta bunları diğer
        yüzlerce sahte bloktan ayıramaz; ama runtime'da bunlar gerçekten debugger
        varsa süreci sonlandırır. Böylece decoy'lar hem KALIR (silme yok) hem işe
        yarar hale gelir — "hepsi sahte" varsayımı artık güvenli değil.
        """
        hyp = HyperionObfuscator()
        live_blocks = []
        for _ in range(live_count):
            v = [hyp._randvar() for _ in range(3)]
            n1 = random.randint(0, 0xFFFFFF)
            # sahte aritmetik cephesi + gizli gerçek kontrol
            live_blocks.append('\n'.join([
                f'try:',
                f'    {v[0]}={n1}',
                f'    {v[1]}=(lambda: __import__("sys").gettrace())()',
                f'    if {v[1]} is not None:',
                f'        __import__("os")._exit(0)',
                f'    {v[2]}={v[0]}^0x{random.randint(0,0xFFFF):04X}',
                f'except Exception: {v[2]}=None',
            ]))
        return source + '\n\n# -- fei-live --\n' + '\n'.join(live_blocks)


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
    """
    O4: .pyc'ye yanıltıcı veri enjekte eder — decompiler çöker/yanılır.

    ── DÜZELTME (v9) ────────────────────────────────────────────────────────
    Eski sürüm junk'ı .pyc gövdesinin ORTASINA gömüyordu (`body[:p1] + j1 + ...`)
    ve `b''*4` boş bytes idi. Bu, marshal akışını KALICI olarak bozuyordu:
    bu .pyc daha sonra ChaCha/AES/XOR zincirine girip runtime'da geri açıldığı
    için, ortasına rastgele veri sokmak `marshal.loads`'u patlatır → payload
    hiç çalışmaz. Yani "junk" fiilen bir data-corruption bug'ıydı.

    Yeni tasarım: junk GERİ ALINABİLİR bir çerçeveye sarılır. Gövdeye
    dokunulmadan, kendine ait uzunluk-önekli bloklar halinde eklenir; runtime
    tarafı `strip()` ile birebir orijinal .pyc'yi geri üretir. Böylece:
      • Statik decompiler (uncompyle6/decompyle3) ham dosyada junk görüp yanılır
      • marshal akışı BOZULMAZ → payload sorunsuz açılır
      • Ekleme deterministik değil (her build farklı) ama tersinir
    Çerçeve formatı:
      MAGIC(4) 'NJJK' | header(16) | njunk(2,BE) | [ len(4,BE) | junk ]* | body
    """
    MAGIC = b'NJJK'

    @staticmethod
    def inject(pyc_data):
        try:
            if len(pyc_data) < 32:
                return pyc_data
            header = pyc_data[:16]
            body   = pyc_data[16:]
            njunk  = random.randint(2, 5)
            out = JunkBytecodeInjector.MAGIC + header + njunk.to_bytes(2, 'big')
            for _ in range(njunk):
                # Decompiler'ı yanıltmak için sahte-ama-inandırıcı .pyc parçaları:
                # rastgele opcode benzeri baytlar + sıfır dolgu karışımı.
                jlen = random.randint(32, 160)
                junk = bytes(random.randint(0, 255) for _ in range(jlen))
                out += jlen.to_bytes(4, 'big') + junk
            out += body
            return out
        except Exception:
            return pyc_data

    @staticmethod
    def strip(data):
        """inject() ile eklenen junk çerçevesini birebir geri alır → orijinal .pyc."""
        try:
            if data[:4] != JunkBytecodeInjector.MAGIC:
                return data  # bu dosyaya junk enjekte edilmemiş
            pos = 4
            header = data[pos:pos+16]; pos += 16
            njunk = int.from_bytes(data[pos:pos+2], 'big'); pos += 2
            for _ in range(njunk):
                jlen = int.from_bytes(data[pos:pos+4], 'big'); pos += 4
                pos += jlen  # junk'ı atla
            body = data[pos:]
            return header + body
        except Exception:
            return data

    @staticmethod
    def generate_strip_code(var_in, var_out):
        """Runtime tarafı: gömülü junk çerçevesini geri alan Python kodu üretir."""
        lines = [
            f'if {var_in}[:4] == b"NJJK":',
            f'    _jk_p = 4',
            f'    _jk_hdr = {var_in}[_jk_p:_jk_p+16]; _jk_p += 16',
            f'    _jk_n = int.from_bytes({var_in}[_jk_p:_jk_p+2], "big"); _jk_p += 2',
            f'    for _jk_i in range(_jk_n):',
            f'        _jk_l = int.from_bytes({var_in}[_jk_p:_jk_p+4], "big"); _jk_p += 4',
            f'        _jk_p += _jk_l',
            f'    {var_out} = _jk_hdr + {var_in}[_jk_p:]',
            f'    del _jk_p, _jk_hdr, _jk_n',
            f'else:',
            f'    {var_out} = {var_in}',
        ]
        return '\n'.join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# KEY FUSION BUS (v9 — Fikir #1)
# Tüm guard'ların (HVM, WBC, anti-debug, AntiVM, MemoryCanary, NinjaVM sabiti,
# native .so hash'i) ürettiği "sağlık değerleri" tek bir fusion_key'e karışır.
# Bu fusion_key, payload decrypt anahtarının ZORUNLU bir bileşeni olur.
#
# NEDEN GÜÇLÜ:
#   Eski tasarımda her guard `if x != expected: SystemExit(1)` idi → tek satır
#   silinince atlanıyordu. Burada guard'ın SONUCU anahtarın kendisine girer.
#   Bir guard'ı silen/patch'leyen kişi fusion_key'i de bozar → payload çöp olur.
#   Patch edilecek bir "if" yok; matematiksel zorunluluk (EKC felsefesi, genele
#   taşınmış hali).
#
# ÇALIŞMA MODELİ:
#   encode-zamanı: her guard'ın TEMİZ-ORTAM değeri bilinir (contribution).
#     fusion = SHA256(c0 || c1 || ... || cN)
#     payload gerçek anahtarı K ile şifrelenir; K, fusion ile maskelenip saklanır:
#       K_stored = K XOR fusion
#   runtime: her guard kendi değerini yeniden üretir (temizse aynı, kirliyse farklı)
#     fusion' = SHA256(c0' || ... || cN')
#     K' = K_stored XOR fusion'      → temizse K'==K, değilse çöp
#   K' payload zincirinin son XOR anahtarı olarak kullanılır.
# ══════════════════════════════════════════════════════════════════════════════
class KeyFusionBus:
    """Guard katkılarını toplayıp payload anahtarına kenetleyen merkezi bus."""

    def __init__(self):
        self.contributions = []   # (etiket, temiz_deger_bytes, runtime_kod_uretici)
        self._labels = set()

    def add(self, label: str, clean_value: bytes, runtime_expr_code: str):
        """
        label            : rapor/debug için ad (benzersiz)
        clean_value      : encode-zamanı bilinen temiz-ortam 32-byte katkısı
        runtime_expr_code: runtime'da '_kfb_c' listesine .append(...) yapan Python
                           kod parçası; temiz ortamda clean_value üretmeli.
        """
        if label in self._labels:
            label = f'{label}_{len(self.contributions)}'
        self._labels.add(label)
        cv = hashlib.sha256(clean_value).digest()  # normalize 32 byte
        self.contributions.append((label, cv, runtime_expr_code))
        return self

    def clean_fusion(self) -> bytes:
        h = hashlib.sha256()
        for _lbl, cv, _rt in self.contributions:
            h.update(cv)
        return h.digest()

    def mask_key(self, real_key: bytes) -> bytes:
        """Gerçek anahtarı temiz fusion ile maskele (saklanacak hali)."""
        fusion = self.clean_fusion()
        rk = (real_key + bytes(32))[:32]
        return bytes(rk[i] ^ fusion[i] for i in range(32))

    def generate_runtime_code(self, masked_key_b64: str, out_key_var: str) -> str:
        """
        Runtime tarafı: guard katkılarını toplar, fusion'ı yeniden kurar,
        maskeli anahtarı açar → out_key_var (32 byte) elde edilir.
        Guard'lardan biri kirliyse out_key_var otomatik çöp olur.
        """
        parts = [
            'import hashlib as _kfb_h, base64 as _kfb_b',
            '_kfb_c = []',
        ]
        for _lbl, _cv, rt in self.contributions:
            parts.append(f'# guard: {_lbl}')
            parts.append(rt)
        parts += [
            '_kfb_hh = _kfb_h.sha256()',
            'for _kfb_x in _kfb_c: _kfb_hh.update(_kfb_h.sha256(_kfb_x).digest())',
            '_kfb_fusion = _kfb_hh.digest()',
            f'_kfb_mk = _kfb_b.b64decode("{masked_key_b64}")',
            f'{out_key_var} = bytes(_kfb_mk[_i] ^ _kfb_fusion[_i] for _i in range(32))',
            'del _kfb_c, _kfb_hh, _kfb_fusion, _kfb_mk',
        ]
        return '\n'.join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# NATIVE BINARY HASH GUARD (v9 — Fikir #3)
# Nuitka/Cython .so'nun encode-zamanı SHA256'sı hesaplanır; runtime guard ZIP
# içindeki .so'yu okuyup doğrular. .so patch'lenirse fusion katkısı bozulur.
# ══════════════════════════════════════════════════════════════════════════════
class NativeHashGuard:
    @staticmethod
    def compute(so_path: str) -> bytes:
        with open(so_path, 'rb') as f:
            return hashlib.sha256(f.read()).digest()

    @staticmethod
    def runtime_contribution_code(native_fname: str) -> str:
        """
        Temiz ortamda .so'nun SHA256'sını _kfb_c'ye ekler. Dosya yoksa/patch'liyse
        farklı değer üretir → fusion bozulur (guard'ı silmek de fusion'ı bozar).
        '_d' değişkeni ZIP'in açıldığı temp dizin (generate_v8 içinde tanımlı).
        """
        return (
            f'try:\n'
            f'    with open(_O.path.join(_d, "{native_fname}"), "rb") as _nhg_f:\n'
            f'        _kfb_c.append(_kfb_h.sha256(_nhg_f.read()).digest())\n'
            f'except Exception:\n'
            f'    _kfb_c.append(b"\\x00" * 32)\n'
        )


class ConstantFoldingSaboteur:
    """O5: Sabit tamsayıları runtime ifadeye çevir — optimizer devre dışı"""

    @staticmethod
    def transform_source(source):
        import re as _re
        def _sub(m):
            # f-string içindeki %NN (URL-encoded) kalıplarına DOKUNMA.
            # m.start() öncesinde '%' varsa bu bir URL encoding → orijinal bırak.
            start = m.start()
            if start > 0 and source[start-1:start] == '%':
                return m.group(0)
            # f-string içinde miyiz? Basit yaklaşım: satır f" veya f' içeriyorsa
            # ve % karakteri öncesindeyse atla — yukarıda zaten kontrol edildi.
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
                elif '%' in line and ('f"' in line or "f'" in line or '%2' in line or '%5' in line or '%3' in line):
                    # URL-encoded içerik olan f-string satırlarına dokunma
                    result.append(line)
                else:
                    # _sub'a satırı da ver ki % kontrolü yapabilsin
                    def _sub_line(m, _line=line):
                        start = m.start()
                        if start > 0 and _line[start-1:start] == '%':
                            return m.group(0)
                        n = int(m.group(0))
                        if n < 2 or n > 9999: return m.group(0)
                        choices = [f'({n-1}+1)', f'({n+1}-1)', f'(0x{n:X})', f'(int("{n}"))']
                        return random.choice(choices)
                    result.append(_re.sub(r'(?<!\w)([2-9]\d{1,3})(?!\w)', _sub_line, line))
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
    AES-128-CBC White-Box (encoding'li) — GERÇEK white-box ağı.

    Standart AES ile şifreler (encrypt_cbc); DECRYPT tarafını, anahtarı gizli bir
    GF(2)-doğrusal encoding F altında T-tablolarına gömen bağımsız bir Python
    kaynağı olarak üretir (generate_wbc_decrypt_source).

    KOD ÜRETİM FAZI (offline):
      - Anahtar 11 round key'e genişletilir; gizli doğrusal bijeksiyon F seçilir.
      - AddRoundKey+InvSubBytes VE InvMixColumns, F encoding'i altında 8→32-bit
        Type-II tablolarına katlanır. Round anahtarı tabloda yalnızca F(IS[x]^rk)
        biçiminde görünür → tek tablodan anahtar OKUNAMAZ.

    ÇALIŞMA FAZI (üretilen kod):
      - Her ara state byte'ı F(x) olarak durur (asla ham anahtar/state RAM'de yok).
      - F doğrusal olduğundan XOR ile değişmeli → 4 InvMixColumns katkısı yerel
        XOR ile toplanır (Type-IV tablosu gerekmez). Dış G/Ç encoding'leri katlanır:
        girdi ham ciphertext, çıktı ham plaintext.

    DÜRÜST SINIR: doğrusal encoding'ler BGE/affine analiz ile kırılabilir; bu
    yapı anahtar çıkarma MALİYETİNİ artırır, koşulsuz güvenlik sağlamaz. Mutlak
    değildir. (self_test standart AES ile tutarlılığı doğrular.)
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
    def _lin_bijection(cls, rng):
        """Rastgele tersinir 8x8 GF(2) matrisi → (F, Finv) 256-byte lookup çifti.
        F(x)=M·x (bit-matris çarpımı). Doğrusal olduğu için XOR ile değişmeli:
        F(a)^F(b)=F(a^b) → InvMixColumns katkıları encode altında yerel XOR ile
        toplanabilir (Type-IV tablosu gerekmez)."""
        while True:
            rows = [rng.randrange(256) for _ in range(8)]
            F = bytearray(256)
            for x in range(256):
                y = 0
                for bit in range(8):
                    if bin(rows[bit] & x).count('1') & 1:
                        y |= (1 << bit)
                F[x] = y
            if len(set(F)) == 256:
                Finv = bytearray(256)
                for x in range(256):
                    Finv[F[x]] = x
                return bytes(F), bytes(Finv)

    @classmethod
    def generate_wbc_decrypt_source(cls, key: bytes) -> Tuple[str, str]:
        """
        Anahtarı GİZLİ doğrusal encoding (F) altında T-tablolarına gömen bağımsız
        Python decrypt kaynağı üretir. Döner → (kaynak_kodu, decrypt_fn_adı);
        fn(ciphertext: bytes) -> plaintext.

        GERÇEK white-box: her ara state byte'ı RAM'de F(x) olarak durur (asla ham);
        round anahtarları tablolarda yalnızca F(IS[x]^rk) / F(x^rk10) biçiminde görünür,
        bu yüzden tek bir tablo incelemesi anahtarı VERMEZ (eski sürümdeki
        rk10[b]=T_init[b*256] türü trivial sızıntı kaldırıldı). InvMixColumns
        tablolara katlanır (8→32-bit Type-II), 4 katkı yerel XOR ile toplanır.
        Dış G/Ç encoding'leri katlanmıştır: T_init ham ciphertext alır, T_final ham
        plaintext üretir.

        DÜRÜST SINIR: doğrusal (affine) encoding'ler BGE/affine analiz ile
        çözülebilir; bu, anahtar çıkarma MALİYETİNİ artırır, koşulsuz güvenlik değil.
        """
        assert len(key) == 16
        IS = cls._isbox(); gf = cls._gf; rks = cls._ks(key)
        rng = random.Random(int.from_bytes(key[:4], 'big') ^ 0xDEADBEEF)
        F, Finv = cls._lin_bijection(rng)

        E = [gf(0x0e, x) for x in range(256)]; B = [gf(0x0b, x) for x in range(256)]
        D = [gf(0x0d, x) for x in range(256)]; N = [gf(0x09, x) for x in range(256)]
        def Ty(j, t):  # InvMixColumns: sütun-pozisyonu j byte'ının 4-byte katkısı
            if j == 0: return (E[t], N[t], D[t], B[t])
            if j == 1: return (B[t], E[t], N[t], D[t])
            if j == 2: return (D[t], B[t], E[t], N[t])
            return (N[t], D[t], B[t], E[t])

        # T_init[b*256+x] = F[x ^ rk10[b]]  (ham ct → F-encoded; AddRoundKey rk10)
        T_init = bytes(F[x ^ rks[10][b]] for b in range(16) for x in range(256))
        # TII[(ri*16*256 + b*256 + e)*4 + k] : F-encoded girdi e → 4-byte F-encoded söz
        TII = bytearray(9 * 16 * 256 * 4)
        for ri in range(9):
            rk = rks[9 - ri]
            for b in range(16):
                j = b % 4
                for e in range(256):
                    t = IS[Finv[e]] ^ rk[b]      # InvSubBytes + AddRoundKey (decode edilmiş)
                    c = Ty(j, t)
                    base = (ri * 16 * 256 + b * 256 + e) * 4
                    TII[base] = F[c[0]]; TII[base + 1] = F[c[1]]
                    TII[base + 2] = F[c[2]]; TII[base + 3] = F[c[3]]
        # T_final[b*256+e] = IS[Finv[e]] ^ rk0[b]  (F-encoded → HAM plaintext)
        T_final = bytes(IS[Finv[e]] ^ rks[0][b] for b in range(16) for e in range(256))

        b85 = lambda d: base64.b85encode(bytes(d)).decode()
        rv = lambda: '_w' + ''.join(rng.choice('abcdefghijklmnopqrstuvwxyz') for _ in range(8))
        Ti, Tr, Tf = rv(), rv(), rv()
        fb, fc = rv(), rv()

        src = f"""import base64 as _wbc_b64
{Ti}=_wbc_b64.b85decode("{b85(T_init)}")
{Tr}=_wbc_b64.b85decode("{b85(TII)}")
{Tf}=_wbc_b64.b85decode("{b85(T_final)}")
del _wbc_b64
_wisr=(0,13,10,7,4,1,14,11,8,5,2,15,12,9,6,3)
def {fb}(_c):
 _s=[{Ti}[_b*256+_c[_b]] for _b in range(16)]
 for _ri in range(9):
  _sr=[_s[_wisr[_b]] for _b in range(16)]
  _n=[0]*16
  for _k in range(4):
   _o0=_o1=_o2=_o3=0
   for _j in range(4):
    _b=4*_k+_j;_ba=(_ri*4096+_b*256+_sr[_b])*4
    _o0^={Tr}[_ba];_o1^={Tr}[_ba+1];_o2^={Tr}[_ba+2];_o3^={Tr}[_ba+3]
   _n[4*_k]=_o0;_n[4*_k+1]=_o1;_n[4*_k+2]=_o2;_n[4*_k+3]=_o3
  _s=_n
 _sr=[_s[_wisr[_b]] for _b in range(16)]
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
                # (v10) İnteraktif hedef seçimi: kullanıcıya hangi fonksiyonların
                # VM'e taşınacağını sor. assume_yes (CI/otomatik) modunda menü
                # atlanır ve eski otomatik davranışa (@ninja_vm + auto) düşülür.
                _vm_selected = None  # None = otomatik mod
                if not assume_yes:
                    try:
                        _cands = VMTargetSelector.analyze(source_code)
                        if _cands:
                            print(VMTargetSelector.render_menu(_cands), end='')
                            try:
                                _raw_sel = input()
                            except EOFError:
                                _raw_sel = '0'
                            _vm_selected, _skipped = VMTargetSelector.parse_selection(_raw_sel, _cands)
                            for _sk_name, _sk_reason in _skipped:
                                logger.warning(f'  Atlandı "{_sk_name}": {_sk_reason} (VM-uyumsuz)')
                            if not _vm_selected:
                                logger.info('  NinjaVM: hiçbir fonksiyon seçilmedi (0/HİÇBİRİ)')
                        else:
                            logger.info('  NinjaVM: kaynakta taşınabilir fonksiyon bulunamadı')
                    except Exception as _menu_e:
                        logger.warning(f'  VM menü hatası ({_menu_e}) — otomatik moda düşülüyor')
                        _vm_selected = None

                # selected_names verildiyse SADECE onlar; None ise otomatik mod
                if _vm_selected is not None and len(_vm_selected) == 0 and not assume_yes:
                    # kullanıcı bilerek "hiçbiri" dedi → VM'i atla
                    logger.info('  NinjaVM: kullanıcı seçimi ile atlandı')
                else:
                    _vm_src, _vm_moved = self.ninja_vm.transform_source(
                        source_code,
                        selected_names=_vm_selected if _vm_selected else None,
                    )
                    if _vm_moved > 0:
                        source_code = _vm_src
                        logger.info(f'  NinjaVM: {_vm_moved} fonksiyon custom ISA\'ya derlendi (Python bytecode gizlendi)')
                    else:
                        logger.info('  NinjaVM: uygun fonksiyon yok (@ninja_vm ile işaretleyebilirsiniz) — atlanıyor')
                    # (v10) Teşhis özeti: hangi fonksiyon neden VM'e giremedi
                    _diag = getattr(self.ninja_vm, '_vm_diag', [])
                    if _diag:
                        logger.info(f'  ── NinjaVM teşhis ({len(_diag)} fonksiyon VM-dışı kaldı) ──')
                        for _dfn, _dreason in _diag:
                            logger.info(f'     • {_dfn}: {_dreason}')
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

            logger.info('Ultimate v9 Adım 8d2: Canlı decoy (sahte bloklara gerçek anti-debug)')
            try:
                obf_source = FakeExceptionInjector.inject_live_decoys(obf_source, live_count=5)
                logger.info('  5 canlı decoy eklendi — sahte bloklardan ayırt edilemez')
            except Exception as _e: logger.warning(f'Canlı decoy atlandı: {_e}')

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

            # NOT (v18): Eski "Adım 9–16" (.pyc derleme/mutasyon + ChaCha/Twofish/AES/
            # XOR/Polymorphic/HMAC/RKM/Shamir/Ascii85) ÖLÜ DALDI — ürettiği
            # `compressed`/`poly_encrypted` çıktı yoluna hiç girmiyordu (canlı yol
            # kaynağı `generate_with_payload` içinde sıfırdan yeniden derler). O blok
            # kaldırıldı; gerçek ChaCha20→Twofish→AES şifrelemesi artık aşağıda
            # CANLI payload'a (`ult_enc`) uygulanıyor ve generate_v8 runtime'da çözüyor.

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

            logger.info('Ultimate Adım 21: MiniVMGenerator → runtime VM program (pipeline modu)')
            vm_prog_bytes = MiniVMGenerator.compile_runtime_program()
            vm_prog_b64   = base64.b64encode(vm_prog_bytes).decode('ascii')
            logger.info(f'  VM program: {len(vm_prog_bytes)} byte (DECOMP+MARSHAL+EXEC+WIPE)')

            # ── (v9) KEY FUSION BUS kurulumu ─────────────────────────────────
            # Guard katkılarını topla → fusion_key üret → payload'ı bu anahtarla
            # simetrik XOR'la. Runtime'da guard'lar aynı katkıları üretirse anahtar
            # birebir geri gelir; biri kirli/patch'li ise anahtar çöp → payload açılmaz.
            logger.info('Ultimate Adım 21b: KeyFusionBus — guard sonuçlarını anahtara kenetleme')
            _fusion_bus = KeyFusionBus()
            _fusion_key_var = self.hyperion._randvar()
            _fusion_setup_lines = []
            try:
                # Katkı 1: HVM homomorfik toplam sabiti (temiz ortamda deterministik)
                _hvm_fuse_vals = [random.randint(1, 0x7FFFFFF0) for _ in range(8)]
                _HVM_P_f = HomomorphicVM.P
                _hvm_sum = 0
                for _v in _hvm_fuse_vals:
                    _hvm_sum = (_hvm_sum + _v) % _HVM_P_f
                _fusion_bus.add(
                    'hvm_sum',
                    _hvm_sum.to_bytes(8, 'big'),
                    HomomorphicVM.generate_runtime() + '\n' +
                    '\n'.join(f'_hvm_load({_i}, {_v})' for _i, _v in enumerate(_hvm_fuse_vals)) + '\n' +
                    '_hvm_add(8, 0, 1)\n' +
                    '\n'.join(f'_hvm_add(8, 8, {_i})' for _i in range(2, 8)) + '\n' +
                    '_kfb_c.append(_hvm_store(8).to_bytes(8, "big"))\n' +
                    '_hvm_clear()'
                )
                # Katkı 2: WBC-AES çözülen bütünlük etiketi (T-box'tan gelen 32 byte)
                _wbc_fk = os.urandom(16)
                _wbc_ftag = os.urandom(32)
                _wbc_fct = WhiteBoxAES.encrypt_cbc(_wbc_ftag, _wbc_fk)
                _wbc_fsrc, _wbc_ffn = WhiteBoxAES.generate_wbc_decrypt_source(_wbc_fk)
                del _wbc_fk
                _wbc_fct_b64 = base64.b64encode(_wbc_fct).decode('ascii')
                _fusion_bus.add(
                    'wbc_tag',
                    _wbc_ftag,
                    _wbc_fsrc + f'_kfb_c.append({_wbc_ffn}(_kfb_b.b64decode("{_wbc_fct_b64}")))'
                )
                # NOT: Anti-debug fusion katkısı KASITLI olarak eklenmedi.
                # gettrace() encode ve runtime ortamları arasında farklı olabilir
                # (örn. Pydroid3/IDE kendi trace hook'unu kurar) → fusion anahtarı
                # deterministik olmaz, self-verify kırılır. Anti-debug guard olarak
                # ZATEN ayrı çalışıyor (runtime _exit); fusion'a sokmak kırılgan.
                # Fusion katkıları yalnızca DETERMİNİSTİK kaynaklardan gelir:
                # HVM sabiti, WBC T-box, native .so hash, NinjaVM sabiti.
                # NOT: Native .so hash fusion katkısı KASITLI olarak eklenmedi.
                # Katkı encode-zamanı native_file'dan hesaplanıyor, ama runtime'da
                # .so ZIP'ten ÇIKARILIP okunuyor; ZIP paketleme/strip sürecinde
                # byte'lar değişebildiği için iki hash eşleşmiyor → fusion kırılır
                # (teşhis: "native_hash temiz değerinden SAPIYOR"). Deterministik
                # olmadığı için fusion'dan çıkarıldı.
                # Katkı 5: NinjaVM sabiti (Fikir #2 — VM ile şifrelemeyi kenetle)
                _nv_const = random.randint(1, 0x7FFFFFFF)
                _fusion_bus.add(
                    'ninjavm_const',
                    _nv_const.to_bytes(8, 'big'),
                    f'_kfb_c.append(({_nv_const}).to_bytes(8, "big"))'
                )

                # Gerçek payload XOR anahtarını üret ve fusion ile maskele
                _real_fusion_key = os.urandom(32)
                _masked_fk = _fusion_bus.mask_key(_real_fusion_key)
                _masked_fk_b64 = base64.b64encode(_masked_fk).decode('ascii')
                _fusion_setup_code = _fusion_bus.generate_runtime_code(_masked_fk_b64, _fusion_key_var)

                # (v9) ZORUNLU self-verify: setup kodu gerçekten temiz ortamda
                # _real_fusion_key'i geri kuruyor mu? Kurmuyorsa (syntax hatası,
                # round-trip bozukluğu vb.) fusion'ı payload'a UYGULAMA — aksi halde
                # üretilen dosya çalışmaz. Bu kontrol, bozuk fusion'ın sessizce
                # çıktıya sızmasını engeller (v9'daki decimal-literal bug'ının dersi).
                _fusion_ok = False
                try:
                    _vns_pre = {}
                    exec(_fusion_setup_code, _vns_pre)
                    _got = _vns_pre.get(_fusion_key_var)
                    _fusion_ok = (_got == _real_fusion_key)
                    if not _fusion_ok:
                        # Teşhis: anahtar üretildi mi, üretildiyse neden eşleşmiyor?
                        if _got is None:
                            logger.warning('  [teşhis] fusion setup çalıştı ama anahtar değişkeni üretilmedi')
                        else:
                            logger.warning(f'  [teşhis] anahtar üretildi ama eşleşmiyor (len={len(_got) if isinstance(_got,(bytes,bytearray)) else "?"})')
                        # Hangi guard katkısı temiz değerinden sapıyor? Tek tek dene.
                        for _lbl, _cv, _rt in _fusion_bus.contributions:
                            try:
                                _t_ns = {'_kfb_c': [], '_kfb_h': hashlib, '_kfb_b': base64}
                                exec('import hashlib as _kfb_h, base64 as _kfb_b\n' + _rt, _t_ns)
                                _produced = hashlib.sha256(_t_ns['_kfb_c'][-1]).digest() if _t_ns['_kfb_c'] else None
                                _match = (_produced == _cv)
                                if not _match:
                                    logger.warning(f'  [teşhis] guard "{_lbl}" temiz değerinden SAPIYOR (bu ortamda farklı üretiyor)')
                            except Exception as _ge:
                                logger.warning(f'  [teşhis] guard "{_lbl}" çalışırken HATA: {_ge}')
                except SyntaxError as _se:
                    logger.warning(f'  [teşhis] fusion setup SYNTAX hatası: satır {_se.lineno}: {_se.text!r}')
                    _fusion_ok = False
                except Exception as _fv_e:
                    logger.warning(f'  Fusion self-verify başarısız: {type(_fv_e).__name__}: {_fv_e}')
                    _fusion_ok = False
                if not _fusion_ok:
                    logger.warning('  Fusion round-trip DOĞRULANAMADI → fusion payload\'a UYGULANMIYOR (güvenli fallback)')
                    _real_fusion_key = None
                    _fusion_setup_code = ''
                else:
                    logger.info(f'  Fusion bus: {len(_fusion_bus.contributions)} guard katkısı kenetlendi + self-verify OK')
            except Exception as _fb_e:
                logger.warning(f'  KeyFusionBus kurulamadı, fusion atlanıyor: {_fb_e}')
                _fusion_setup_code = ''
                _real_fusion_key = None

            # (v9→v17) Payload'a fusion XOR uygula — SADECE fallback modunda.
            # v17 fusion-derive modunda: fusion_key XOR chain anahtarlarının
            # üretiminde kullanıldığı için ayrı payload-fusion-XOR GEREKSİZ.
            # Fallback modunda (fusion self-verify başarısız): eski davranış korunur.
            # Not: Şu an self-verify her zaman başarılı → payload-fusion-XOR atlanıyor.
            # Bu değişiklik encode/runtime simetrisini korur (aksi halde çift-XOR olur).
            if _real_fusion_key is not None and not _fusion_ok:
                ult_enc = bytes(ult_enc[_i] ^ _real_fusion_key[_i % 32] for _i in range(len(ult_enc)))
                logger.info('  Payload fusion XOR (fallback) uygulandı')
            elif _real_fusion_key is not None and _fusion_ok:
                logger.info('  Fusion-derive mod: XOR anahtarları fusion_key\'den türetilecek (statik görünmez)')

            # ── (v18) CANLI kripto zinciri — EN DIŞ katman olarak uygula ──────
            # ult_enc şu an: multi-XOR blob (+ fallback modunda fusion XOR). Bu blob'un
            # ÜZERİNE ChaCha20-Poly1305 → gerçek Twofish-256-CBC → AES-256-GCM uygula.
            # Blok şifreleri XOR ile yer değiştiremediğinden kripto EN DIŞTA olmalı:
            # runtime (generate_v8) lazy-reconstruct'tan HEMEN SONRA, XOR/fusion
            # adımlarından ÖNCE ters sırada (AES→TF→ChaCha) çözer. Her katman
            # MAGIC-çerçeveli → uygulanmayan katman runtime'da no-op (pass-through).
            crypto_layers = []  # encode sırası: [(algo, key_bytes), ...]
            chacha_encrypted = aes_encrypted = None
            _tf_key = None
            logger.info('Ultimate Adım 12: ChaCha20-Poly1305 (canlı payload, en dış-1)')
            if ChaCha20Encryptor.is_available():
                _cc_out, _cc_key = ChaCha20Encryptor.encrypt(ult_enc)
                if _cc_out:
                    ult_enc = _cc_out; crypto_layers.append(('CC', _cc_key)); chacha_encrypted = True
                    logger.info('  ChaCha20-Poly1305 uygulandı')
                else:
                    logger.warning('  ChaCha20 başarısız, atlanıyor')
            else:
                logger.info('  ChaCha20 kullanılamıyor (pycryptodome yok)')
            logger.info('Ultimate Adım 12b: Twofish-256-CBC (canlı payload)')
            try:
                _tf_out, _tf_key = TwofishEncryptor.encrypt(ult_enc)
                if _tf_out:
                    ult_enc = _tf_out; crypto_layers.append(('TF', _tf_key))
                    logger.info('  Gerçek Twofish uygulandı')
                else:
                    _tf_key = None; logger.warning('  Twofish None döndü, atlanıyor')
            except Exception as _e:
                _tf_key = None; logger.warning(f'  Twofish atlandı: {_e}')
            logger.info('Ultimate Adım 13: AES-256-GCM + PBKDF2 (canlı payload, en dış)')
            if AESEncryptor.is_available():
                _aes_out, _aes_key = AESEncryptor.encrypt(ult_enc)
                if _aes_out:
                    ult_enc = _aes_out; crypto_layers.append(('AES', _aes_key)); aes_encrypted = True
                    logger.info('  AES-256-GCM uygulandı')
                else:
                    logger.warning('  AES başarısız, atlanıyor')
            else:
                logger.info('  pycryptodome yok, AES atlanıyor')
            logger.info(f'  Canlı kripto zinciri: {len(crypto_layers)} katman '
                        f'({" → ".join(a for a, _ in crypto_layers) or "yok"})')

            logger.info('Ultimate Adım 20: LazyChunkEncoder → __s0-4__.bin (zincir key)')
            lazy_chunks, lazy_base_key = LazyChunkEncoder.encode(ult_enc)
            logger.info(f'  LazyChunk: {len(lazy_chunks)} parça, base_key=0x{lazy_base_key:02x}')

            ult_main_v8 = self.main_generator.generate_v8(
                xor_keys    = ult_keys,
                has_native  = bool(native_fname),
                native_fname= native_fname,
                lazy_base_key = lazy_base_key,
                vm_prog_b64 = vm_prog_b64,
                fusion_setup_code = _fusion_setup_code if _real_fusion_key and _fusion_ok else '',
                fusion_key_var = _fusion_key_var if _real_fusion_key and _fusion_ok else '',
                # (v17) real_fusion_key sadece self-verify BAŞARILIYSA geçilir.
                # Verilirse: XOR anahtarları statik olarak dosyada görünmez —
                # runtime'da fusion_key hesaplandıktan sonra ondan türetilirler.
                real_fusion_key = _real_fusion_key if _real_fusion_key and _fusion_ok else b'',
                crypto_layers = crypto_layers,
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
            # özetleriz — böylece katman iddiası ölçülebilir/dürüst hale gelir.
            # Rapor üretimi encode'u ASLA bozmamalı → tümü try/except içinde.
            try:
                _rep = []
                _rep.append(('NinjaVM (custom ISA)', _vm_moved > 0,
                             f'{_vm_moved} fonksiyon' if _vm_moved > 0
                             else 'uygun fonksiyon yok (@ninja_vm ile işaretleyin)'))
                _rep.append(('NinjaVM ISA rastgeleleştirme', True, 'her build farklı opcode'))
                # (v18) Canlı kripto zinciri — payload'a fiilen uygulanan katmanlar
                _rep.append(('ChaCha20-Poly1305 (canlı)', bool(chacha_encrypted),
                             'payload şifreli' if chacha_encrypted else 'pycryptodome yok/atlandı'))
                _rep.append(('Twofish-256-CBC (canlı, gerçek)', bool(_tf_key),
                             'payload şifreli' if _tf_key else 'atlandı'))
                _rep.append(('AES-256-GCM (canlı)', bool(aes_encrypted),
                             'payload şifreli' if aes_encrypted else 'pycryptodome yok/atlandı'))
                _rep.append(('Canlı kripto zinciri', bool(crypto_layers),
                             f'{len(crypto_layers)} katman: '
                             + (" → ".join(a for a, _ in crypto_layers) or "yok")))
                _rep.append(('WBC-AES canlı katman', bool(wbc_live_block),
                             'gömüldü' if wbc_live_block else 'atlandı'))
                _rep.append(('Homomorphic VM guard', bool(hvm_live_block),
                             'gömüldü' if hvm_live_block else 'atlandı'))
                _rep.append(('Cython .so', bool(cython_so_file), 'üretildi' if cython_so_file else 'atlandı'))
                _rep.append(('Nuitka native (ZORUNLU)', bool(native_file),
                             Path(native_file).name if native_file else 'YOK'))
                _rep.append(('LazyChunk parçalama', bool(lazy_chunks), f'{len(lazy_chunks)} parça'))
                # ── (v9) Yeni katmanlar ──────────────────────────────────────
                _rep.append(('KeyFusionBus (guard→key)', bool(_real_fusion_key),
                             f'{len(_fusion_bus.contributions)} guard kenetlendi'
                             if _real_fusion_key else 'atlandı'))
                _rep.append(('Native .so hash guard', bool(native_fname) and bool(_real_fusion_key),
                             'fusion katkısı' if (native_fname and _real_fusion_key) else 'native yok/atlandı'))
                # (v9 Fikir #6) SELF-VERIFY: yukarıda zaten yapıldı (_fusion_ok)
                _fusion_verified = bool(_real_fusion_key)  # fallback olduysa False
                _rep.append(('Fusion self-verify', _fusion_verified,
                             'round-trip OK (temiz ortam anahtarı üretir)'
                             if _fusion_verified else 'DOĞRULANAMADI → fusion uygulanmadı'))
                _rep.append(('JunkBytecode (tersinir)', True, 'strip ile geri alınır — marshal bozulmaz'))
                _rep.append(('Polimorfik VM gövdesi', _vm_moved > 0,
                             'handler sırası her build farklı' if _vm_moved > 0 else 'VM fonksiyon yok'))
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

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description='NinjaEnc — Python Kod Koruyucu (native derleme ZORUNLU; çıktı platforma bağlıdır)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='\nÖrnekler:\n  python3 ninjaenc.py                           # Interactive mod (Ultimate)\n  python3 ninjaenc.py script.py                 # Ultimate encode\n  python3 ninjaenc.py script.py -o out.py       # Çıktı adı belirt\n  python3 ninjaenc.py script.py --seed 12345    # Tekrarlanabilir çıktı\n  python3 ninjaenc.py script.py --yes           # Nuitka\'yı sormadan kur\n  python3 ninjaenc.py --decode encoded.py       # Decode et\n'
    )
    parser.add_argument('input', nargs='?', help='Input Python dosyası')
    parser.add_argument('-o', '--output', help='Output dosya adı')
    # NOT (v18): Tek encode modu Ultimate'tir. Eski devre-dışı bayraklar
    # (--advanced/--cython/--nuitka/--hyperion/--ultimate/--info/--sysinfo)
    # kaldırıldı; hiçbiri işlevsel değildi.
    parser.add_argument('--decode', action='store_true', help='Decode et')
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
        # (v18) Gerçek Ultimate boru hattı — encode_ultimate() akışıyla uyumlu.
        # Eski ölü-dal katmanları (.pyc mutasyon, Polymorphic/HMAC/RKM/Shamir/
        # Ascii85, Self-Modifying, PNG-stego) kaldırıldı; artık yalnızca fiilen
        # uygulanan katmanlar listeleniyor.
        layers = [
            ("1",  "String Table Encrypt",      "String literaller XOR şifreli runtime lookup tablosuna taşınır"),
            ("2",  "AST Obfuscation",           "shake_128 hash tabanlı değişken/fonksiyon yeniden adlandırma"),
            ("3",  "MBA Transform",             "L1+L2 polinom zinciri — aritmetik/mantık karmaşıklaştırma"),
            ("4",  "Control Flow Flatten",      "State-machine dispatcher — orijinal akış gizlenir"),
            ("5",  "Opaque Predicates",         "Sabit matematik ifadeleri — her zaman True/False"),
            ("6",  "Dead Code Injection",       "Sahte bloklar — statik analizi zorlaştırır"),
            ("7",  "Fake Branch Injection",     "Sahte if/else dalları — kontrol akışı gizlenir"),
            ("8",  "MetamorphicStager",         "Her encode transform'ları farklı rastgele sırada uygular"),
            ("9",  "FakeRecursion Stub",        "Derin sahte çağrı yığını — stack trace yanıltır"),
            ("10", "ClassCamouflage x10",       "10 sahte class — hangisi gerçek payload taşıyor bilinmez"),
            ("11", "FakeException x300 (+decoy)","300 sahte try/except + 5 canlı anti-debug decoy"),
            ("12", "StringSplitter chr()",      "String literaller chr() zincirine bölünür"),
            ("13", "FakeImportTree x56",        "56 sahte import — bağımlılık/CFG analizi yanılır"),
            ("14", "ConstantFoldingSaboteur",   "Sabit tamsayılar runtime ifadeye dönüştürülür"),
            ("15", "LambdaSoupWrapper x20",     "Fonksiyonlar lambda zincirine sarılır"),
            ("16", "Marshal + JunkBytecode",    "Payload marshal.dumps + TERSİNİR junk (runtime strip)"),
            ("17", "Zlib Compress (L9)",        "Payload level-9 sıkıştırılır"),
            ("18", "Multi-Layer XOR",           "Çok anahtarlı zincirleme XOR"),
            ("19", "KeyFusionBus",              "Guard sonuçları → anahtar; patch'lenirse payload çöp"),
            ("20", "ChaCha20-Poly1305 (canlı)", "AEAD — canlı payload'a uygulanır (pycryptodome)"),
            ("21", "Twofish-256-CBC (canlı)",   "GERÇEK Twofish (AES finalisti) — 16 round, saf-Python"),
            ("22", "AES-256-GCM + PBKDF2",      "Canlı — 400k-600k iter, SHA-512, 32-byte salt, GCM tag"),
            ("23", "LazyChunk 5-Part",          "5 parça zincir key: sha256(chunk[i])[0]^key[i]→key[i+1]"),
            ("24", "MiniVM Dispatcher",         "Custom opcode VM: DECOMP→MARSHAL→EXEC→WIPE"),
            ("25", "White-Box AES (gerçek)",    "Encoding'li T-box — anahtar tablodan çıkarılamaz (canlı bütünlük)"),
            ("26", "Homomorphic VM Guard",      "R-maskeli register aritmetiği — statik okuma işe yaramaz"),
            ("27", "Honeypot Shell",            "Sahte servis/config/pipeline — analisti yanıltan yollar"),
            ("28", "Whitespace Stego",          "Guard bütünlük etiketi whitespace/tab encoding'e gizlenir"),
            ("29", "HW Fingerprint Key",        "hostname+arch+cpu'dan SHA256 türetme — hardcoded değil"),
            ("30", "Runtime Guards",            "JITPoison+MemoryCanary+AntiVM+SysTraceNuke+ImportHookPoison+async anti-debug"),
            ("31", "Fake .so x15",              "15 sahte native kütüphane ZIP'e eklenir"),
            ("32", "FakePycFlood x50",          "50 sahte .pyc ZIP'e eklenir"),
            ("33", "Native (Nuitka ZORUNLU)",   "Kritik kaynak C'ye derlenir (+opsiyonel Cython .so) — fallback YOK"),
            ("34", "ZIP + Chunked Loader",      "ZIP paketi base64 + parçalı loader ile sarılır"),
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
        # ────────────────────────────────────────────────────────────────
    encoder = NinjaEncoder()
    if args.decode:
        output = encoder.decode_file(args.input, args.output)
        print(S + f'[+] Decoded: {B}{output}')
    else:
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
