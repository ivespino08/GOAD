#!/usr/bin/env python3
"""
Simulated "git deploy key repository" service for Scenario12 / docker-10.

Not a real Git server -- this implements exactly the line-based AUTH/LIST/GET
protocol described in the scenario XML's git_deploy_key_repo flag-node-generator
access_instructions, over plain TCP (so `nc host port` works as the XML's own
step-1 instructions say). Values (credentials, port, endpoint path, banner) are
all taken directly from the XML's resolved_outputs/resolved_inputs -- nothing
here is guessed.

Protocol (one command per line, CRLF- or LF-terminated):
  AUTH <username> <password>   -> "OK authenticated" / "ERROR invalid credentials"
  LIST                          -> newline-separated list of available paths (post-auth)
  GET <path>                    -> raw file contents for a known path (post-auth)
  QUIT                          -> closes the connection
Anything else, or LIST/GET before a successful AUTH, gets an ERROR line.
"""
import socket
import threading

HOST = "0.0.0.0"
PORT = 19418
USERNAME = "user_db1031"
PASSWORD = "pass_2b4a4bceef4a"
BANNER = "SIMULATED-GIT-SERVICE ready (git-simulated-git)"

# Endpoint(path) -> local file inside the container
REPO_FILES = {
    "/repo/deploy.env": "/data/deploy.env",
}


def handle_client(conn, addr):
    authed = False
    try:
        conn.settimeout(120)
        f = conn.makefile("rwb")
        f.write((BANNER + "\r\n").encode())
        f.flush()

        while True:
            line = f.readline()
            if not line:
                break
            line = line.decode(errors="replace").strip()
            if not line:
                continue

            parts = line.split()
            cmd = parts[0].upper() if parts else ""

            if cmd == "AUTH" and len(parts) == 3:
                user, pwd = parts[1], parts[2]
                if user == USERNAME and pwd == PASSWORD:
                    authed = True
                    f.write(b"OK authenticated\r\n")
                else:
                    f.write(b"ERROR invalid credentials\r\n")

            elif cmd == "LIST":
                if not authed:
                    f.write(b"ERROR auth required\r\n")
                else:
                    f.write(("\r\n".join(REPO_FILES.keys()) + "\r\n").encode())

            elif cmd == "GET" and len(parts) == 2:
                if not authed:
                    f.write(b"ERROR auth required\r\n")
                else:
                    local_path = REPO_FILES.get(parts[1])
                    if local_path:
                        with open(local_path, "rb") as fh:
                            data = fh.read()
                        f.write(data)
                        if not data.endswith(b"\n"):
                            f.write(b"\r\n")
                    else:
                        f.write(b"ERROR not found\r\n")

            elif cmd == "QUIT":
                f.write(b"BYE\r\n")
                f.flush()
                break

            else:
                f.write(b"ERROR unknown command\r\n")

            f.flush()
    except Exception:
        pass
    finally:
        conn.close()


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((HOST, PORT))
    s.listen(20)
    while True:
        conn, addr = s.accept()
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    main()
