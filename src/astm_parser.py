# -*- coding: utf-8 -*-
"""ASTM E1394 message parser.

Parses raw ASTM byte streams as sent by Mindray lab machines
(BS-240Pro, BC-series) into structured Python dicts.

ASTM frame structure:
    ENQ (0x05)           — initiation
    STX (0x02) + data    — start of frame
    ETX (0x03) or ETB (0x17) — end of frame / end of block
    EOT (0x04)           — end of transmission

Each frame contains records delimited by CR (0x0D):
    H — Header
    P — Patient
    O — Order (contains sample barcode)
    R — Result (contains test code + value)
    L — Terminator
"""

import logging
import re

logger = logging.getLogger(__name__)

# ASTM control characters
ENQ = b"\x05"
STX = b"\x02"
ETX = b"\x03"
ETB = b"\x17"
EOT = b"\x04"
ACK = b"\x06"
NAK = b"\x15"
CR = b"\x0d"
LF = b"\x0a"


class ASTMParser:
    """Parse ASTM E1394 messages using configurable field positions."""

    def __init__(self, config):
        """
        Args:
            config: dict from daemon.yml 'astm' section
        """
        self.field_delim = config.get("field_delimiter", "|")
        self.component_delim = config.get("component_delimiter", "^")
        self.repeat_delim = config.get("repeat_delimiter", "\\")

        self.record_types = config.get("record_types", {})
        self.order_config = config.get("order_record", {})
        self.result_config = config.get("result_record", {})
        self.patient_config = config.get("patient_record", {})

    def extract_data(self, raw_bytes):
        """Extract ASTM data from raw byte stream.

        Strips control characters (STX, ETX, ETB, CR, LF, frame numbers,
        checksums) and returns the clean text content.

        Args:
            raw_bytes: bytes received from TCP socket

        Returns:
            str: cleaned ASTM text
        """
        # Remove control characters
        text = raw_bytes.replace(STX, b"").replace(ETX, b"").replace(ETB, b"")
        text = text.replace(ENQ, b"").replace(EOT, b"").replace(ACK, b"").replace(NAK, b"")

        # Decode to string
        try:
            text = text.decode("ascii", errors="replace")
        except Exception:
            text = text.decode("latin-1", errors="replace")

        # Remove frame numbers (single digit at start of each frame)
        # and checksums (2 hex chars after ETX/ETB)
        # Frame format: <frame_num><data><CR><ETX><checksum><CR><LF>
        text = re.sub(r"[\x00-\x1f]", "\n", text)

        return text.strip()

    def parse_message(self, raw_bytes):
        """Parse a complete ASTM message into structured records.

        Args:
            raw_bytes: bytes - full ASTM transmission

        Returns:
            dict with keys:
                'raw': str - cleaned text
                'header': dict or None
                'patient': dict or None
                'orders': list of order dicts
                'results': list of result dicts
        """
        text = self.extract_data(raw_bytes)
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        parsed = {
            "raw": text,
            "header": None,
            "patient": None,
            "orders": [],
            "results": [],
        }

        for line in lines:
            if not line:
                continue

            # Get record type from first character (after optional frame number)
            record_line = line
            # Strip leading frame number (digit)
            if record_line and record_line[0].isdigit():
                record_line = record_line[1:]

            if not record_line:
                continue

            record_type = record_line[0]
            fields = record_line.split(self.field_delim)

            if record_type == self.record_types.get("header", "H"):
                parsed["header"] = self._parse_header(fields)
            elif record_type == self.record_types.get("patient", "P"):
                parsed["patient"] = self._parse_patient(fields)
            elif record_type == self.record_types.get("order", "O"):
                parsed["orders"].append(self._parse_order(fields))
            elif record_type == self.record_types.get("result", "R"):
                parsed["results"].append(self._parse_result(fields))
            elif record_type == self.record_types.get("terminator", "L"):
                pass  # end of message

        logger.debug(
            "Parsed ASTM message: %d orders, %d results",
            len(parsed["orders"]),
            len(parsed["results"]),
        )
        return parsed

    def _get_field(self, fields, index, default=""):
        """Safely get a field by 1-based index."""
        # ASTM fields are 1-indexed but the record type is at index 0
        # So field N in the spec is at list index N - 1
        try:
            return fields[index - 1] if index - 1 < len(fields) and index > 0 else default
        except (IndexError, TypeError):
            return default

    def _parse_header(self, fields):
        """Parse H (Header) record."""
        return {"type": "H", "raw_fields": fields}

    def _parse_patient(self, fields):
        """Parse P (Patient) record."""
        cfg = self.patient_config
        return {
            "type": "P",
            "patient_id": self._get_field(fields, cfg.get("patient_id_field", 4)),
            "name": self._get_field(fields, cfg.get("name_field", 5)),
            "dob": self._get_field(fields, cfg.get("dob_field", 7)),
            "sex": self._get_field(fields, cfg.get("sex_field", 8)),
        }

    def _parse_order(self, fields):
        """Parse O (Order) record — extract sample barcode."""
        cfg = self.order_config
        sample_id_raw = self._get_field(fields, cfg.get("sample_id_field", 3))

        # Sample ID might be wrapped in components: clean it
        sample_id = sample_id_raw.strip()

        return {
            "type": "O",
            "sample_id": sample_id,
            "raw_fields": fields,
        }

    def _parse_result(self, fields):
        """Parse R (Result) record — extract test code, value, unit, flags."""
        cfg = self.result_config

        # Test code field often contains component-separated data: ^^^GLU
        test_code_raw = self._get_field(fields, cfg.get("test_code_field", 3))
        test_code = self._extract_test_code(test_code_raw)

        value = self._get_field(fields, cfg.get("value_field", 4)).strip()
        unit = self._get_field(fields, cfg.get("unit_field", 5)).strip()
        ref_range = self._get_field(fields, cfg.get("ref_range_field", 6)).strip()
        flags = self._get_field(fields, cfg.get("flags_field", 7)).strip()

        return {
            "type": "R",
            "test_code": test_code,
            "value": value,
            "unit": unit,
            "ref_range": ref_range,
            "flags": flags,
            "raw_fields": fields,
        }

    def _extract_test_code(self, raw):
        """Extract test code from component-separated field.

        Examples:
            '^^^GLU'          → 'GLU'
            '^^^GLU^1^2'      → 'GLU'
            'GLU'             → 'GLU'
            '^^^WBC\\^^^RBC'  → 'WBC' (first code only — each R record has one)
        """
        if not raw:
            return ""

        # Take first repeat if multiple
        raw = raw.split(self.repeat_delim)[0]

        # Split by component delimiter and find non-empty parts
        parts = raw.split(self.component_delim)
        for part in parts:
            part = part.strip()
            if part:
                return part

        return raw.strip()
