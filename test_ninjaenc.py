#!/usr/bin/env python3
"""
NinjaEnc doğrulama harness'ı
============================

Bir obfuscator'ın GERÇEK gücü = (koruma gücü) × (çıktının doğru çalışma olasılığı).
Bu harness ikinci çarpanı ÖLÇER: her kaynak-seviye katmanı bir örnek bataryasında
(int/float aritmetik, sınıf, özyineleme, kwargs, closure, comprehension, exception)
uygular ve dönüştürülmüş kaynağın:
  1. hâlâ compile() edilebildiğini,
  2. davranış-koruyan katmanlar için orijinalle AYNI stdout/dönüş değerini
     ürettiğini
doğrular. Böylece sessizce bozulan/atlanan katmanlar ölçülebilir bir sinyale döner.

Kullanım:
  python3 test_ninjaenc.py           # kaynak-seviye + NinjaVM testleri (hızlı)
  python3 test_ninjaenc.py --e2e     # ayrıca tam encode_ultimate + native çalıştırma
                                     # (Nuitka + C derleyici gerektirir; yavaş)
"""
import importlib.util
import io
import sys
import os
import random
import logging
import tempfile
import subprocess
import contextlib
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATH = os.path.join(HERE, 'cevbey_new_enc_6.py')

GREEN = '\x1b[92m'; RED = '\x1b[91m'; YEL = '\x1b[93m'; CYN = '\x1b[96m'; RST = '\x1b[0m'


