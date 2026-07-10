"""
CSV telemetry for trace-app joystick jogging, for offline stutter analysis.

Writes one row per event to logs/joystick_csv/joystick_YYYYmmdd_HHMMSS.csv
(a new file per visit to the trace screen). Event types:

    sample  - every jog-loop tick: raw + deadzoned stick values, angle/magnitude
    send    - a $J jog command was written to serial (command text, distances, feed)
    ack     - GRBL responded to a jog command (round-trip latency)
    quit    - stick returned to centre and the jog was cancelled
    timeout - ack timeout hit, in-flight window force-reset
    pos     - GRBL status report changed the machine position

Every row also carries the latest machine state (m_state, MPos X/Y, GRBL's
reported feed, planner blocks / serial chars available), so any row can be
correlated against what the machine was actually doing. Actual velocity can be
derived in analysis from consecutive 'pos' rows (delta position / delta time).

Rows are buffered by the OS and flushed every few rows, so logging adds no
meaningful I/O jitter of its own on the Pi's SD card.

@author: Benji
"""
import csv
import math
import os
import sys
import time

from asmcnc import paths
from asmcnc.comms.logging_system.logging_system import Logger

LOG_DIR = os.path.join(paths.EASYCUT_SMARTBENCH_PATH, "logs", "joystick_csv")

HEADER = [
    "t", "t_rel", "event",
    # Stick state (sample rows; stick_x/y also on send rows)
    "raw_x", "raw_y", "stick_x", "stick_y", "angle_deg", "magnitude",
    # Jog command bookkeeping (send/ack/timeout rows)
    "in_flight", "cmd_feed", "cmd_x_dist", "cmd_y_dist", "ack_latency", "command",
    # Latest machine state (every row)
    "m_state", "m_x", "m_y", "grbl_feed", "blocks_avail", "chars_avail",
]


class JoystickCsvLogger(object):
    """One instance per trace screen; start()/stop() from on_enter/on_leave."""

    FLUSH_EVERY_N_ROWS = 20

    def __init__(self, serial_connection):
        self.s = serial_connection
        self._file = None
        self._writer = None
        self._t0 = 0.0
        self._rows_since_flush = 0
        self._last_logged_pos = None

    @property
    def active(self):
        return self._writer is not None

    def start(self):
        if self._writer:
            return
        try:
            if not os.path.isdir(LOG_DIR):
                os.makedirs(LOG_DIR)
            path = os.path.join(LOG_DIR, time.strftime("joystick_%Y%m%d_%H%M%S.csv"))
            # csv wants binary mode on py2 and newline='' on py3
            if sys.version_info[0] >= 3:
                self._file = open(path, "w", newline="")
            else:
                self._file = open(path, "wb")
            self._writer = csv.writer(self._file)
            self._writer.writerow(HEADER)
            self._t0 = time.time()
            self._rows_since_flush = 0
            self._last_logged_pos = None
            Logger.info("Joystick CSV: logging to {}".format(path))
        except (IOError, OSError):
            Logger.exception("Joystick CSV: could not open log file, logging disabled")
            self._file = None
            self._writer = None

    def stop(self):
        if not self._file:
            return
        try:
            self._file.close()
        except (IOError, OSError):
            Logger.exception("Joystick CSV: error closing log file")
        self._file = None
        self._writer = None

    # --- Event loggers -------------------------------------------------------

    def log_sample(self, raw_x, raw_y, stick_x, stick_y, in_flight):
        if not self._writer:
            return
        angle = magnitude = ""
        if stick_x or stick_y:
            angle = round(math.degrees(math.atan2(stick_y, stick_x)), 1)
            magnitude = round(math.hypot(stick_x, stick_y), 3)
        self._write_row("sample", raw_x=raw_x, raw_y=raw_y,
                        stick_x=round(stick_x, 4), stick_y=round(stick_y, 4),
                        angle_deg=angle, magnitude=magnitude, in_flight=in_flight)

    def log_send(self, command, stick_x, stick_y, cmd_feed, cmd_x_dist, cmd_y_dist, in_flight):
        self._write_row("send", command=command,
                        stick_x=round(stick_x, 4), stick_y=round(stick_y, 4),
                        cmd_feed=cmd_feed, cmd_x_dist=round(cmd_x_dist, 3),
                        cmd_y_dist=round(cmd_y_dist, 3), in_flight=in_flight)

    def log_ack(self, latency, in_flight):
        self._write_row("ack", ack_latency="" if latency is None else round(latency, 4),
                        in_flight=in_flight)

    def log_quit(self):
        self._write_row("quit")

    def log_timeout(self, in_flight):
        self._write_row("timeout", in_flight=in_flight)

    def log_position(self):
        """Call whenever a status report updates m_x/m_y; deduplicates itself."""
        if not self._writer:
            return
        pos = (self.s.m_x, self.s.m_y)
        if pos == self._last_logged_pos:
            return
        self._last_logged_pos = pos
        self._write_row("pos")

    # --- Row plumbing ---------------------------------------------------------

    def _write_row(self, event, **cells):
        if not self._writer:
            return
        try:
            now = time.time()
            cells["t"] = round(now, 4)
            cells["t_rel"] = round(now - self._t0, 4)
            cells["event"] = event
            cells["m_state"] = self.s.m_state
            cells["m_x"] = self.s.m_x
            cells["m_y"] = self.s.m_y
            cells["grbl_feed"] = self.s.feed_rate
            cells["blocks_avail"] = self.s.serial_blocks_available
            cells["chars_avail"] = self.s.serial_chars_available
            self._writer.writerow([cells.get(column, "") for column in HEADER])
            self._rows_since_flush += 1
            if self._rows_since_flush >= self.FLUSH_EVERY_N_ROWS:
                self._rows_since_flush = 0
                self._file.flush()
        except (IOError, OSError, ValueError):
            Logger.exception("Joystick CSV: write failed, disabling logging")
            self.stop()
