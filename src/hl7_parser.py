# -*- coding: utf-8 -*-
"""HL7 v2.x message parser for Mindray lab machines.

Mindray BC-780 / BS-240Pro send ORU^R01 messages wrapped in MLLP framing:
    0x0B              — MLLP start (VT)
    <HL7 segments>    — separated by 0x0D (CR)
    0x1C 0x0D         — MLLP end (FS + CR)

OBX data types processed:
    NM  — numeric result  → goes into results list
    ED  — encapsulated data (base64 BMP images) → goes into images list
    IS / ST / ... — machine metadata, skipped
"""

import logging

logger = logging.getLogger(__name__)

MLLP_START = b"\x0b"
MLLP_END   = b"\x1c\x0d"


class HL7Parser:
    """Parse HL7 v2.x ORU^R01 messages into structured dicts."""

    def __init__(self, config=None):
        config = config or {}
        self.field_sep     = config.get("field_separator", "|")
        self.component_sep = config.get("component_separator", "^")
        self.repeat_sep    = config.get("repeat_separator", "~")

        obr = config.get("obr", {})
        self.obr_placer = obr.get("placer_order_field", 2)
        self.obr_filler = obr.get("filler_order_field", 3)

        obx = config.get("obx", {})
        self.obx_type  = 2   # OBX-2: data type (NM / IS / ST / ED …)
        self.obx_id    = obx.get("obs_id_field", 3)
        self.obx_value = obx.get("value_field", 5)
        self.obx_unit  = obx.get("units_field", 6)
        self.obx_range = obx.get("ref_range_field", 7)
        self.obx_flag  = obx.get("abnormal_flag_field", 8)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse_message(self, raw_text):
        """Parse a complete HL7 v2.x message.

        Returns dict:
            sending_app   : str  (MSH-3)
            message_id    : str  (MSH-10)
            message_type  : str  (MSH-9)
            patient_id    : str  (PID-3)
            sample_barcode: str  (OBR-2 or OBR-3)
            results       : list of result dicts (NM OBX only)
            images        : list of image dicts  (ED OBX only)
        """
        segments = [s.strip() for s in raw_text.replace("\n", "\r").split("\r") if s.strip()]

        parsed = {
            "sending_app":    "",
            "message_id":     "",
            "message_type":   "",
            "patient_id":     "",
            "sample_barcode": "",
            "results":        [],
            "images":         [],
        }

        current_barcode = ""
        obx_seq = 0

        for seg in segments:
            seg_id = seg[:3]

            if seg_id == "MSH":
                fields = seg.split(self.field_sep)
                parsed["sending_app"]  = self._get(fields, 2)
                parsed["message_type"] = self._get(fields, 8)
                parsed["message_id"]   = self._get(fields, 9)

            elif seg_id == "PID":
                fields = seg.split(self.field_sep)
                parsed["patient_id"] = self._get(fields, 3)

            elif seg_id == "OBR":
                fields = seg.split(self.field_sep)
                placer = self._get(fields, self.obr_placer)
                filler = self._get(fields, self.obr_filler)
                current_barcode = placer or filler
                if not parsed["sample_barcode"]:
                    parsed["sample_barcode"] = current_barcode

            elif seg_id == "OBX":
                fields = seg.split(self.field_sep)
                obx_seq += 1
                data_type = self._get(fields, self.obx_type).strip()
                obs_id    = self._get(fields, self.obx_id)

                if data_type == "NM":
                    test_code = self._extract_nm_code(obs_id)
                    value     = self._clean_value(self._get(fields, self.obx_value))
                    unit      = self._get(fields, self.obx_unit).strip()
                    ref_range = self._clean_ref_range(self._get(fields, self.obx_range))
                    flags     = self._clean_flag(self._get(fields, self.obx_flag))

                    if test_code and value not in ("", "****"):
                        parsed["results"].append({
                            "test_code":      test_code,
                            "value":          value,
                            "unit":           unit,
                            "ref_range":      ref_range,
                            "flags":          flags,
                            "sample_barcode": current_barcode or parsed["sample_barcode"],
                        })

                elif data_type == "ED":
                    # OBX-5 format: ^ContentType^Encoding^SubType^Data
                    # e.g. ^Image^BMP^Base64^iVBOR...
                    image = self._extract_ed_image(fields, obs_id, obx_seq)
                    if image:
                        parsed["images"].append(image)

                # IS / ST / CWE / ... → skip silently

        logger.debug(
            "Parsed HL7 %s from %s: barcode=%s, %d results, %d images",
            parsed["message_type"], parsed["sending_app"],
            parsed["sample_barcode"], len(parsed["results"]), len(parsed["images"]),
        )
        return parsed

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get(self, fields, index, default=""):
        try:
            v = fields[index] if index < len(fields) else default
            return v if v is not None else default
        except (IndexError, TypeError):
            return default

    def _extract_nm_code(self, obs_id):
        """Return human-readable code from OBX-3.

        '6690-2^WBC^LN'  → 'WBC'   (prefer component 2)
        'WBC^GB^'        → 'WBC'   (component 1 if component 2 empty)
        'WBC'            → 'WBC'
        """
        if not obs_id:
            return ""
        parts = [p.strip() for p in obs_id.split(self.component_sep)]
        # Prefer component 2 (short clinical name) over component 1 (LOINC)
        if len(parts) >= 2 and parts[1]:
            return parts[1]
        return parts[0] if parts[0] else ""

    def _clean_value(self, raw):
        """Normalise French decimal comma to dot: '11,10' → '11.10'."""
        return raw.strip().replace(",", ".")

    def _clean_ref_range(self, raw):
        """Normalise reference range comma decimals: '4,00-10,00' → '4.00-10.00'."""
        return raw.strip().replace(",", ".")

    def _clean_flag(self, raw):
        """Extract first flag from repeat list: 'H~N' → 'H', 'N' → 'N'."""
        first = raw.strip().split(self.repeat_sep)[0].strip()
        # Treat 'N' (normal) as empty — keeps logs clean
        return "" if first in ("N", "") else first

    def _extract_ed_image(self, fields, obs_id, seq):
        """Parse an ED OBX segment and return image dict or None.

        OBX-5 (index 5) format: ^Image^BMP^Base64^<data>
        """
        raw_value = self._get(fields, self.obx_value)
        if not raw_value:
            return None

        parts = raw_value.split(self.component_sep)
        # parts[0]='' parts[1]='Image' parts[2]='BMP' parts[3]='Base64' parts[4]=data
        if len(parts) < 5:
            return None

        encoding = parts[3].strip().lower()
        data     = parts[4].strip()
        if encoding != "base64" or not data:
            return None

        # Label: use the text part of OBX-3, e.g. 'WBC Histogram. BMP'
        obs_parts = obs_id.split(self.component_sep)
        label = obs_parts[1].strip() if len(obs_parts) >= 2 and obs_parts[1].strip() else obs_id

        return {
            "sequence": seq,
            "name":     label,
            "obx_code": obs_parts[0].strip(),
            "data_b64": data,          # raw base64 string
            "format":   parts[2].strip().upper(),  # 'BMP'
        }
