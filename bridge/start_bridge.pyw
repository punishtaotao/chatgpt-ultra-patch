# -*- coding: utf-8 -*-
"""Windowless starter for bridge/server.mjs on Windows (pythonw, no console).

Env vars consumed (all optional, see server.mjs):
  WB_CONNECT_MODULE           file:// path to dsh-workbuddy-connect module
  WORKBUDDY_ELECTRON_BIN      path to the CN desktop app exe
  WORKBUDDY_AI_ELECTRON_BIN   path to the international desktop app exe
  WB_BRIDGE_PORT              default 15790

Looks up node.exe from PATH (or NODE_EXE env).  stdout/stderr go to
bridge.log next to server.mjs.
"""
import os, sys, subprocess, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "bridge.log")
NODE = os.environ.get("NODE_EXE") or shutil.which("node")

if not NODE:
    msg = "node.exe not found on PATH; set NODE_EXE"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write("[start] " + msg + "\n")
    sys.exit(1)

cmd = [NODE, os.path.join(HERE, "server.mjs")]
out = open(LOG, "a", encoding="utf-8")
# CREATE_NO_WINDOW: never flash a console
flags = 0x08000000 if os.name == "nt" else 0
subprocess.Popen(cmd, stdout=out, stderr=out, cwd=HERE, creationflags=flags)
