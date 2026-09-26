{ pkgs, ... }:

{
  packages = [
    pkgs.git
  ];

  languages.python = {
    enable = true;
    venv.enable = true;
    venv.requirements = ./requirements.txt;
  };

  scripts = {
    repl.exec = ''
      PORT="''${PYPORTAL_PORT:-}"
      if [ -z "$PORT" ] || [ ! -e "$PORT" ]; then
        PORT="$(ls /dev/cu.usbmodem* /dev/ttyACM* 2>/dev/null | head -n 1)"
      fi
      if [ -z "$PORT" ]; then
        echo "No PyPortal serial port found"
        exit 1
      fi
      python -m serial.tools.miniterm --raw "$PORT" 115200
    '';

    # Send a soft reload (Ctrl+D) to the device
    reload.exec = ''
      PORT="''${PYPORTAL_PORT:-}"
      if [ -z "$PORT" ] || [ ! -e "$PORT" ]; then
        PORT="$(ls /dev/cu.usbmodem* /dev/ttyACM* 2>/dev/null | head -n 1)"
      fi
      if [ -z "$PORT" ]; then
        echo "No PyPortal serial port found"
        exit 1
      fi
      python - "$PORT" <<'PY'
import serial, time
import sys
s = serial.Serial(sys.argv[1], 115200, timeout=2)
time.sleep(0.2)
s.write(b'\x03\x03\x04')
time.sleep(0.5)
s.close()
print('Reloaded')
PY
    '';

    # Deploy a project: deploy <project-name>
    deploy.exec = ''
      set -e
      if [ -z "$1" ]; then
        echo "Usage: deploy <project-name>"
        echo ""
        echo "Available projects:"
        ls projects/
        exit 1
      fi
      if [ ! -f "projects/$1/code.py" ]; then
        echo "No code.py found in projects/$1/"
        exit 1
      fi
      PORT="''${PYPORTAL_PORT:-}"
      if [ -z "$PORT" ] || [ ! -e "$PORT" ]; then
        PORT="$(ls /dev/cu.usbmodem* /dev/ttyACM* 2>/dev/null | head -n 1)"
      fi
      if [ -z "$PORT" ]; then
        echo "No PyPortal serial port found"
        exit 1
      fi
      echo "Deploying $1..."
      # Use chunked exec write — mpremote cp fails when USB storage is read-only
      # (which happens when boot.py calls storage.remount("/", readonly=False))
      python - "$PORT" "projects/$1/code.py" <<'PY'
import pathlib, subprocess, sys
port, src = sys.argv[1], sys.argv[2]
data = pathlib.Path(src).read_bytes()
chunk_size = 1024
for i in range(0, len(data), chunk_size):
    chunk = data[i:i+chunk_size]
    mode = "wb" if i == 0 else "ab"
    script = f'f=open("/code.py",{repr(mode)});f.write({repr(chunk)});f.close()'
    r = subprocess.run(["mpremote","connect",port,"resume","exec",script],capture_output=True)
    if r.returncode != 0:
        sys.stderr.buffer.write(r.stdout)
        sys.stderr.buffer.write(r.stderr)
        print("Write failed at chunk", i // chunk_size, file=sys.stderr)
        sys.exit(1)
check = subprocess.run(
    ["mpremote", "connect", port, "resume", "exec", 'import os;print(os.stat("/code.py")[6])'],
    capture_output=True,
    text=True,
)
if check.returncode != 0 or str(len(data)) not in check.stdout:
    print("Upload size verification failed", check.stdout, check.stderr, file=sys.stderr)
    sys.exit(1)
subprocess.run(["mpremote", "connect", port, "reset"], check=True)
print("Verified", len(data), "bytes")
PY
      echo "Done"
    '';

    # Push shared files to device
    deploy-shared.exec = ''
      PORT="''${PYPORTAL_PORT:-}"
      if [ -z "$PORT" ] || [ ! -e "$PORT" ]; then
        PORT="$(ls /dev/cu.usbmodem* /dev/ttyACM* 2>/dev/null | head -n 1)"
      fi
      if [ -z "$PORT" ]; then
        echo "No PyPortal serial port found"
        exit 1
      fi
      echo "Pushing shared files..."
      mpremote connect "$PORT" cp shared/boot.py :/boot.py
      echo "Done"
    '';

    # Push secrets to device (shared/secrets.py -> /secrets.py)
    deploy-secrets.exec = ''
      if [ ! -f shared/secrets.py ]; then
        echo "No shared/secrets.py found — copy secrets.py.example and fill it in"
        exit 1
      fi
      PORT="''${PYPORTAL_PORT:-}"
      if [ -z "$PORT" ] || [ ! -e "$PORT" ]; then
        PORT="$(ls /dev/cu.usbmodem* /dev/ttyACM* 2>/dev/null | head -n 1)"
      fi
      if [ -z "$PORT" ]; then
        echo "No PyPortal serial port found"
        exit 1
      fi
      echo "Pushing secrets..."
      mpremote connect "$PORT" cp shared/secrets.py :/secrets.py
      python - "$PORT" <<'PY'
import serial, time
import sys
s = serial.Serial(sys.argv[1], 115200, timeout=2)
time.sleep(0.3)
s.write(b'\x03\x03\x04')
time.sleep(0.5)
s.close()
PY
      echo "Done"
    '';

    libs.exec = ''
      circup --path "''${CIRCUITPY_PATH:-/Volumes/CIRCUITPY}" "$@"
    '';
  };



  enterShell = ''
    echo "PyPortal dev environment"
    echo "  deploy <name>        — deploy a project via USB"
    echo "  deploy-shared   — push shared boot.py"
    echo "  deploy-secrets  — push shared/secrets.py to device"
    echo "  reload          — soft reload device"
    echo "  repl            — open CircuitPython REPL"
    echo "  libs            — manage CircuitPython libraries (circup)"
    echo ""
    echo "Projects: $(ls projects/ 2>/dev/null | tr '\n' ' ')"
  '';
}
