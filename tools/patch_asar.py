# -*- coding: utf-8 -*-
r"""Patch an Electron asar archive to unlock hidden reasoning-effort levels.

What it does (two things, both purely client-side text replacement):
  A) neutralize a server-side feature-flag gate:  fn(module,"1186680773") -> !0
     (the flag enables the "ultra" reasoning effort in the webview UI)
  B) extend the effort visibility default list:
     ["low","medium","high","xhigh"] -> [...,"max","ultra"]

It then rebuilds the asar header (offsets + per-file integrity hashes) and
rewrites the archive in place, keeping a .bak-original backup.

IMPORTANT: after patching the asar you MUST also update the expected header
hash embedded in the Electron executable, otherwise the app will crash at
startup with `FATAL asar_util.cc Integrity check failed`.  Use
patch_exe_hash.py for that step.

Usage:
  python patch_asar.py <path\to\app.asar>

Importable: patch_asar(path, log=print) -> dict
"""
import struct, json, hashlib, re, io, os, sys, shutil, time

FLAG_ID = "1186680773"  # includeUltraReasoningEffort feature flag
M1 = FLAG_ID.encode()
M2 = b'["low","medium","high","xhigh"]'

PATTERNS = [
    (re.compile(r'\w+\(\w+\.JW,"%s"\)' % FLAG_ID), '!0'),
    (re.compile(r'\(0,\w+\.\w+\)\(\w+,\s*"%s"\)' % FLAG_ID), '!0'),
    (re.compile(r'(?<![.\w])(\w+)\((\w+),"%s"\)' % FLAG_ID), '!0'),
    (re.compile(r'(\w+)=\["low","medium","high","xhigh"\],_='), r'\1=["low","medium","high","xhigh","max","ultra"],_='),
]


class PatchError(Exception):
    """Raised with a user-friendly message when patching must abort."""


def integrity(new_bytes, old_int):
    bs = old_int.get("blockSize", 4 * 1024 * 1024)
    blocks = [hashlib.sha256(new_bytes[i:i + bs]).hexdigest() for i in range(0, len(new_bytes), bs)]
    return {"algorithm": old_int.get("algorithm", "SHA256"),
            "hash": hashlib.sha256(new_bytes).hexdigest(),
            "blockSize": bs, "blocks": blocks}


