"""Alt bot başlatıcısı: kaynak sınırlarını uygular, sonra asıl programı exec eder.

Supervisor çok iş parçacıklı olduğu için preexec_fn yerine bu küçük ara program
kullanılır (preexec_fn thread'li süreçlerde kilitlenmeye yol açabilir).

Kullanım: python _launch.py <ram_bayt> <AS|DATA> <nice> -- <program> [argümanlar...]
"""
import os
import resource
import sys


def main():
    ram, mode, nice = int(sys.argv[1]), sys.argv[2].upper(), int(sys.argv[3])
    argv = sys.argv[sys.argv.index("--") + 1:]
    if ram > 0:
        limit = resource.RLIMIT_DATA if mode == "DATA" else resource.RLIMIT_AS
        resource.setrlimit(limit, (ram, ram))
    if nice:
        os.nice(nice)
    os.execv(argv[0], argv)


if __name__ == "__main__":
    main()
