"""NEW: stops what a hard-killed run (SIGKILL, power loss) left behind, Linux only.
  python3 -B -I harness/cleanup.py <work folder>
It reads <work>/.run/pids.json and ends a recorded process (and its process group) ONLY if all four hold: it started in this
work folder (/proc/<pid>/cwd is the work folder), it is not this script, its parent, or the group or session of this script,
it leads its own process group, and it is the recorded program. Any other process is printed by program name only and skipped. SIGTERM first, SIGKILL after
about 2 seconds, then it reports whether ports 5679 and 5780 are free. The recorded program is node (n8n) for the n8n entry and python for
the stand-in server. A recorded leader that is gone (or exited and not yet reaped) while its group still has members is reported, not signalled (check `ps -g`).
Exit code 1 if a process could not be stopped, was skipped as not the recorded program or unreadable, or a leaderless group remains, else 0.
The Postgres container is removed by the README's docker line."""
import json
import os
import pathlib
import signal
import socket
import sys
import time

PORTS = (5679, 5780)  # n8n and the stand-in server, as set in lib.py


def program(pid):  # program name only (never the arguments, they may hold secrets); "?" when unreadable
    try:
        raw = pathlib.Path("/proc/%d/cmdline" % pid).read_bytes().split(b"\0")[0]
        return raw.decode("utf-8", "replace").rsplit("/", 1)[-1][:40] or "?"
    except OSError:
        return "?"


def own_ids():  # this script, its parent, its process group and its session are never ended
    ids = {os.getpid(), os.getppid()}
    for f in ("getpgrp", "getsid"):
        if hasattr(os, f):
            ids.add(getattr(os, f)(*((0,) if f == "getsid" else ())))
    return ids


def port_free(port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) != 0


def comm_of(pid):  # /proc/<pid>/comm, the kernel's short program name; never printed
    try:
        return pathlib.Path("/proc/%d/comm" % pid).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def state_of(pid):  # /proc/<pid>/stat state letter ("Z" = exited, not yet reaped by its parent); "" when unreadable
    try:
        return pathlib.Path("/proc/%d/stat" % pid).read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return ""


def program_ok(key, comm):  # n8n runs as node, the stand-in server as python; any other key is not ours
    return comm in ("node", "n8n") if key == "n8n" else comm.startswith("python") if key == "fakes" else False


def group_alive(pid, killpg):  # a process group id is not reused while any member lives, so signal 0 on it is a safe test
    try:
        killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def clean(work, readlink=os.readlink, getpgid=getattr(os, "getpgid", None), killpg=getattr(os, "killpg", None),
          name=program, own=None, sleep=time.sleep, free=port_free, comm=comm_of, pstate=state_of):
    """Returns one report line per recorded pid, then one line about the ports."""
    work = os.path.realpath(str(work))
    own = own_ids() if own is None else own
    try:
        pids = json.loads((pathlib.Path(work) / ".run" / "pids.json").read_text(encoding="utf-8"))
    except OSError:
        return ["no readable .run/pids.json in the work folder: no process was recorded, nothing to stop"]
    except (ValueError, RecursionError):
        pids = None
    if not isinstance(pids, dict):
        return ["pids.json is not an object / unreadable: nothing stopped"]

    def verdict(key, pid):  # ("ours", "") / ("dead", "") / ("skip", why)
        try:
            if os.path.realpath(readlink("/proc/%d/cwd" % pid)) != work:
                return "skip", "SKIPPED, not started in this work folder (program: %r)" % name(pid)
            if pid in own:
                return "skip", "SKIPPED, it is this script, its parent, or its group or session"
            if getpgid(pid) != pid:
                return "skip", "SKIPPED, not a process group leader (program: %r)" % name(pid)
            if not program_ok(key, comm(pid)):
                return "skip", "SKIPPED, not the program recorded for this name (program: %r)" % name(pid)
        except PermissionError:
            return "skip", "cannot be read (another user? use sudo): skipped"
        except OSError:
            return "dead", ""
        return "ours", ""

    out = []
    for key, pid in pids.items():
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1 or pid > 4194304:
            continue  # 0 = already stopped by the harness; anything else is not a pid
        who = "%r pid %d" % (key, pid)
        state, why = verdict(key, pid)
        if state == "dead":
            if not group_alive(pid, killpg):
                out.append("%s: not running: skipped" % who)
            else:  # a zombie leader (exited, not yet reaped) still holds its group id
                out.append("%s: leader %s, group may still hold processes: check `ps -g %d`" % (who, "exited, not yet reaped" if pstate(pid) == "Z" else "gone", pid))
            continue
        if state == "skip":
            out.append("%s: %s" % (who, why))
            continue
        try:
            killpg(pid, signal.SIGTERM)
            sleep(2)
            if not group_alive(pid, killpg):
                out.append("%s: stopped (SIGTERM)" % who)
                continue
            killpg(pid, getattr(signal, "SIGKILL", 9))
            sleep(0.5)
            out.append("%s: could not stop it (still running after SIGKILL)" % who if group_alive(pid, killpg)
                       else "%s: stopped (SIGKILL after 2 s)" % who)
        except ProcessLookupError:
            out.append("%s: stopped (it was already gone)" % who)
        except OSError as e:
            out.append("%s: could not stop it (%s)" % (who, e.__class__.__name__))
    busy = [p for p in PORTS if not free(p)]
    out.append("ports %s: %s (check with `ss -ltn`)" % (" and ".join(map(str, PORTS)), "still in use: " + ", ".join(map(str, busy)) if busy else "free"))
    return out


def main(work, **kw):
    lines = clean(work, **kw)
    print("\n".join(lines))
    bad = ("could not stop", "leader gone", "leader exited", "SKIPPED, not the program", "cannot be read")  # a stranger must see these as a failure
    return 1 if any(b in x for x in lines for b in bad) else 0


if __name__ == "__main__":
    if os.name == "nt" or not os.path.isdir("/proc") or len(sys.argv) != 2:
        sys.exit("usage (Linux only): python3 -B -I harness/cleanup.py <work folder>")
    sys.exit(main(sys.argv[1]))