def patch_asar(asar_path, log=print):
    """Patch the asar at asar_path.  Returns a result dict:
    {patched_files, new_header_sha256, backup}.  Raises PatchError."""
    ASAR = os.path.abspath(asar_path)
    if not os.path.isfile(ASAR):
        raise PatchError("asar not found: %s" % ASAR)
    BACKUP = ASAR + ".bak-original"
    tmp = ASAR + ".tmp"

    # remove stale tmp from a crashed run
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError as e:
            log("WARN cannot remove stale tmp: %s" % e)

    with io.open(ASAR, "rb") as f:
        blob = f.read()
    log("asar loaded: %d bytes" % len(blob))

    a, b, c, d = struct.unpack("<IIII", blob[:16])
    data_start = 8 + b
    tree = json.loads(blob[16:16 + d].decode("utf-8"))

    flat = {}
    def walk(node, prefix=""):
        if "files" in node:
            for name, ch in node["files"].items():
                walk(ch, prefix + "/" + name if prefix else name)
        else:
            if "offset" in node and not node.get("unpacked"):
                flat[prefix] = node
    walk(tree)
    log("files in asar = %d" % len(flat))

    # find candidate files by byte search in the blob
    candidates = []
    for rel, node in flat.items():
        off = int(node["offset"]); size = node["size"]
        seg = blob[data_start + off:data_start + off + size]
        if M1 in seg or M2 in seg:
            candidates.append((rel, node, seg))
    log("candidate files = %d" % len(candidates))

    patched = {}
    for rel, node, seg in candidates:
        try:
            src = seg.decode("utf-8")
        except UnicodeDecodeError:
            continue
        n = 0
        new = src
        for rx, rep in PATTERNS:
            new, k = rx.subn(rep, new)
            n += k
        if n == 0:
            continue
        log("  %s :: %d replacements" % (rel, n))
        patched[rel] = new.encode("utf-8")

    log("PATCHED FILES = %d" % len(patched))
    if not patched:
        raise PatchError("nothing matched - the copy may already be patched, "
                         "or this app version uses different code patterns")

    # build new node table; snapshot original offsets BEFORE mutating
    new_nodes = {}
    orig_offsets = {rel: int(node["offset"]) for rel, node in flat.items()}
    for rel, node in flat.items():
        if rel in patched:
            data = patched[rel]
            nd = dict(node)
            nd["size"] = len(data)
            if "integrity" in nd:
                nd["integrity"] = integrity(data, nd["integrity"])
            new_nodes[rel] = (nd, data)
        else:
            new_nodes[rel] = (node, None)

    pos = 0
    for rel, (node, data) in new_nodes.items():
        node["offset"] = str(pos)
        pos += node["size"]

    def refresh(node, prefix=""):
        if "files" in node:
            for name, ch in node["files"].items():
                p = prefix + "/" + name if prefix else name
                if p in new_nodes:
                    nd = new_nodes[p][0]
                    if ch is not nd:  # avoid clear()-ing the same dict we're copying from
                        ch.clear()
                        ch.update(nd)
                else:
                    refresh(ch, p)
    refresh(tree)

    new_json = json.dumps(tree, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    pad = (4 - len(new_json) % 4) % 4
    b_new = 4 + (4 + len(new_json) + pad)
    d_new = len(new_json)
    data_start_new = 8 + b_new
    log("new header: b=%d d=%d data_start=%d" % (b_new, d_new, data_start_new))

    if not os.path.exists(BACKUP):
        shutil.copy2(ASAR, BACKUP)
        log("backup saved: %s" % BACKUP)

    # assemble the new archive in memory
    header_bytes = struct.pack("<IIII", a, b_new, 4 + d_new + pad, d_new) + new_json + b"\x00" * pad
    if len(header_bytes) != data_start_new:
        header_bytes += b"\x00" * (data_start_new - len(header_bytes))
    parts = [header_bytes]
    for rel, (node, data) in new_nodes.items():
        if data is not None:
            parts.append(data)
        else:
            off = orig_offsets[rel]; size = node["size"]
            parts.append(blob[data_start + off:data_start + off + size])

    expected_total = data_start_new + sum(int(v[0]["size"]) for v in new_nodes.values())
    actual_total = len(header_bytes) + sum(len(p_) for p_ in parts[1:])
    if expected_total != actual_total:
        raise PatchError("internal size mismatch (%d vs %d) - aborted, file untouched"
                         % (expected_total, actual_total))

    log("writing file (%d bytes total)..." % actual_total)
    with io.open(tmp, "wb") as out:
        for p_ in parts:
            out.write(p_)

    # replace: os.replace first, in-place overwrite as fallback (file may be locked)
    replaced = False
    last_err = None
    for attempt in range(5):
        try:
            os.replace(tmp, ASAR)
            replaced = True
            break
        except PermissionError as e:
            last_err = e
            try:
                with open(tmp, "rb") as ftmp, open(ASAR, "r+b") as fdst:
                    while True:
                        chunk = ftmp.read(4 * 1024 * 1024)
                        if not chunk:
                            break
                        fdst.write(chunk)
                    fdst.truncate()
                replaced = True
                os.remove(tmp)
                break
            except PermissionError as e2:
                last_err = e2
                log("replace attempt %d failed (file locked), retrying..." % (attempt + 1))
                time.sleep(3)
    if not replaced:
        raise PatchError("could not replace the asar (file locked): %r "
                         "- close the app and retry" % last_err)
    log("archive rewritten, data bytes = %d" % pos)

    # verify: reload and check patched content is present
    with io.open(ASAR, "rb") as f:
        blob2 = f.read()
    a2, b2, c2, d2 = struct.unpack("<IIII", blob2[:16])
    t2 = json.loads(blob2[16:16 + d2].decode("utf-8"))
    hits = {"flag": 0, "slider": 0}
    def walk2(node, prefix=""):
        if "files" in node:
            for name, ch in node["files"].items():
                walk2(ch, prefix + "/" + name if prefix else name)
        else:
            if "offset" in node and not node.get("unpacked"):
                off = 8 + b2 + int(node["offset"]); size = node.get("size", 0)
                seg = blob2[off:off + size]
                if M1 in seg:
                    hits["flag"] += 1
                if b'"low","medium","high","xhigh","max","ultra"' in seg:
                    hits["slider"] += 1
    walk2(t2)
    log("VERIFY: ultra effort-list hits = %d" % hits["slider"])

    header_sha = hashlib.sha256(blob2[16:16 + d2]).hexdigest()
    return {
        "patched_files": len(patched),
        "new_header_sha256": header_sha,
        "backup": BACKUP,
        "residual_flag_hits": hits["flag"],
        "slider_hits": hits["slider"],
    }


def main():
    if len(sys.argv) < 2:
        print("usage: python patch_asar.py <path\\to\\app.asar>")
        sys.exit(64)
    try:
        res = patch_asar(sys.argv[1])
        print("NEW_HEADER_SHA256 = %s" % res["new_header_sha256"])
    except PatchError as e:
        print("ERROR: %s" % e)
        sys.exit(1)


if __name__ == "__main__":
    main()
