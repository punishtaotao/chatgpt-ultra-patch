# -*- coding: utf-8 -*-
r"""Update the asar integrity hash embedded in the Electron executable.

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

Importable: patch_exe_hash(exe_path, asar_path, backup=False, log=print) -> dict
"""
import io, os, sys, hashlib, struct, re, shutil

MARKER_PAT = re.compile(
    rb'"file":"resources\\\\app\.asar","alg":"SHA256","value":"([0-9a-f]{64})"')


class PatchError(Exception):
    pass


def header_sha256(asar_path):
    with io.open(asar_path, "rb") as f:
        head = f.read(16)
        a, b, c, d = struct.unpack("<IIII", head)
        js = f.read(d)
    return hashlib.sha256(js).hexdigest()


def patch_exe_hash(exe_path, asar_path, backup=False, log=print):
    exe = os.path.abspath(exe_path)
    asar = os.path.abspath(asar_path)
    if not os.path.isfile(exe):
        raise PatchError("exe not found: %s" % exe)
    if not os.path.isfile(asar):
        raise PatchError("asar not found: %s" % asar)

    new_hash = header_sha256(asar).encode("ascii")
    log("patched asar header sha256 = %s" % new_hash.decode())

    with io.open(exe, "rb") as f:
        blob = f.read()

    hits = MARKER_PAT.findall(blob)
    log("integrity resource entries found in exe = %d" % len(hits))
    if not hits:
        raise PatchError("integrity marker not found in exe "
                         "(already patched, or different app layout)")

    nb = blob
    replaced = 0
    for old in set(hits):
        if old == new_hash:
            log("already up to date for %s" % old.decode())
            continue
        nb = nb.replace(b'"value":"' + old + b'"', b'"value":"' + new_hash + b'"')
        replaced += 1
    if replaced == 0:
        return {"replaced": 0, "new_header_sha256": new_hash.decode()}

    if backup and not os.path.exists(exe + ".bak-hash"):
        shutil.copy2(exe, exe + ".bak-hash")
        log("backup saved: %s.bak-hash" % exe)

    tmp = exe + ".tmp"
    with io.open(tmp, "wb") as f:
        f.write(nb)
    import time
    last_err = None
    for attempt in range(5):
        try:
            os.replace(tmp, exe)
            break
        except PermissionError as e:
            last_err = e
            log("replace attempt %d failed (file locked), retrying..." % (attempt + 1))
            time.sleep(3)
    else:
        raise PatchError("could not replace the exe (file locked): %r "
                         "- close the app and retry" % last_err)
    log("exe updated, %d hash(es) replaced" % replaced)
    return {"replaced": replaced, "new_header_sha256": new_hash.decode()}


def main():
    if len(sys.argv) < 3:
        print("usage: python patch_exe_hash.py <ChatGPT.exe> <app.asar> [--backup]")
        sys.exit(64)
    try:
        patch_exe_hash(sys.argv[1], sys.argv[2], backup=("--backup" in sys.argv[3:]))
    except PatchError as e:
        print("ERROR: %s" % e)
        sys.exit(1)


if __name__ == "__main__":
    main()
