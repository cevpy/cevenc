"""
NinjaVMHardened (VM Hardening Layer) testleri.

Doğrulanan davranışlar:
  1. VM'e taşınan fonksiyon orijinaliyle BİREBİR aynı sonucu üretir
     (döngü, karşılaştırma, try/except/exc-table dahil).
  2. Program blob'u artık düz `marshal.loads(b64decode(blob))` ile açılamaz
     (build'e özgü keystream ile şifreli).
  3. Çıktı lazy çözücü (_nv_open) kullanır; eski düz marshal stub'ı kalmaz.
  4. İki bağımsız build farklı blob VE farklı anahtar üretir (polimorfik) —
     bir hedef için yazılan devirtualizer diğerinde çalışmaz.

Çalıştırma:  python3 -m pytest tests/ -q      (pytest varsa)
             python3 tests/test_vm_hardening.py   (düz çalıştırma)
"""
import base64
import importlib.util
import marshal
import os
import sys

sys.argv = ['test']
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "ninjaenc", os.path.join(_ROOT, "ninjaenc.py"))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


CALC_SRC = (
    "def calc(a, b):\n"
    "    c = a * b + 7\n"
    "    total = 0\n"
    "    for i in range(a):\n"
    "        total = total + i * b\n"
    "    if c > total:\n"
    "        return c - total\n"
    "    return total - c\n"
)


def _calc_ref(a, b):
    c = a * b + 7
    total = 0
    for i in range(a):
        total = total + i * b
    return c - total if c > total else total - c


TRY_SRC = (
    "def transform(x, y):\n"
    "    out = []\n"
    "    for i in range(x):\n"
    "        try:\n"
    "            v = (i * y) // (i - 2)\n"
    "        except ZeroDivisionError:\n"
    "            v = -1\n"
    "        if v > 0 and v % 2 == 0:\n"
    "            out.append(v)\n"
    "        elif v < 0:\n"
    "            out.append(0)\n"
    "    s = sum(out)\n"
    "    return s if s else len(out)\n"
)


def _try_ref(x, y):
    out = []
    for i in range(x):
        try:
            v = (i * y) // (i - 2)
        except ZeroDivisionError:
            v = -1
        if v > 0 and v % 2 == 0:
            out.append(v)
        elif v < 0:
            out.append(0)
    s = sum(out)
    return s if s else len(out)


def _blob_of(src_text, fname):
    for line in src_text.splitlines():
        if line.startswith("_NVP_" + fname):
            return line.split("=", 1)[1].strip().strip("'")
    return None


def _build(mod, src, name):
    vm = mod.NinjaVMHardened()
    out, moved = vm.transform_source(src, selected_names={name})
    assert moved == 1, f"{name} VM'e taşınmadı (moved={moved})"
    ns = {}
    exec(out, ns)
    return vm, out, ns


def test_correctness_calc():
    mod = _load_module()
    _, _, ns = _build(mod, CALC_SRC, "calc")
    for a in range(0, 13):
        for b in range(0, 6):
            assert ns["calc"](a, b) == _calc_ref(a, b), (a, b)


def test_correctness_exception_table():
    mod = _load_module()
    _, _, ns = _build(mod, TRY_SRC, "transform")
    for x in range(0, 12):
        for y in range(0, 6):
            assert ns["transform"](x, y) == _try_ref(x, y), (x, y)


def _is_valid_prog(p):
    return isinstance(p, dict) and {'c', 'k', 'n', 'l'} <= set(p.keys())


def test_blob_is_encrypted():
    # Güvenlik özelliği: blob'u ŞİFRE ÇÖZMEDEN marshal.loads etmek geçerli
    # programı VERMEZ (marshal bozuk baytlarda bazen hata vermeden çöp döndürür,
    # o yüzden "hata fırlatmalı" değil "geçerli program vermemeli" test edilir).
    # Buna karşılık doğru yol (keystream çözme) geçerli programı verir.
    mod = _load_module()
    vm, out, _ = _build(mod, CALC_SRC, "calc")
    blob = _blob_of(out, "calc")
    assert blob is not None
    raw = base64.b64decode(blob)
    static_prog = None
    try:
        static_prog = marshal.loads(raw)
    except Exception:
        static_prog = None
    assert not _is_valid_prog(static_prog), \
        "şifrelenmemiş: statik marshal.loads geçerli program verdi"
    assert _is_valid_prog(vm._prog_unseal(blob)), \
        "keystream çözme geçerli program vermiyor"


