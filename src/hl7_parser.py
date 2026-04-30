# -*- coding: utf-8 -*-
"""HL7 v2.x message parser for Mindray lab machines.

Mindray BS-240Pro and BC-780 send ORU^R01 messages wrapped in MLLP framing:
    0x0B              — MLLP start (VT)
    <HL7 segments>    — separated by 0x0D (CR)
    0x1C 0x0D         — MLLP end (FS + CR)

Relevant segments:
    MSH  — Message header  (MSH-3 = sending application = machine identifier)
    PID  — Patient info
    OBR  — Observation request  (OBR-2/3 = sample barcode LS###)
    OBX  — Observation result   (one per parameter)
"""

import logging

logger = logging.getLogger(__name__)

# MLLP framing bytes
MLLP_START = b"\x0b"        # VT  — begin message
MLLP_END   = b"\x1c\x0d"   # FS + CR — end message


class HL7Parser:
    """Parse HL7 v2.x ORU^R01 messages into structured dicts."""

    def __init__(self, config=None):
        config = config or {}
        self.field_sep      = config.get("field_separator", "|")
        self.component_sep  = config.get("component_separator", "^")

        obr = config.get("obr", {})
        self.obr_placer = obr.get("placer_order_field", 2)
        self.obr_filler = obr.get("filler_order_field", 3)

        obx = config.get("obx", {})
        self.obx_id       = obx.get("obs_id_field", 3)
        self.obx_value    = obx.get("value_field", 5)
        self.obx_unit     = obx.get("units_field", 6)
        self.obx_range    = obx.get("ref_range_field", 7)
        self.obx_flag     = obx.get("abnormal_flag_field", 8)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse_message(self, raw_text):
        """Parse a complete HL7 v2.x message.

        Args:
            raw_text: str — HL7 message text (segments separated by \\r)

        Returns:
            dict:
                raw          : str
                sending_app  : str  (MSH-3, machine identifier)
                message_id   : str  (MSH-10)
                message_type : str  (MSH-9, e.g. "ORU^R01")
                patient_id   : str  (PID-3)
                sample_barcode: str (OBR-2 or OBR-3)
                results      : list of result dicts
        """
        segments = [s.strip() for s in raw_text.replace("\n", "\r").split("\r") if s.strip()]

        parsed = {
            "raw":            raw_text,
            "sending_app":    "",
            "message_id":     "",
            "message_type":   "",
            "patient_id":     "",
            "sample_barcode": "",
            "results":        [],
        }

        current_barcode = ""

        for seg in segments:
            seg_id = seg[:3]

            if seg_id == "MSH":
                # MSH is special: fields[1] is the encoding characters field
                # so MSH field N (1-indexed) is at list index N (not N-1)
                fields = seg.split(self.field_sep)
                parsed["sending_app"]  = self._get(fields, 2)   # MSH-3
                parsed["message_type"] = self._get(fields, 8)   # MSH-9
                parsed["message_id"]   = self._get(fields, 9)   # MSH-10

            elif seg_id == "PID":
                fields = seg.split(self.field_sep)
                parsed["patient_id"] = self._get(fields, 3)     # PID-3

            elif seg_id == "OBR":
                fields = seg.split(self.field_sep)
                placer = self._get(fields, self.obr_placer)
                filler = self._get(fields, self.obr_filler)
                current_barcode = placer or filler
                if not parsed["sample_barcode"]:
                    parsed["sample_barcode"] = current_barcode

            elif seg_id == "OBX":
                fields = seg.split(self.field_sep)
                obs_id    = self._get(fields, self.obx_id)
                test_code = self._extract_code(obs_id)
                value     = self._get(fields, self.obx_value).strip()
                unit      = self._get(fields, self.obx_unit).strip()
                ref_range = self._get(fields, self.obx_range).strip()
                flags     = self._get(fields, self.obx_flag).strip()

                if test_code:
                    parsed["results"].append({
                        "test_code":      test_code,
                        "value":          value,
                        "unit":           unit,
                        "ref_range":      ref_range,
                        "flags":          flags,
                        "sample_barcode": current_barcode or parsed["sample_barcode"],
                    })

        logger.debug(
            "Parsed HL7 %s from %s: barcode=%s, %d results",
            parsed["message_type"], parsed["sending_app"],
            parsed["sample_barcode"], len(parsed["results"]),
        )
        return parsed

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get(self, fields, index, default=""):
        """Get field by 1-based index (index 0 = segment ID)."""
        try:
            v = fields[index] if index < len(fields) else default
            return v if v is not None else default
        except (IndexError, TypeError):
            return default

    def _extract_code(self, obs_id):
        """Extract test code from OBX-3 component string.

        Examples:
            'WBC^GB^'  → 'WBC'
            'HGB'      → 'HGB'
            '^GB^'     → 'GB'   (fall back to second component)
        """
        if not obs_id:
            return ""
        parts = [p.strip() for p in obs_id.split(self.component_sep)]
        for part in parts:
            if part:
                return part
        return obs_id.strip()