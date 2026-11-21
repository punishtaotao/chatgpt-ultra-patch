# -*- coding: utf-8 -*-
r"""Verify per-file integrity of an asar archive against its own header.

Recomputes sha256 for every file stored inside the archive and compares it
with the integrity record in the header.  Run this after patch_asar.py to
make sure the rebuild is fully self-consistent.

Usage:
  python check_asar.py <path\to\app.asar> [max_report]

Importable: check_asar(asar_path) -> dict {checked, mismatches, ok}
"""
import io, os, sys, json, hashlib, struct


def check_asar(asar_path):
    ASAR = os.path.abspath(asar_path)
    if not os.path.isfile(ASAR):
        raise FileNotFoundError(ASAR)
    with io.open(ASAR, "rb") as f:
        head = f.read(16)
        a, b, c, d = struct.unpack("<IIII", head)
        js = f.read(d)
    data_start = 8 + b
    tree = json.loads(js.decode("utf-8"))
    res = {"checked": 0, "mismatches": []}

    def check(node, prefix=""):
        if "files" in node:
            for name, ch in node["files"].items():
                check(ch, prefix + "/" + name if prefix else name)
            return
        if "offset" not in node or node.get("unpacked"):
            return
        off = data_start + int(node["offset"])
        size = node["size"]
        with io.open(ASAR, "rb") as f:
            f.seek(off)
            data = f.read(size)
        actual = hashlib.sha256(data).hexdigest()
        intg = node.get("integrity", {})
        expected = intg.get("hash")
        res["checked"] += 1
        if actual != expected:
            res["mismatches"].append({
                "file": prefix, "size": size,
                "actual": actual, "expected": expected,
            })

    check(tree)
    res["ok"] = not res["mismatches"]
    return res


def main():
    if len(sys.argv) < 2:
        print("usage: python check_asar.py <path\\to\\app.asar> [max_report]")
        sys.exit(64)
    try:
        res = check_asar(sys.argv[1])
    except FileNotFoundError as e:
        print("ERROR: not found: %s" % e)
        sys.exit(2)
    print(json.dumps({
        "checked": res["checked"],
        "mismatch_count": len(res["mismatches"]),
        "ok": res["ok"],
        "first_mismatches": res["mismatches"][:int(sys.argv[2]) if len(sys.argv) > 2 else 20],
    }, ensure_ascii=False, indent=1))
    sys.exit(0 if res["ok"] else 1)


if __name__ == "__main__":
    main()