def test_lazy_decoder_present_and_plain_stub_gone():
    mod = _load_module()
    _, out, _ = _build(mod, CALC_SRC, "calc")
    assert "_nv_open" in out, "lazy çözücü prelude yok"
    assert "marshal.loads(_b64.b64decode" not in out.replace(" ", ""), \
        "eski düz marshal stub'ı hâlâ üretiliyor"


def test_polymorphic_across_builds():
    mod = _load_module()
    vm1, out1, ns1 = _build(mod, CALC_SRC, "calc")
    vm2, out2, ns2 = _build(mod, CALC_SRC, "calc")
    assert _blob_of(out1, "calc") != _blob_of(out2, "calc"), "blob'lar build'ler arası aynı"
    keys1 = (vm1._blobSeed, vm1._lcgA, vm1._lcgC)
    keys2 = (vm2._blobSeed, vm2._lcgA, vm2._lcgC)
    assert keys1 != keys2, "blob anahtarları build'ler arası aynı"
    # her iki build de doğru çalışmalı
    for a, b in [(3, 4), (7, 7), (0, 9)]:
        assert ns1["calc"](a, b) == _calc_ref(a, b)
        assert ns2["calc"](a, b) == _calc_ref(a, b)


MULTI_SRC = (
    "def alpha(a, b):\n"
    "    t = 0\n"
    "    for i in range(a):\n"
    "        t += i * b\n"
    "    return t - b if t > b else t + b\n"
    "def beta(x, y):\n"
    "    s = 1\n"
    "    for i in range(1, x + 1):\n"
    "        s = s * i + y\n"
    "    return s\n"
)


def _alpha_ref(a, b):
    t = 0
    for i in range(a):
        t += i * b
    return t - b if t > b else t + b


def _beta_ref(x, y):
    s = 1
    for i in range(1, x + 1):
        s = s * i + y
    return s


def test_handler_body_variants():
    # (A) Handler gövdeleri build'ler arası anlamca eşdeğer ama byte-byte farklı
    # olmalı (gövde-deseni eşleştirmesini kırar), üstelik hep doğru çalışmalı.
    mod = _load_module()
    forms = set()
    probes = ['_s += [_k', '_s[len(_s):] = [_k', '_s.append(_k[_g2])',
              '0-_s.pop()', 'False if _s.pop()', 'return _s.pop(-1)', '_ip=_g2+0']
    for _ in range(40):
        vm = mod.NinjaVMHardened()
        rs = vm.runtime_source()
        for p in probes:
            if p in rs:
                forms.add(p)
        out, moved = vm.transform_source(CALC_SRC, selected_names={"calc"})
        assert moved == 1
        ns = {}
        exec(out, ns)
        for a, b in [(3, 4), (7, 7), (0, 9), (11, 2)]:
            assert ns["calc"](a, b) == _calc_ref(a, b), (a, b)
    assert len(forms) >= 3, f"handler gövde varyantları yetersiz ({forms})"


NEG_SRC = "def neg(x):\n    return -x\n"


def test_unary_neg_preserves_negative_zero():
    # Regresyon: UNARY_NEG handler'i tekli eksi (-x) olmali, ikili cikarma (0-x)
    # DEGIL. Fark yalnizca float/complex -0.0 ve ozel __neg__ tiplerinde gorunur
    # (tamsayida gizli kalir). random.choice varyanti yuzunden build'e bagli
    # olabilecegi icin cok sayida build denenir.
    import math
    mod = _load_module()
    for _ in range(30):
        vm = mod.NinjaVMHardened()
        out, moved = vm.transform_source(NEG_SRC, selected_names={"neg"})
        assert moved == 1
        ns = {}
        exec(out, ns)
        r = ns["neg"](0.0)
        assert math.copysign(1.0, r) == -1.0, "VM -0.0 isaretini kaybetti (0-x varyanti?)"
        assert ns["neg"](5) == -5 and ns["neg"](-3) == 3


