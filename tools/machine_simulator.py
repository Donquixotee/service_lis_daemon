#!/usr/bin/env python3
"""
Mindray lab machine simulator — sends HL7 v2.x ORU^R01 results over TCP/MLLP.

Usage:
    python tools/machine_simulator.py --host 192.9.101.27 --port 5001 \\
        --barcode LS001 \\
        --results "WBC:7.5:10*3/uL:4.0-11.0:N" "RBC:4.8:10*6/uL:4.2-5.4:N"

Result format: CODE:VALUE:UNIT:REF_RANGE:FLAG
  FLAG: N=normal  H=high  L=low  A=abnormal  HH=very high  LL=very low

Examples:
    # Biochemistry (port 5001)
    python tools/machine_simulator.py --host 192.9.101.27 --port 5001 \\
        --barcode LS001 \\
        --results "GLU:5.2:mmol/L:3.9-6.1:N" "CREA:88:umol/L:62-115:N"

    # Hematology (port 5002)
    python tools/machine_simulator.py --host 192.9.101.27 --port 5002 \\
        --barcode LS002 \\
        --results "WBC:7.5:10*3/uL:4.0-11.0:N" "HGB:14.5:g/dL:12.5-17.0:N"

    # Abnormal value
    python tools/machine_simulator.py --host 192.9.101.27 --port 5001 \\
        --barcode LS001 --results "GLU:25.0:mmol/L:3.9-6.1:HH"
"""

import argparse
import datetime
import socket
import sys
import time

MLLP_START = b"\x0b"
MLLP_END   = b"\x1c\x0d"
CR         = "\r"


def build_hl7_message(barcode: str, results: list[tuple],
                      sending_app: str = "SIMULATOR") -> str:
    now = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    msg_id = f"SIM{now}"

    segments = [
        f"MSH|^~\\&|{sending_app}|LAB||LIS|{now}||ORU^R01|{msg_id}|P|2.3",
        f"PID|||SIM001||PATIENT^SIMULATED||19800101|M",
        f"OBR|1|{barcode}||PANEL^Panel|||{now}",
    ]
    for i, (code, value, unit, ref_range, flag) in enumerate(results, start=1):
        segments.append(
            f"OBX|{i}|NM|{code}^{code}||{value}|{unit}|{ref_range}|{flag}|||F"
        )

    return CR.join(segments) + CR


def send_mllp(host: str, port: int, message: str, timeout: int = 10) -> str | None:
    raw = MLLP_START + message.encode("ascii") + MLLP_END
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(raw)
        # Read ACK
        buf = b""
        sock.settimeout(5)
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if MLLP_END in buf:
                    break
        except socket.timeout:
            pass
        return buf.decode("ascii", errors="replace") if buf else None


def parse_result_arg(s: str) -> tuple:
    parts = s.split(":")
    if len(parts) != 5:
        raise argparse.ArgumentTypeError(
            f"Invalid format '{s}'. Expected CODE:VALUE:UNIT:REF_RANGE:FLAG  "
            f"e.g. WBC:7.5:10*3/uL:4.0-11.0:N"
        )
    return tuple(parts)


def main():
    parser = argparse.ArgumentParser(
        description="Simulate a Mindray machine sending HL7 ORU^R01 results to the LIS daemon.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--host",    default="192.9.101.27", help="Daemon host (default: 192.9.101.27)")
    parser.add_argument("--port",    default=5001, type=int,  help="Daemon port (default: 5001)")
    parser.add_argument("--barcode", default="LS001",         help="Sample barcode — must exist in Odoo (default: LS001)")
    parser.add_argument("--app",     default="SIMULATOR",     help="HL7 MSH-3 sending application name (default: SIMULATOR)")
    parser.add_argument("--results", nargs="+", type=parse_result_arg, required=True,
                        metavar="CODE:VALUE:UNIT:REF_RANGE:FLAG",
                        help="Results to send. Format: CODE:VALUE:UNIT:REF_RANGE:FLAG")
    parser.add_argument("--timeout", default=10, type=int,   help="TCP timeout in seconds (default: 10)")
    parser.add_argument("--repeat",  default=1,  type=int,   help="Send N times")
    parser.add_argument("--delay",   default=1.0, type=float, help="Seconds between repeats (default: 1.0)")

    args = parser.parse_args()

    print(f"Simulator → {args.host}:{args.port}  barcode={args.barcode}")
    for code, value, unit, ref_range, flag in args.results:
        flag_note = {"H": " ⚠ HIGH", "L": " ⚠ LOW", "HH": " 🔴 VERY HIGH",
                     "LL": " 🔴 VERY LOW", "A": " ⚠ ABNORMAL"}.get(flag.upper(), "")
        print(f"  {code} = {value} {unit}  [{ref_range}]  {flag}{flag_note}")

    success = 0
    for i in range(args.repeat):
        if args.repeat > 1:
            print(f"\n--- Run {i+1}/{args.repeat} ---")
        try:
            msg = build_hl7_message(args.barcode, args.results, sending_app=args.app)
            print(f"\nSending HL7 message ({len(msg)} bytes)...")
            ack = send_mllp(args.host, args.port, msg, timeout=args.timeout)
            if ack:
                print(f"← ACK received")
                if "AA" in ack:
                    print("  Application Accept (AA) ✓")
                elif "AE" in ack:
                    print("  Application Error (AE) ✗")
                success += 1
            else:
                print("← No ACK (connection closed)")
        except ConnectionRefusedError:
            print(f"ERROR: Connection refused — is the daemon running on {args.host}:{args.port}?",
                  file=sys.stderr)
        except socket.timeout:
            print(f"ERROR: Timed out connecting to {args.host}:{args.port}", file=sys.stderr)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)

        if i < args.repeat - 1:
            time.sleep(args.delay)

    print(f"\nDone: {success}/{args.repeat} succeeded.")
    sys.exit(0 if success == args.repeat else 1)


if __name__ == "__main__":
    main()
