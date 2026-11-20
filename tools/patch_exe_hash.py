# -*- coding: utf-8 -*-
"""Update the asar integrity hash embedded in the Electron executable.

Electron builds of some packaged apps (e.g. the ChatGPT desktop app) embed a
binary resource of the form:

    [{"file":"resources\\app.asar","alg":"SHA256","value":"<64 hex chars>"}]

At startup Electron compares the SHA256 of the asar *header* against this
value and dies with `FATAL ... asar_util.cc Integrity check failed` on
mismatch.  If you patch the asar (see patch_asar.py) you must replace the
embedded hex with the new header hash.  Both are 64 ASCII hex chars, so the
replacement is length-safe.

Usage:
  python patch_exe_hash.py <path\to\ChatGPT.exe> <path\to\app.asar> [--backup]
"""
import io, os, sys, hashlib, struct, re, shutil

MARKER_PAT = re.compile(
    rb'"file":"resources\\\\app\.asar","alg":"SHA256","value":"([0-9a-f]{64})"')


def header_sha256(asar_path):
    with io.open(asar_path, "rb") as f:
        head = f.read(16)
        a, b, c, d = struct.unpack("<IIII", head)
        js = f.read(d)
    return hashlib.sha256(js).hexdigest()


def main():
    if len(sys.argv) < 3:
        print("usage: python patch_exe_hash.py <ChatGPT.exe> <app.asar> [--backup]")
        sys.exit(64)
    exe = os.path.abspath(sys.argv[1])
    asar = os.path.abspath(sys.argv[2])
    do_backup = "--backup" in sys.argv[3:]

    new_hash = header_sha256(asar).encode("ascii")
    print("patched asar header sha256 = %s" % new_hash.decode())

    with io.open(exe, "rb") as f:
        blob = f.read()

    hits = MARKER_PAT.findall(blob)
    print("integrity resource entries found in exe = %d" % len(hits))
    if not hits:
        print("nothing to patch - marker not found (already patched, or different app layout)")
        sys.exit(1)
    if len(set(hits)) > 1:
        print("WARNING: multiple distinct hashes present: %s" % [h.decode() for h in set(hits)])

    nb = blob
    cnt = 0
    for old in set(hits):
        if old == new_hash:
            print("already up to date for %s" % old.decode())
            continue
        # length-safe equal-length replacement
        nb = nb.replace(b'"value":"' + old + b'"', b'"value":"' + new_hash + b'"')
        cnt += 1
    if cnt == 0:
        sys.exit(0)

    if do_backup and not os.path.exists(exe + ".bak-hash"):
        shutil.copy2(exe, exe + ".bak-hash")
        print("backup saved: %s.bak-hash" % exe)

    tmp = exe + ".tmp"
    with io.open(tmp, "wb") as f:
        f.write(nb)
    for attempt in range(5):
        try:
            os.replace(tmp, exe)
            break
        except PermissionError:
            import time
            time.sleep(3)
            if attempt == 4:
                print("REPLACE FAILED - file locked (close the app and retry)")
                sys.exit(3)
    print("exe updated, %d hash(es) replaced -> %s" % (cnt, new_hash.decode()))


if __name__ == "__main__":
    main()
