# -*- coding: utf-8 -*-
"""LIS Daemon — HL7/MLLP async TCP server.

Single TCP listener on port 5001. Both machines connect to this port.
Machine identification is by source IP (matched against dnd.lis.machine.ip_address).
If IP is not yet configured, falls back to MSH-3 (Sending Application) name matching.

Flow per connection:
    1. Accept TCP connection, note source IP
    2. Identify machine record from Odoo by IP
    3. Read MLLP frames: 0x0B ... 0x1C 0x0D
    4. Parse HL7 ORU^R01 message with HL7Parser
    5. Send MLLP ACK back to machine
    6. Call Odoo action_receive_result() for each OBX result
    7. Update machine online/offline status
"""

import asyncio
import logging
from datetime import datetime, timezone

from .hl7_parser import HL7Parser, MLLP_START, MLLP_END

logger = logging.getLogger(__name__)


class LisDaemon:
    """Async TCP server that receives HL7 results from lab machines."""

    def __init__(self, odoo_client, parser, config):
        self.odoo_client = odoo_client
        self.parser = parser
        self.config = config
        self.machines = []      # flat list of machine dicts from Odoo
        self.servers = []       # asyncio.Server instances
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Authenticate with Odoo, load machines, start TCP listener."""
        logger.info("Starting LIS daemon (HL7/MLLP)...")

        self.odoo_client.authenticate()
        await self._load_machines()

        port = self.config.get("listen_port", 5001)
        server = await asyncio.start_server(
            self._handle_connection,
            "0.0.0.0",
            port,
        )
        self.servers.append(server)
        self._running = True

        logger.info(
            "Listening on 0.0.0.0:%d — %d machine(s) configured",
            port, len(self.machines),
        )
        asyncio.create_task(self._refresh_loop())

    async def stop(self):
        """Stop all TCP servers gracefully."""
        self._running = False
        for server in self.servers:
            server.close()
            await server.wait_closed()
        logger.info("LIS daemon stopped")

    # ------------------------------------------------------------------
    # Machine config
    # ------------------------------------------------------------------

    async def _load_machines(self):
        """Reload active machine list from Odoo."""
        try:
            self.machines = self.odoo_client.load_machines()
            for m in self.machines:
                logger.info(
                    "  Machine: %s  id=%s  type=%s  ip=%s",
                    m["name"], m["id"], m["machine_type"],
                    m.get("ip_address") or "not set",
                )
        except Exception as e:
            logger.error("Failed to load machines from Odoo: %s", e)

    async def _refresh_loop(self):
        """Periodically refresh machine config from Odoo."""
        interval = self.config.get("machine_refresh_interval", 300)
        while self._running:
            await asyncio.sleep(interval)
            try:
                await self._load_machines()
            except Exception as e:
                logger.error("Machine refresh failed: %s", e)

    def _find_machine(self, client_ip, sending_app=""):
        """Find machine record by source IP, with fallback to MSH-3 name."""
        # Primary: exact IP match
        for m in self.machines:
            if m.get("ip_address") and m["ip_address"].strip() == client_ip:
                return m
        # Fallback: Sending Application (MSH-3) substring match against machine name
        if sending_app:
            sa_lower = sending_app.lower()
            for m in self.machines:
                if sa_lower in m.get("name", "").lower() or m.get("name", "").lower() in sa_lower:
                    logger.info(
                        "Machine identified by MSH-3 '%s' → '%s' (IP not configured)",
                        sending_app, m["name"],
                    )
                    return m
        return None

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def _handle_connection(self, reader, writer):
        """Handle one TCP connection — read MLLP frames until disconnect."""
        addr = writer.get_extra_info("peername")
        client_ip = addr[0] if addr else "unknown"
        logger.info("New connection from %s", client_ip)

        timeout  = self.config.get("tcp", {}).get("receive_timeout", 60)
        buf_size = self.config.get("tcp", {}).get("buffer_size", 65536)

        # Machine is identified after the first message (need MSH-3 for fallback)
        machine = self._find_machine(client_ip)
        if machine:
            self._mark_online(machine)

        buffer     = bytearray()
        in_message = False

        try:
            while True:
                try:
                    data = await asyncio.wait_for(reader.read(buf_size), timeout=timeout)
                except asyncio.TimeoutError:
                    logger.warning("Timeout on connection from %s — closing", client_ip)
                    break

                if not data:
                    logger.info("Connection closed by %s", client_ip)
                    break

                # Log raw bytes on first receive to diagnose protocol
                if not buffer and not in_message:
                    logger.warning(
                        "RAW first %d bytes from %s: hex=%s repr=%r",
                        len(data), client_ip,
                        data[:64].hex(), data[:64],
                    )

                for byte in data:
                    b = bytes([byte])

                    if b == MLLP_START:
                        buffer = bytearray()
                        in_message = True

                    elif in_message:
                        buffer.append(byte)

                        # MLLP end is last two bytes: 0x1C 0x0D
                        if len(buffer) >= 2 and buffer[-2:] == MLLP_END:
                            raw_text = buffer[:-2].decode("utf-8", errors="replace")

                            # Re-try machine lookup using MSH-3 now that we have message
                            if machine is None:
                                parsed_for_id = self.parser.parse_message(raw_text)
                                machine = self._find_machine(client_ip, parsed_for_id.get("sending_app", ""))
                                if machine:
                                    self._mark_online(machine)
                                else:
                                    logger.warning(
                                        "Unknown machine from %s (MSH-3=%s) — processing anyway",
                                        client_ip, parsed_for_id.get("sending_app"),
                                    )

                            # ACK immediately before processing (machine may time out waiting)
                            ack_msg = self._build_ack(raw_text)
                            writer.write(MLLP_START + ack_msg.encode("utf-8") + MLLP_END)
                            await writer.drain()

                            await self._process_message(raw_text, machine, client_ip)

                            buffer = bytearray()
                            in_message = False

        except Exception as e:
            logger.error("Error on connection from %s: %s", client_ip, e, exc_info=True)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            logger.info("Disconnected: %s", client_ip)
            if machine:
                self._mark_offline(machine)

    # ------------------------------------------------------------------
    # HL7 ACK builder
    # ------------------------------------------------------------------

    def _build_ack(self, raw_text):
        """Build a minimal HL7 AA (Application Accept) ACK."""
        msg_id    = "UNKNOWN"
        sender    = ""
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

        for line in raw_text.replace("\n", "\r").split("\r"):
            if line.startswith("MSH"):
                fields = line.split("|")
                sender = fields[2] if len(fields) > 2 else ""
                msg_id = fields[9] if len(fields) > 9 else "UNKNOWN"
                break

        return (
            f"MSH|^~\\&|LIS||{sender}||{timestamp}||ACK^R01|ACK{msg_id}|P|2.3\r"
            f"MSA|AA|{msg_id}\r"
        )

    # ------------------------------------------------------------------
    # Message processing
    # ------------------------------------------------------------------

    async def _process_message(self, raw_text, machine, client_ip):
        """Parse HL7 message and send each OBX result to Odoo."""
        machine_id   = machine["id"]   if machine else None
        machine_name = machine["name"] if machine else f"unknown@{client_ip}"

        try:
            parsed = self.parser.parse_message(raw_text)
        except Exception as e:
            logger.error("HL7 parse error (machine=%s): %s", machine_name, e, exc_info=True)
            return

        sample_barcode = parsed.get("sample_barcode", "")

        if not parsed["results"]:
            logger.warning(
                "No OBX results in message from %s (barcode=%s)",
                machine_name, sample_barcode,
            )
            return

        for result in parsed["results"]:
            # Each OBX may carry its own barcode if multiple OBR blocks
            barcode    = result.get("sample_barcode") or sample_barcode
            test_code  = result.get("test_code", "")
            value      = result.get("value", "")
            unit       = result.get("unit", "")
            flags      = result.get("flags", "")
            ref_range  = result.get("ref_range", "")

            logger.info(
                "Result  machine=%-15s  barcode=%-8s  code=%-6s  value=%s %s  flags=%s",
                machine_name, barcode, test_code, value, unit, flags or "N",
            )

            if not machine_id:
                logger.warning("Cannot send to Odoo — machine not identified yet")
                continue

            try:
                response = self.odoo_client.send_result(
                    machine_id     = machine_id,
                    sample_barcode = barcode,
                    test_code      = test_code,
                    value          = value,
                    unit           = unit       or None,
                    flags          = flags      or None,
                    ref_range      = ref_range  or None,
                    raw_message    = raw_text,
                )
                logger.info(
                    "Odoo  barcode=%s  code=%s  status=%s  log_id=%s",
                    barcode, test_code,
                    response.get("status"), response.get("log_id"),
                )
            except Exception as e:
                logger.error(
                    "Failed to send result to Odoo (barcode=%s code=%s): %s",
                    barcode, test_code, e, exc_info=True,
                )

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    def _mark_online(self, machine):
        try:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            self.odoo_client.set_machine_status(machine["id"], "online", last_seen=now)
        except Exception as e:
            logger.warning("Could not mark machine online: %s", e)

    def _mark_offline(self, machine):
        try:
            self.odoo_client.set_machine_status(machine["id"], "offline")
        except Exception as e:
            logger.warning("Could not mark machine offline: %s", e)