def load_module():
    logging.disable(logging.CRITICAL)  # boru hattı INFO gürültüsünü sustur
    spec = importlib.util.spec_from_file_location('ninjaenc_under_test', MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@contextlib.contextmanager
def _captured():
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        yield buf
    finally:
        sys.stdout = old


def _run_source(src):
    """Kaynağı izole bir namespace'te çalıştır → (stdout, exception|None)."""
    ns = {'__name__': '__main__'}
    try:
        code = compile(src, '<harness>', 'exec')
    except Exception as e:
        return None, ('compile', e)
    with _captured() as buf:
        try:
            exec(code, ns)
        except Exception as e:
            return buf.getvalue(), ('runtime', e)
    return buf.getvalue(), None


# ── Örnek batarya: her biri deterministik çıktı basan tam modül ──────────────
SAMPLES = {
    'int_arith': '''
def f(a, b):
    return (a ^ b) + (a & b) - (a | b) + a * b - (a - b)
print(f(12, 10), f(255, 3), f(-7, 5))
''',
    'float_arith': '''
def g(a, b):
    return a - b + a * b - (a - b) * 2.0
print(round(g(3.5, 1.2), 4), round(g(10.0, 4.5), 4))
''',
    'kwargs': '''
def area(width, height=2):
    return width * height
def show(**opts):
    return opts.get('color', 'none')
print(area(width=4, height=5), area(3), show(color='red'), show())
''',
    'classes': '''
class Point:
    def __init__(self, x, y):
        self.x = x; self.y = y
    def norm2(self):
        return self.x * self.x + self.y * self.y
p = Point(3, 4)
print(p.norm2(), Point(1, 2).norm2())
''',
    'recursion': '''
def fib(n):
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)
def fact(n):
    return 1 if n <= 1 else n * fact(n - 1)
print(fib(10), fact(6))
''',
    'closures': '''
def make_adder(n):
    def add(x):
        return x + n
    return add
a5 = make_adder(5)
print(a5(10), make_adder(100)(1))
''',
    'comprehensions': '''
data = [i * i for i in range(6)]
d = {k: k + 1 for k in range(3)}
print(sum(data), d[2], [x for x in data if x % 2 == 0])
''',
    'exceptions': '''
def safe_div(a, b):
    try:
        return a // b
    except ZeroDivisionError:
        return -1
print(safe_div(10, 2), safe_div(5, 0))
''',
    'strings': '''
def greet(name):
    return "Hello, " + name + "!"
print(greet("world"), "abc".upper(), len("ninja"))
''',
}


# ── Test edilecek kaynak→kaynak dönüşümler ───────────────────────────────────
def build_transforms(m):
    """(ad, fn) listesi — fn(src)->yeni_src. Yalnızca davranış-koruyan katmanlar."""
    T = []
    T.append(('ASTObfuscator', lambda s: m.ASTObfuscator().obfuscate(s)))
    T.append(('MBATransformer', lambda s: m.MBATransformer().transform_source(s)))
    T.append(('ControlFlowFlattener', lambda s: m.ControlFlowFlattener().flatten_source(s)))
    T.append(('OpaquePredicates', lambda s: m.OpaquePredicates().wrap_with_opaques(s)))
    T.append(('StringTableEncryptor', lambda s: m.StringTableEncryptor().encrypt_to_table(s)))
    T.append(('DeadCodeInjector', lambda s: m.DeadCodeInjector().inject_into_source(s, count=5)))
    T.append(('ControlFlowObfuscator', lambda s: m.ControlFlowObfuscator().inject_fake_branches(s)))
    # v5 enjektörleri — hepsi statik metod
    for cname, meth, kw in [
        ('StringSplitter', 'transform_source', {}),
        ('ConstantFoldingSaboteur', 'transform_source', {}),
        ('LambdaSoupWrapper', 'inject', {}),
        ('ClassCamouflage', 'wrap_source', {}),
        ('FakeExceptionInjector', 'inject_into_source', {'count': 20}),
        ('FakeRecursion', 'inject', {}),
    ]:
        cls = getattr(m, cname, None)
        if cls is None:
            continue
        fn = getattr(cls, meth, None)
        if fn is None:
            continue
        T.append((cname, (lambda f, k: (lambda s: f(s, **k)))(fn, kw)))
    return T


# Boru hattının (encode_ultimate) her payload'a garanti ettiği runtime import
# öneki. Bazı katmanlar (ör. OpaquePredicates) bu import'lara güvenir; harness
# aynı koşulu sağlamalı ki gerçek davranışı test etsin.
PIPELINE_PREAMBLE = (
    'import random\nimport sys\nimport hashlib\nimport os\nimport time\n'
    'import platform\nimport struct\nimport marshal\nimport zlib\n'
    'import base64\nimport tempfile\nimport shutil\n'
)


def test_source_layers(m):
    print(f'{CYN}══ Kaynak-seviye katman testleri ══{RST}')
    transforms = build_transforms(m)
    results = {}
    for name, fn in transforms:
        ok = 0; fail = 0; details = []
        for sname, src in SAMPLES.items():
            base_out, base_err = _run_source(PIPELINE_PREAMBLE + src)
            if base_err is not None:
                # örnek kendisi bozuksa atla (harness hatası)
                details.append(f'{sname}: örnek hatası, atlandı')
                continue
            # determinizm için sabit seed
            random.seed(0xC0FFEE)
            try:
                new_src = fn(src)
            except Exception as e:
                fail += 1
                details.append(f'{sname}: dönüşüm istisnası {type(e).__name__}: {e}')
                continue
            new_out, new_err = _run_source(PIPELINE_PREAMBLE + new_src)
            if new_err is not None:
                fail += 1
                kind, err = new_err
                details.append(f'{sname}: {kind} {type(err).__name__}: {err}')
                continue
            if new_out != base_out:
                fail += 1
                details.append(f'{sname}: çıktı farkı {base_out!r} != {new_out!r}')
                continue
            ok += 1
        results[name] = (ok, fail, details)
        status = f'{GREEN}PASS{RST}' if fail == 0 else f'{RED}FAIL{RST}'
        print(f'  [{status}] {name:<24} {ok}/{ok + fail} örnek')
        if fail:
            for d in details:
                print(f'         {YEL}- {d}{RST}')
    return results


def test_ninja_vm(m):
    print(f'{CYN}══ NinjaVM (custom ISA) testleri ══{RST}')
    prog = '''
@ninja_vm
def calc(a, b):
    c = a * b + 7
    if c > 50:
        c = c - 10
    else:
        c = c + 3
    return c

@ninja_vm
def loopsum(x):
    r = 0
    i = 0
    while i < x:
        r = r + i * i
        i = i + 1
    return r
'''
    def refcalc(a, b):
        c = a * b + 7
        return c - 10 if c > 50 else c + 3
    def refloop(x):
        r = 0; i = 0
        while i < x:
            r += i * i; i += 1
        return r

    vm = m.NinjaVM()
    random.seed(2024)
    out, moved = vm.transform_source(prog)
    ns = {}
    exec(compile(out, '<vm>', 'exec'), ns)
    ok = True
    for a, b in [(3, 4), (9, 9), (2, 100), (-5, 6)]:
        if ns['calc'](a, b) != refcalc(a, b):
            ok = False
    for x in [0, 1, 5, 12]:
        if ns['loopsum'](x) != refloop(x):
            ok = False
    print(f'  [{GREEN + "PASS" + RST if ok and moved == 2 else RED + "FAIL" + RST}] '
          f'round-trip {moved}/2 fonksiyon VM\'e derlendi, sonuçlar eşleşti={ok}')

    # ISA her build'de farklı olmalı
    v1, v2 = m.NinjaVM(), m.NinjaVM()
    diff = any(getattr(v1, n) != getattr(v2, n) for n in m.NinjaVM._ISA_MAIN)
    print(f'  [{GREEN + "PASS" + RST if diff else RED + "FAIL" + RST}] ISA per-build rastgele '
          f'(vm1.CALL={v1.CALL} vm2.CALL={v2.CALL})')

    # aynı seed → aynı ISA (determinizm)
    random.seed(11); a = m.NinjaVM(); random.seed(11); b = m.NinjaVM()
    det = all(getattr(a, n) == getattr(b, n) for n in m.NinjaVM._ISA_MAIN)
    print(f'  [{GREEN + "PASS" + RST if det else RED + "FAIL" + RST}] aynı seed → aynı ISA (determinizm)')
    return ok and moved == 2 and diff and det


def test_vm_ast_integration(m):
    """NinjaVM + AST rename entegrasyonu: VM'e taşınan özyinelemeli/global-çağıran
    fonksiyonlar, sonraki isim yeniden adlandırmasından SONRA da çalışmalı
    (blob'a gömülü global adların AST'de korunması)."""
    print(f'{CYN}══ NinjaVM + AST entegrasyon testi ══{RST}')
    src = '''def fib(n):
    return n if n < 2 else fib(n - 1) + fib(n - 2)
def helper(a, b):
    return a * b + 1
def usehelper(x):
    return helper(x, x)
print("fib=", fib(11))
print("uh=", usehelper(7))
'''
    random.seed(4242)
    enc = m.NinjaEncoder()
    vm_src, moved = enc.ninja_vm.transform_source(src)
    obf = enc.ast_obf.obfuscate(vm_src, extra_protected=getattr(enc.ninja_vm, 'protected_names', None))
    out, err = _run_source(PIPELINE_PREAMBLE + obf)
    expected = 'fib= 89\nuh= 50\n'
    ok = (err is None and out == expected)
    print(f'  [{GREEN + "PASS" + RST if ok else RED + "FAIL" + RST}] '
          f'{moved} fonksiyon VM+rename sonrası çalıştı '
          f'(korunan: {sorted(getattr(enc.ninja_vm, "protected_names", []))})')
    if not ok:
        print(f'    beklenen: {expected!r}  alınan: {out!r}  hata: {err}')
    return ok


# Sandbox/VM tespiti yapan runtime guard'lar bir KONTEYNERDE (bu CI ortamı gibi)
# payload çalışmadan _exit(0) yapar — bu KASITLI anti-analiz davranışıdır. Uçtan-uca
# testin AMACI payload'ın DOĞRU çalıştığını doğrulamaktır, guard'ları değil; bu yüzden
# test sırasında ortam guard'larını etkisizleştiririz (kalıcı dosya değişmez).
_GUARD_ATTRS = [
    ('AntiVM', 'generate_code'),
    ('JITPoison', 'generate_code'),
    ('MemoryCanary', 'generate_code'),
    ('SysTraceNuke', 'generate_code'),
    ('ImportHookPoison', 'generate_code'),
    ('AntiDebug', 'generate_async_runtime_check'),
]


def _neutralize_guards(m):
    for cname, meth in _GUARD_ATTRS:
        cls = getattr(m, cname, None)
        if cls is not None and hasattr(cls, meth):
            setattr(cls, meth, staticmethod(lambda *a, **k: ''))


def test_end_to_end(m):
    print(f'{CYN}══ Uçtan-uca test (tam encode_ultimate + native çalıştırma) ══{RST}')
    print(f'  {YEL}not: sandbox/VM anti-analiz guard\'ları test için etkisizleştirildi '
          f'(payload doğruluğu ölçülüyor; guard\'lar bu konteynerde _exit(0) yapardı).{RST}')
    _neutralize_guards(m)
    ok_tool, msg = m.ensure_native_toolchain(auto_install=False, assume_yes=True)
    if not ok_tool:
        print(f'  {YEL}ATLANDI: native zincir hazır değil ({msg}). '
              f'`pip install nuitka` + gcc/clang gerekir.{RST}')
        return None
    sample = '''
def fib(n):
    return n if n < 2 else fib(n - 1) + fib(n - 2)
def area(width, height=3):
    return width * height
print("fib10=", fib(10))
print("area=", area(width=4, height=5))
print("floats=", round(3.5 - 1.2, 4))
'''
    workdir = tempfile.mkdtemp(prefix='ninja_e2e_')
    inp = os.path.join(workdir, 'sample.py')
    outp = os.path.join(workdir, 'sample_enc.py')
    with open(inp, 'w') as f:
        f.write(sample)
    base = subprocess.run([sys.executable, inp], capture_output=True, text=True, timeout=60)
    try:
        enc = m.NinjaEncoder()
        enc.encode_ultimate(inp, outp, seed=1234, assume_yes=True)
    except Exception as e:
        print(f'  {RED}FAIL: encode_ultimate istisna: {e}{RST}')
        traceback.print_exc()
        return False
    if not os.path.exists(outp):
        print(f'  {RED}FAIL: çıktı üretilmedi{RST}')
        return False
    run = subprocess.run([sys.executable, outp], capture_output=True, text=True, timeout=180)
    same = (run.stdout == base.stdout)
    status = f'{GREEN}PASS{RST}' if same else f'{RED}FAIL{RST}'
    print(f'  [{status}] encode edilmiş çıktı çalıştı, stdout eşleşti={same}')
    if not same:
        print(f'    beklenen: {base.stdout!r}')
        print(f'    alınan  : {run.stdout!r}')
        print(f'    stderr  : {run.stderr[-800:]!r}')
    return same


def main():
    e2e = '--e2e' in sys.argv
    m = load_module()
    src_results = test_source_layers(m)
    vm_ok = test_ninja_vm(m)
    vm_ast_ok = test_vm_ast_integration(m)

    total_fail = sum(1 for _o, f, _d in src_results.values() if f)
    print(f'{CYN}══ Özet ══{RST}')
    passed = len(src_results) - total_fail
    print(f'  Kaynak katmanları: {passed}/{len(src_results)} tam geçti')
    print(f'  NinjaVM: {"PASS" if vm_ok else "FAIL"}')
    print(f'  NinjaVM+AST entegrasyon: {"PASS" if vm_ast_ok else "FAIL"}')

    e2e_ok = None
    if e2e:
        e2e_ok = test_end_to_end(m)
        print(f'  Uçtan-uca: {"PASS" if e2e_ok else ("ATLANDI" if e2e_ok is None else "FAIL")}')

    hard_fail = (total_fail > 0) or (not vm_ok) or (not vm_ast_ok) or (e2e_ok is False)
    print(f'{(GREEN + "TÜM TESTLER GEÇTİ" if not hard_fail else RED + "BAŞARISIZ TESTLER VAR")}{RST}')
    sys.exit(1 if hard_fail else 0)


if __name__ == '__main__':
    main()