def test_no_subtraction_masquerading_as_negation():
    # Savunma: hicbir handler varyanti tekli eksiyi ikili cikarmayla degistirmesin.
    mod = _load_module()
    for default, variants in mod.NinjaVMHardened._BODY_VARIANTS.items():
        if '-_s.pop()' in default:  # UNARY_NEG girisi
            assert all('0-_s.pop()' not in v for v in variants), \
                f"UNARY_NEG varyanti ikili cikarma iceriyor: {variants}"


def test_per_function_salt_and_no_keys_in_pp():
    # (master-key) Her VM'li fonksiyon kendi TUZU ile kodlanir; cozulmus _pp
    # icinde HICBIR ISA anahtari (ok/aa/ab) bulunmaz (hepsi master M'den turer,
    # M yalnizca native-derlenen interpreter'da). Havuzlar (k/n) sifreli bytes.
    mod = _load_module()
    vm = mod.NinjaVMHardened()
    out, moved = vm.transform_source(MULTI_SRC, selected_names={"alpha", "beta"})
    assert moved == 2
    ns = {}
    exec(out, ns)
    for a, b in [(3, 4), (7, 2), (0, 5), (9, 9)]:
        assert ns["alpha"](a, b) == _alpha_ref(a, b), (a, b)
    for x, y in [(3, 4), (5, 1), (1, 9), (6, 2)]:
        assert ns["beta"](x, y) == _beta_ref(x, y), (x, y)

    da = vm._prog_unseal(_blob_of(out, "alpha"))
    db = vm._prog_unseal(_blob_of(out, "beta"))
    # tuzlar farkli (per-fonksiyon)
    assert da["s"] != db["s"], "iki fonksiyon ayni tuzu paylasiyor"
    # _pp'de hicbir decode anahtari YOK
    for d in (da, db):
        assert "ok" not in d and "aa" not in d and "ab" not in d, "ISA anahtari _pp'de sizmis"
        # havuzlar sifreli bytes (duz liste/str degil)
        assert isinstance(d["k"], (bytes, bytearray)) and isinstance(d["n"], (bytes, bytearray))


def test_strings_hidden_in_const_pool():
    # Asil amac: KAYNAGI GIZLEME. Ayirt edici bir string sabiti, cozulmus _pp'nin
    # const havuzunda DUZ gorunmemeli (sifreli); yalnizca master-key ile cozulunce
    # ortaya cikmali. Yani _nv_open'i hook edip _pp dump eden biri string'i goremez.
    import marshal
    mod = _load_module()
    SECRET = "SUPER_SECRET_API_ENDPOINT_/v9/pay"
    src = (f"def route(n):\n    tag = {SECRET!r}\n    return tag * (n % 2) + str(n)\n")
    vm = mod.NinjaVMHardened()
    out, moved = vm.transform_source(src, selected_names={"route"})
    assert moved == 1
    ns = {}
    exec(out, ns)
    # calisirlik
    assert ns["route"](3) == (SECRET * 1 + "3")
    assert ns["route"](4) == (SECRET * 0 + "4")
    # cozulmus _pp'nin const havuzu ciphertext; string DUZ gecmemeli
    d = vm._prog_unseal(_blob_of(out, "route"))
    assert SECRET.encode() not in bytes(d["k"]), "string const havuzda DUZ gorunuyor!"
    # ... ama master-key ile cozulunce ortaya cikar (encode mantiginin aynasi)
    seed = vm._h(vm._M, d["s"], 0)
    ksb = vm._ksb(seed, 3, len(d["k"]))
    consts = marshal.loads(bytes(b ^ x for b, x in zip(d["k"], ksb)))
    assert SECRET in consts, "master-key ile cozulunce string geri gelmeli"


def test_base_ninjavm_unchanged():
    """Kimlik-varsayılanlı seam'ler temel NinjaVM davranışını bozmamalı."""
    mod = _load_module()
    vm = mod.NinjaVM()
    out, moved = vm.transform_source(CALC_SRC, selected_names={"calc"})
    assert moved == 1
    ns = {}
    exec(out, ns)
    # temel sürüm düz marshal stub'ı kullanmaya devam eder
    assert "_m.loads(_b64.b64decode" in out
    for a, b in [(3, 4), (5, 2), (10, 3)]:
        assert ns["calc"](a, b) == _calc_ref(a, b)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
