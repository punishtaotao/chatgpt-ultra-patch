# -*- coding: utf-8 -*-
"""Patch an Electron asar archive to unlock hidden reasoning-effort levels.

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

Only well-known open formats are touched; the script is fully offline.
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


def integrity(new_bytes, old_int):
    bs = old_int.get("blockSize", 4 * 1024 * 1024)
    blocks = [hashlib.sha256(new_bytes[i:i + bs]).hexdigest() for i in range(0, len(new_bytes), bs)]
    return {"algorithm": old_int.get("algorithm", "SHA256"),
            "hash": hashlib.sha256(new_bytes).hexdigest(),
            "blockSize": bs, "blocks": blocks}


def main():
    if len(sys.argv) < 2:
        print("usage: python patch_asar.py <path\\to\\app.asar>")
        sys.exit(64)
    ASAR = os.path.abspath(sys.argv[1])
    BACKUP = ASAR + ".bak-original"
    tmp = ASAR + ".tmp"

    # remove stale tmp from a crashed run
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError as e:
            print("WARN cannot remove stale tmp: %s" % e)

    with io.open(ASAR, "rb") as f:
        blob = f.read()
    print("blob loaded %d bytes" % len(blob))

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
    print("files in asar = %d" % len(flat))

    # find candidate files by byte search in the blob
    candidates = []
    for rel, node in flat.items():
        off = int(node["offset"]); size = node["size"]
        seg = blob[data_start + off:data_start + off + size]
        if M1 in seg or M2 in seg:
            candidates.append((rel, node, seg))
    print("candidates = %d" % len(candidates))

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
        print("%s :: %d replacements (size %d -> %d)" % (rel, n, node["size"], len(new.encode("utf-8"))))
        patched[rel] = new.encode("utf-8")

    print("PATCHED FILES = %d" % len(patched))
    if not patched:
        print("NOTHING PATCHED - abort (already patched, or pattern mismatch for this app version)")
        sys.exit(1)

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
    print("new header: b=%d d=%d data_start=%d" % (b_new, d_new, data_start_new))

    if not os.path.exists(BACKUP):
        shutil.copy2(ASAR, BACKUP)
        print("backup saved: %s" % BACKUP)

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
        print("TOTAL MISMATCH expected=%d actual=%d" % (expected_total, actual_total))
        sys.exit(2)

    print("writing file (%d bytes total)" % actual_total)
    with io.open(tmp, "wb") as out:
        for p_ in parts:
            out.write(p_)

    # replace: os.replace first, in-place overwrite as fallback (file may be locked)
    replaced = False
    for attempt in range(5):
        try:
            os.replace(tmp, ASAR)
            replaced = True
            break
        except PermissionError:
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
                print("replace attempt %d failed: %r" % (attempt, e2))
                time.sleep(3)
    if not replaced:
        print("REPLACE FAILED - file locked (close the app and retry)")
        sys.exit(3)
    print("archive rewritten, data bytes = %d" % pos)

    # verify: reload and check patched content is present
    with io.open(ASAR, "rb") as f:
        blob2 = f.read()
    a2, b2, c2, d2 = struct.unpack("<IIII", blob2[:16])
    t2 = json.loads(blob2[16:16 + d2].decode("utf-8"))
    flag_hits = [0]
    slider_ok = [0]
    def walk2(node, prefix=""):
        if "files" in node:
            for name, ch in node["files"].items():
                walk2(ch, prefix + "/" + name if prefix else name)
        else:
            if "offset" in node and not node.get("unpacked"):
                off = 8 + b2 + int(node["offset"]); size = node.get("size", 0)
                seg = blob2[off:off + size]
                if M1 in seg:
                    flag_hits[0] += 1
                if b'"low","medium","high","xhigh","max","ultra"' in seg:
                    slider_ok[0] += 1
    walk2(t2)
    print("VERIFY: unpatched flag hits = %d" % flag_hits[0])
    print("VERIFY: ultra effort-list hits = %d" % slider_ok[0])

    # print new header hash (needed by patch_exe_hash.py)
    print("NEW_HEADER_SHA256 = %s" % hashlib.sha256(blob2[16:16 + d2]).hexdigest())


if __name__ == "__main__":
    main()
