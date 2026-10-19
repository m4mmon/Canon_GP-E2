#!/usr/bin/env python3
"""gpe2tool - remplacement libre de GPS Log File Utility pour le Canon GP-E2.

Fonctions : infos, liste et téléchargement des logs, effacement,
lecture et réglage de l'intervalle d'enregistrement.
La mise à jour du firmware n'est PAS gérée (opcodes bloqués).

Protocole : PTP sur USB (04A9:3251), opcodes Canon 0x91Ax.
Dépendance : pyusb (sudo apt install python3-usb). Lancer avec sudo sous WSL.
"""

import argparse
import datetime as dt
import json
import os
import struct
import sys
import time
import zipfile
from xml.sax.saxutils import escape

VID, PID = 0x04A9, 0x3251

# --- Codes PTP -------------------------------------------------------------
OC_GET_DEVICE_INFO = 0x1001
OC_OPEN_SESSION = 0x1002
OC_CLOSE_SESSION = 0x1003
OC_GET_PROP_DESC = 0x1014
OC_GET_PROP_VALUE = 0x1015
OC_SET_PROP_VALUE = 0x1016
OC_CANON_GET_DEVICE_INFO_EX = 0x9108
OC_CANON_SET_REMOTE_MODE = 0x9114
OC_GPE2_ERASE_ALL = 0x91A2
OC_GPE2_LIST = 0x91A3
OC_GPE2_READ = 0x91A4
OC_GPE2_MODE = 0x91A7
OC_GPE2_STATUS = 0x91AA

# Seuls ces opcodes peuvent partir vers le GP-E2. Les opcodes de mise à jour
# du firmware (0x91A0, 0x91A1, 0x91A8, 0x91A9) et les inconnus (0x91A5, 0x91A6)
# sont volontairement absents.
ALLOWED_OPCODES = {
    OC_GET_DEVICE_INFO, OC_OPEN_SESSION, OC_CLOSE_SESSION,
    OC_GET_PROP_DESC, OC_GET_PROP_VALUE, OC_SET_PROP_VALUE,
    OC_CANON_GET_DEVICE_INFO_EX, OC_CANON_SET_REMOTE_MODE,
    OC_GPE2_ERASE_ALL, OC_GPE2_LIST, OC_GPE2_READ, OC_GPE2_MODE, OC_GPE2_STATUS,
}
WRITABLE_PROPS = {0xD14F}  # seule propriété que l'outil a le droit d'écrire

RC_OK = 0x2001
RC_DEVICE_BUSY = 0x2019
RC_SESSION_ALREADY_OPEN = 0x201E
RC_NAMES = {
    0x2001: "OK", 0x2002: "General_Error", 0x2003: "Session_Not_Open",
    0x2005: "Operation_Not_Supported", 0x2006: "Parameter_Not_Supported",
    0x200A: "DeviceProp_Not_Supported", 0x2019: "Device_Busy",
    0x201C: "Invalid_DeviceProp_Format", 0x201D: "Invalid_DeviceProp_Value",
    0x201E: "Session_Already_Open",
}

PROP_INTERVAL = 0xD14F
PROP_SERIAL = 0xD16C
PROP_MODEL_ID = 0xD16D
PROP_READ_BLOCK = 0xD16E

CT_COMMAND, CT_DATA, CT_RESPONSE, CT_EVENT = 1, 2, 3, 4

DEBUG = False


def dbg(msg):
    if DEBUG:
        print(f"[debug] {msg}", file=sys.stderr)


def hexdump(b, limit=64):
    s = b[:limit].hex(" ")
    return s + (f" … (+{len(b) - limit} o)" if len(b) > limit else "")


class PTPError(Exception):
    pass


# --- Transport USB -----------------------------------------------------------
class USBTransport:
    """Accès brut aux endpoints bulk du GP-E2 via pyusb."""

    def __init__(self, timeout_ms=5000):
        try:
            import usb.core
            import usb.util
        except ImportError:
            sys.exit("pyusb manquant : sudo apt install python3-usb")
        self.usb = usb
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            sys.exit("GP-E2 introuvable (04a9:3251). Interrupteur sur ON ? "
                     "Attaché à WSL avec 'usbipd attach --wsl --busid …' ?")
        self.dev = dev
        self.timeout = timeout_ms
        cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]
        self.intf_num = intf.bInterfaceNumber
        try:
            if dev.is_kernel_driver_active(self.intf_num):
                dev.detach_kernel_driver(self.intf_num)
        except (NotImplementedError, usb.core.USBError):
            pass
        usb.util.claim_interface(dev, self.intf_num)
        E = usb.util
        self.ep_out = E.find_descriptor(intf, custom_match=lambda e:
            E.endpoint_direction(e.bEndpointAddress) == E.ENDPOINT_OUT and
            E.endpoint_type(e.bmAttributes) == E.ENDPOINT_TYPE_BULK)
        self.ep_in = E.find_descriptor(intf, custom_match=lambda e:
            E.endpoint_direction(e.bEndpointAddress) == E.ENDPOINT_IN and
            E.endpoint_type(e.bmAttributes) == E.ENDPOINT_TYPE_BULK)
        if self.ep_out is None or self.ep_in is None:
            sys.exit("Endpoints bulk introuvables sur le GP-E2.")
        dbg(f"endpoints OUT=0x{self.ep_out.bEndpointAddress:02x} "
            f"IN=0x{self.ep_in.bEndpointAddress:02x}")
        mps = self.ep_in.wMaxPacketSize or 512
        self.read_size = mps * 64  # 32 Ko : couvre un bloc de 8 Ko + en-tête

    def write(self, data):
        self.ep_out.write(data, self.timeout)

    def read(self):
        return bytes(self.ep_in.read(self.read_size, self.timeout))

    def close(self):
        try:
            self.usb.util.release_interface(self.dev, self.intf_num)
            self.usb.util.dispose_resources(self.dev)
        except Exception:
            pass


# --- Couche PTP --------------------------------------------------------------
class PTPSession:
    def __init__(self, transport):
        self.t = transport
        self.tid = 0

    def _send_container(self, ctype, code, payload):
        pkt = struct.pack("<IHHI", 12 + len(payload), ctype, code, self.tid) + payload
        dbg(f"→ type={ctype} code=0x{code:04x} tid={self.tid} {hexdump(pkt)}")
        self.t.write(pkt)

    def _recv_container(self):
        buf = b""
        while not buf:            # ignore les paquets de longueur nulle (ZLP)
            buf = self.t.read()
        if len(buf) < 12:
            raise PTPError(f"conteneur trop court : {buf.hex()}")
        length = struct.unpack_from("<I", buf)[0]
        while len(buf) < length:
            buf += self.t.read()
        buf = buf[:length]
        ctype, code, tid = struct.unpack_from("<HHI", buf, 4)
        dbg(f"← type={ctype} code=0x{code:04x} tid={tid} {hexdump(buf)}")
        return ctype, code, tid, buf[12:]

    def transaction(self, opcode, params=(), data_out=None, accept=(RC_OK,)):
        """Envoie une opération. Renvoie (code_réponse, params_réponse, données)."""
        if opcode not in ALLOWED_OPCODES:
            raise PTPError(f"opcode 0x{opcode:04x} interdit par l'outil")
        if opcode == OC_SET_PROP_VALUE and (not params or params[0] not in WRITABLE_PROPS):
            raise PTPError("écriture de propriété non autorisée")
        self._send_container(CT_COMMAND, opcode,
                             b"".join(struct.pack("<I", p) for p in params))
        if data_out is not None:
            self._send_container(CT_DATA, opcode, data_out)
        data = b""
        while True:
            ctype, code, tid, payload = self._recv_container()
            if tid != self.tid and opcode != OC_OPEN_SESSION:
                raise PTPError(f"transaction ID inattendu {tid} (attendu {self.tid})")
            if ctype == CT_DATA:
                data += payload
            elif ctype == CT_RESPONSE:
                rparams = struct.unpack(f"<{len(payload) // 4}I", payload[:len(payload) // 4 * 4])
                break
            elif ctype == CT_EVENT:
                continue
            else:
                raise PTPError(f"type de conteneur inconnu {ctype}")
        self.tid += 1
        if code not in accept:
            raise PTPError(f"opération 0x{opcode:04x} : réponse 0x{code:04x} "
                           f"({RC_NAMES.get(code, '?')})")
        return code, rparams, data

    def open(self):
        self.tid = 0
        self.transaction(OC_OPEN_SESSION, (1,), accept=(RC_OK, RC_SESSION_ALREADY_OPEN))

    def close(self):
        try:
            self.transaction(OC_CLOSE_SESSION)
        except Exception as e:
            dbg(f"CloseSession : {e}")


# --- Décodage des structures PTP ---------------------------------------------
class Reader:
    def __init__(self, b):
        self.b, self.o = b, 0

    def u8(self):
        v = self.b[self.o]; self.o += 1; return v

    def u16(self):
        v = struct.unpack_from("<H", self.b, self.o)[0]; self.o += 2; return v

    def u32(self):
        v = struct.unpack_from("<I", self.b, self.o)[0]; self.o += 4; return v

    def string(self):
        n = self.u8()
        s = self.b[self.o:self.o + 2 * n].decode("utf-16-le", "replace").rstrip("\x00")
        self.o += 2 * n
        return s

    def array16(self):
        return [self.u16() for _ in range(self.u32())]

    def value(self, dtype):
        if dtype == 0x0002: return self.u8()
        if dtype == 0x0004: return self.u16()
        if dtype == 0x0006: return self.u32()
        if dtype == 0xFFFF: return self.string()
        raise PTPError(f"type de propriété non géré 0x{dtype:04x}")


def parse_device_info(b):
    r = Reader(b)
    info = {"std_version": r.u16(), "vendor_ext_id": r.u32(),
            "vendor_ext_version": r.u16(), "vendor_ext_desc": r.string(),
            "functional_mode": r.u16(), "operations": r.array16(),
            "events": r.array16(), "properties": r.array16(),
            "capture_formats": r.array16(), "image_formats": r.array16()}
    info["manufacturer"] = r.string()
    info["model"] = r.string()
    info["device_version"] = r.string()
    info["serial"] = r.string()
    return info


def parse_prop_desc(b):
    r = Reader(b)
    d = {"code": r.u16(), "type": r.u16(), "writable": bool(r.u8())}
    d["default"] = r.value(d["type"])
    d["current"] = r.value(d["type"])
    form = r.u8()
    d["form"] = form
    if form == 1:
        d["range"] = (r.value(d["type"]), r.value(d["type"]), r.value(d["type"]))
    elif form == 2:
        d["enum"] = [r.value(d["type"]) for _ in range(r.u16())]
    return d


# --- Liste des logs (0x91A3) -------------------------------------------------
LIST_ENTRY_SIZE = 44
RECORD_SIZE = 32


class LogEntry:
    def __init__(self, raw):
        self.raw = raw
        self.handle = struct.unpack_from("<I", raw, 0)[0]
        self.name = raw[4:20].split(b"\x00")[0].decode("ascii", "replace")
        self.size = struct.unpack_from("<I", raw, 20)[0]
        d, m, y = raw[24], raw[25], raw[26]
        self.date = f"{d:02d}/{m:02d}/20{y:02d}"
        self.start = "{:02d}:{:02d}:{:02d}".format(*raw[28:31])
        self.end = "{:02d}:{:02d}:{:02d}".format(*raw[36:39])

    @property
    def points(self):
        return self.size // RECORD_SIZE

    @property
    def stem(self):
        return os.path.splitext(self.name)[0]


def parse_list(b):
    count = struct.unpack_from("<I", b, 0)[0]
    entries = [LogEntry(b[4 + i * LIST_ENTRY_SIZE: 4 + (i + 1) * LIST_ENTRY_SIZE])
               for i in range(count)]
    return entries


# --- Décodage des points (format binaire 32 octets) --------------------------
# Octet de drapeaux (octet 1), validé le 18/10/2026 avec l'émulateur de GP-E2 :
#   bit 0 (0x01) : altitude négative
#   bit 1 (0x02) : rôle inconnu (toujours 1 sur les vraies traces, sans effet
#                  sur la position dans GPS Log File Utility)
#   bit 2 (0x04) : 1 = Est, 0 = Ouest
#   bit 3 (0x08) : 1 = Nord, 0 = Sud
#   bits 4-7     : ignorés par GPS Log File Utility (toujours 0 sur les vraies traces)
FLAG_ALT_NEG = 0x01
FLAG_UNKNOWN1 = 0x02
FLAG_EAST = 0x04
FLAG_NORTH = 0x08


class Point:
    __slots__ = ("flags", "day", "month", "year", "h", "m", "s", "ms",
                 "lat_d", "lat_m", "lat_f", "lon_d", "lon_m", "lon_f",
                 "alt", "sats", "hdop_i", "hdop_d", "unk24")

    @classmethod
    def decode(cls, r):
        if len(r) != RECORD_SIZE or r[0] != 0x5A or r[31] != 0xA5:
            raise ValueError("enregistrement invalide (marqueurs 5A/A5 absents)")
        p = cls()
        p.flags = r[1]
        p.day, p.month, p.year = r[2], r[3], r[4]
        p.h, p.m, p.s = r[5], r[6], r[7]
        p.ms = struct.unpack_from("<H", r, 8)[0]
        p.lat_d, p.lat_m = r[10], r[11]
        p.lat_f = struct.unpack_from("<H", r, 12)[0]
        p.lon_d = struct.unpack_from("<H", r, 14)[0]
        p.lon_m = r[16]
        p.lon_f = struct.unpack_from("<H", r, 17)[0]
        # Valeurs impossibles (lat > 99, lon > 999) : GPS Log File Utility ne garde
        # que les 2 (lat) / 3 (lon) premiers chiffres ; on fait pareil.
        p.lat_d = int(str(p.lat_d)[:2])
        p.lon_d = int(str(p.lon_d)[:3])
        alt = struct.unpack_from("<H", r, 19)[0]
        p.alt = -alt if p.flags & FLAG_ALT_NEG else alt
        p.sats = r[21]
        p.hdop_i, p.hdop_d = r[22], r[23]
        p.unk24 = r[24]
        return p

    @property
    def ns(self):
        return "N" if self.flags & FLAG_NORTH else "S"

    @property
    def ew(self):
        return "E" if self.flags & FLAG_EAST else "W"

    @property
    def lat_deg(self):
        v = self.lat_d + (self.lat_m + self.lat_f / 10000) / 60
        return v if self.ns == "N" else -v

    @property
    def lon_deg(self):
        v = self.lon_d + (self.lon_m + self.lon_f / 10000) / 60
        return v if self.ew == "E" else -v

    @property
    def iso_time(self):
        return (f"20{self.year:02d}-{self.month:02d}-{self.day:02d}T"
                f"{self.h:02d}:{self.m:02d}:{self.s:02d}.{self.ms:03d}Z")


def decode_points(raw):
    if len(raw) % RECORD_SIZE:
        print(f"⚠ taille {len(raw)} non multiple de 32 : fin tronquée ignorée",
              file=sys.stderr)
    pts, odd_flags = [], set()
    for i in range(0, len(raw) - len(raw) % RECORD_SIZE, RECORD_SIZE):
        p = Point.decode(raw[i:i + RECORD_SIZE])
        if p.flags & 0xF0 or not p.flags & FLAG_UNKNOWN1:
            odd_flags.add(p.flags)
        pts.append(p)
    if odd_flags:
        print("⚠ drapeaux inhabituels (bit 1 à 0 ou bits 4-7 à 1) : "
              + ", ".join(f"0x{f:02x}" for f in sorted(odd_flags))
              + " — décodés comme GPS Log File Utility. Garde le .bin (--raw) !",
              file=sys.stderr)
    return pts


def nmea_checksum(s):
    c = 0
    for ch in s:
        c ^= ord(ch)
    return f"{c:02X}"


def to_canon_log(points, serial):
    """Reproduit le .log de GPS Log File Utility (en-tête + paires GGA/RMC, CRLF)."""
    out = [f"@CanonGPS/ver1.0/wgs-84/Canon GP-E2/{serial}"]
    for p in points:
        t = f"{p.h:02d}{p.m:02d}{p.s:02d}.{p.ms:03d}"
        lat = f"{p.lat_d:02d}{p.lat_m:02d}.{p.lat_f:04d}"
        lon = f"{p.lon_d:03d}{p.lon_m:02d}.{p.lon_f:04d}"
        date = f"{p.day:02d}{p.month:02d}{p.year:02d}"
        gga = (f"GPGGA,{t},{lat},{p.ns},{lon},{p.ew},1,{p.sats:02d},"
               f"{p.hdop_i}.{p.hdop_d},{p.alt:.1f},M,,,,0000")
        rmc = f"GPRMC,{t},A,{lat},{p.ns},{lon},{p.ew},,,{date},,,A"
        out.append(f"${gga}*{nmea_checksum(gga)}")
        out.append(f"${rmc}*{nmea_checksum(rmc)}")
    return ("\r\n".join(out) + "\r\n").encode("ascii")


def _zip_kml(kml, canon_like):
    """Emballe doc.kml dans un KMZ. canon_like : stocké sans compression, en-têtes
    « MS-DOS » version 1.0, comme GPS Log File Utility."""
    import io
    buf = io.BytesIO()
    info = zipfile.ZipInfo("doc.kml", date_time=dt.datetime.now().timetuple()[:6])
    if canon_like:
        info.compress_type = zipfile.ZIP_STORED
        info.create_system, info.create_version, info.extract_version = 0, 10, 10
        info.external_attr = 0
    else:
        info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(info, kml.encode("utf-8"))
        if canon_like:
            # writestr() impose des droits Unix (0600) quand external_attr vaut 0 ;
            # Canon met 0. Le répertoire central est écrit à la fermeture : on corrige ici.
            z.infolist()[0].external_attr = 0
    return buf.getvalue()


def to_kmz_canon(points, log_name):
    """KMZ identique (contenu) à celui de GPS Log File Utility : un seul tracé."""
    coords = "".join(f"{p.lon_deg:.10f},{p.lat_deg:.10f}\n" for p in points)
    kml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<kml xmlns="http://www.opengis.net/kml/2.2" '
           'xmlns:gx="http://www.google.com/kml/ext/2.2">\n'
           '<Document>\n'
           '<Style id="myLineStyle">\n'
           '<LineStyle><color>7f0000ff</color><width>10</width></LineStyle>\n'
           '</Style>\n'
           '<Placemark>\n'
           f'<name>{escape(log_name)}</name>\n'
           '<styleUrl>#myLineStyle</styleUrl>\n'
           '<LineString>\n'
           '<extrude>0</extrude><tessellate>1</tessellate>'
           '<gx:altitudeMode>clampToGround</gx:altitudeMode>\n'
           '<coordinates>\n'
           f'{coords}'
           '</coordinates>\n'
           '</LineString>\n'
           '</Placemark>\n'
           '</Document>\n'
           '</kml>\n')
    return _zip_kml(kml, canon_like=True)


def to_kmz(points, name):
    coords = "\n".join(f"{p.lon_deg:.7f},{p.lat_deg:.7f},{p.alt}" for p in points)
    track = "\n".join(f"<when>{p.iso_time}</when>" for p in points)
    track += "\n" + "\n".join(
        f"<gx:coord>{p.lon_deg:.7f} {p.lat_deg:.7f} {p.alt}</gx:coord>" for p in points)
    first, last = points[0], points[-1]

    def pin(label, p):
        return (f"<Placemark><name>{escape(label)}</name>"
                f"<TimeStamp><when>{p.iso_time}</when></TimeStamp>"
                f"<Point><coordinates>{p.lon_deg:.7f},{p.lat_deg:.7f},{p.alt}"
                f"</coordinates></Point></Placemark>")

    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">
<Document>
<name>{escape(name)}</name>
<Style id="trace"><LineStyle><color>ff0000ff</color><width>3</width></LineStyle></Style>
<Placemark><name>Trace</name><styleUrl>#trace</styleUrl>
<LineString><tessellate>1</tessellate><altitudeMode>clampToGround</altitudeMode>
<coordinates>
{coords}
</coordinates></LineString></Placemark>
<Placemark><name>Parcours horodaté</name><visibility>0</visibility>
<gx:Track><altitudeMode>clampToGround</altitudeMode>
{track}
</gx:Track></Placemark>
{pin("Début " + first.iso_time, first)}
{pin("Fin " + last.iso_time, last)}
</Document>
</kml>
"""
    return _zip_kml(kml, canon_like=False)


# --- Le GP-E2 ----------------------------------------------------------------
class GPE2:
    def __init__(self, transport):
        self.ptp = PTPSession(transport)
        self.transport = transport
        self.info = None

    def __enter__(self):
        self.ptp.open()
        _, _, di = self.ptp.transaction(OC_GET_DEVICE_INFO)
        self.info = parse_device_info(di)
        # Même initialisation que GPS Log File Utility
        self.ptp.transaction(OC_CANON_GET_DEVICE_INFO_EX)
        self.ptp.transaction(OC_CANON_SET_REMOTE_MODE, (1,))
        _, p, _ = self.ptp.transaction(OC_GPE2_MODE)
        mode = p[0] if p else None
        if mode != 0:
            raise PTPError(f"le GP-E2 n'est pas en mode normal (0x91A7 = {mode}) : "
                           "mode mise à jour ? Débranche-le et rebranche-le.")
        return self

    def __exit__(self, *exc):
        self.ptp.close()
        self.transport.close()

    def get_prop(self, code):
        _, _, d = self.ptp.transaction(OC_GET_PROP_VALUE, (code,))
        return d

    def prop_u32(self, code):
        return struct.unpack("<I", self.get_prop(code)[:4])[0]

    def serial(self):
        s = self.info.get("serial") if self.info else ""
        if not s:
            try:
                s = Reader(self.get_prop(PROP_SERIAL)).string()
            except PTPError:
                s = ""
        return s

    def interval_desc(self):
        _, _, d = self.ptp.transaction(OC_GET_PROP_DESC, (PROP_INTERVAL,))
        return parse_prop_desc(d)

    def set_interval(self, seconds):
        desc = self.interval_desc()
        allowed = desc.get("enum", [])
        if seconds not in allowed:
            raise PTPError(f"valeur {seconds} refusée ; valeurs possibles : {allowed}")
        self.ptp.transaction(OC_SET_PROP_VALUE, (PROP_INTERVAL,),
                             data_out=struct.pack("<I", seconds))

    def list_logs(self):
        _, _, d = self.ptp.transaction(OC_GPE2_LIST)
        return parse_list(d)

    def read_log(self, entry, progress=None):
        block = self.prop_u32(PROP_READ_BLOCK) or 0x2000
        data, offset = bytearray(), 0
        while offset < entry.size:
            want = min(block, entry.size - offset)
            _, p, chunk = self.ptp.transaction(OC_GPE2_READ, (entry.handle, offset, want))
            n = p[0] if p else len(chunk)
            if n == 0:
                raise PTPError(f"lecture vide à l'offset {offset}")
            data += chunk[:n]
            offset += n
            if progress:
                progress(offset, entry.size)
        return bytes(data)

    def erase_all(self, timeout_s=120, progress=None):
        self.ptp.transaction(OC_GPE2_ERASE_ALL)
        t0 = time.time()
        while True:
            code, _, _ = self.ptp.transaction(OC_GPE2_STATUS,
                                              accept=(RC_OK, RC_DEVICE_BUSY))
            if code == RC_OK:
                return time.time() - t0
            if time.time() - t0 > timeout_s:
                raise PTPError("effacement : délai dépassé (toujours occupé)")
            if progress:
                progress(time.time() - t0)
            time.sleep(0.5)


# --- Suivi des exports -------------------------------------------------------
STATE_FILE = ".gpe2_exports.json"


def load_state(outdir):
    try:
        with open(os.path.join(outdir, STATE_FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(outdir, state):
    with open(os.path.join(outdir, STATE_FILE), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def already_exported(state, serial, entry):
    rec = state.get(serial, {}).get(entry.name)
    return bool(rec) and rec.get("size") == entry.size


# --- Commandes ---------------------------------------------------------------
def make_transport(args):
    return USBTransport()


def cmd_info(args):
    with GPE2(make_transport(args)) as g:
        i = g.info
        ver = i["device_version"]
        print(f"Fabricant   : {i['manufacturer']}")
        print(f"Modèle      : {i['model']}")
        print(f"Firmware    : {ver.split('-', 1)[-1]}  (brut : {ver})")
        print(f"N° de série : {g.serial()}")
        if args.debug:
            print(f"ID modèle   : 0x{g.prop_u32(PROP_MODEL_ID):08X}")
            print("Opérations  : " + " ".join(f"{o:04X}" for o in i["operations"]))


def print_list(entries, state, serial):
    if not entries:
        print("Aucun log dans le GP-E2.")
        return
    print(f"{'N°':>3}  {'Fichier':<14} {'Date':<10}  {'Début':<8} {'Fin':<8} "
          f"{'Points':>6}  État")
    for n, e in enumerate(entries, 1):
        st = "déjà exporté" if already_exported(state, serial, e) else "nouveau"
        print(f"{n:>3}  {e.name:<14} {e.date:<10}  {e.start:<8} {e.end:<8} "
              f"{e.points:>6}  {st}")


def cmd_list(args):
    with GPE2(make_transport(args)) as g:
        entries = g.list_logs()
        print_list(entries, load_state(args.output), g.serial())
        if args.debug:
            for e in entries:
                print(f"  handle=0x{e.handle:08X} {e.raw.hex(' ')}")


def select_entries(entries, wanted):
    sel = []
    for w in wanted:
        if w.isdigit() and 1 <= int(w) <= len(entries) and len(w) < 4:
            sel.append(entries[int(w) - 1])
            continue
        stem = os.path.splitext(w)[0]
        match = [e for e in entries if e.stem == stem]
        if not match:
            sys.exit(f"log introuvable : {w}")
        sel.extend(match)
    return sel


def export(raw, name_stem, serial, outdir, want_raw, want_kmz, kmz_detail=False):
    written = []
    if want_raw:
        path = os.path.join(outdir, name_stem + ".bin")
        with open(path, "wb") as f:
            f.write(raw)
        written.append(path)
    points = decode_points(raw)
    path = os.path.join(outdir, name_stem + ".log")
    with open(path, "wb") as f:
        f.write(to_canon_log(points, serial))
    written.append(path)
    if want_kmz and points:
        path = os.path.join(outdir, name_stem + ".kmz")
        with open(path, "wb") as f:
            f.write(to_kmz(points, name_stem) if kmz_detail
                    else to_kmz_canon(points, name_stem + ".log"))
        written.append(path)
    return points, written


def cmd_download(args):
    os.makedirs(args.output, exist_ok=True)
    state = load_state(args.output)
    with GPE2(make_transport(args)) as g:
        serial = g.serial()
        entries = g.list_logs()
        if args.files:
            todo = select_entries(entries, args.files)
        elif args.force:
            todo = entries
        else:
            todo = [e for e in entries if not already_exported(state, serial, e)]
        if not todo:
            print("Rien à télécharger (tout est déjà exporté ; --force pour tout reprendre).")
            return
        for e in todo:
            def prog(done, total, name=e.name):
                print(f"\r{name} : {done * 100 // total:3d} %", end="", flush=True)
            raw = g.read_log(e, prog)
            print()
            points, files = export(raw, e.stem, serial, args.output,
                                   args.raw or args.debug,
                                   args.kmz or args.kmz_detaille, args.kmz_detaille)
            state.setdefault(serial, {})[e.name] = {
                "size": e.size, "points": len(points),
                "exported": dt.datetime.now().isoformat(timespec="seconds")}
            save_state(args.output, state)
            print("  → " + ", ".join(files))


def cmd_erase(args):
    with GPE2(make_transport(args)) as g:
        serial = g.serial()
        entries = g.list_logs()
        state = load_state(args.output)
        print_list(entries, state, serial)
        if not entries:
            return
        pending = [e.name for e in entries if not already_exported(state, serial, e)]
        if pending:
            print(f"⚠ Non exportés (d'après {args.output}) : {', '.join(pending)}")
        if not args.yes:
            rep = input("Effacer TOUS les logs du GP-E2 ? Tape 'effacer' pour confirmer : ")
            if rep.strip().lower() != "effacer":
                print("Annulé.")
                return
        dur = g.erase_all(progress=lambda t: print(f"\rEffacement… {t:4.1f} s",
                                                     end="", flush=True))
        print(f"\rEffacement terminé en {dur:.1f} s.")
        print(f"Logs restants : {len(g.list_logs())}")


def cmd_interval(args):
    with GPE2(make_transport(args)) as g:
        if args.value is None:
            d = g.interval_desc()
            print(f"Intervalle : {d['current']} s  (usine : {d['default']} s)")
            print("Valeurs possibles : " + ", ".join(str(v) for v in d.get("enum", [])))
        else:
            g.set_interval(args.value)
            d = g.interval_desc()
            print(f"Intervalle réglé : {d['current']} s")


def cmd_decode(args):
    """Convertit un .bin déjà téléchargé, sans GP-E2 branché."""
    with open(args.bin, "rb") as f:
        raw = f.read()
    outdir = args.output or os.path.dirname(os.path.abspath(args.bin))
    stem = os.path.splitext(os.path.basename(args.bin))[0]
    points, files = export(raw, stem, args.serial, outdir, False,
                           args.kmz or args.kmz_detaille, args.kmz_detaille)
    print(f"{len(points)} points → " + ", ".join(files))


def main(argv=None):
    global DEBUG
    ap = argparse.ArgumentParser(prog="gpe2tool",
                                 description="Outil libre pour le Canon GP-E2")
    ap.add_argument("--debug", action="store_true",
                    help="affiche les échanges PTP et les détails bruts")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def out_opt(p):
        p.add_argument("-o", "--output", default="gpe2_logs",
                       help="dossier des exports (défaut : gpe2_logs)")

    sub.add_parser("info", help="modèle, firmware, n° de série").set_defaults(func=cmd_info)

    p = sub.add_parser("list", help="liste des logs du GP-E2")
    out_opt(p); p.set_defaults(func=cmd_list)

    p = sub.add_parser("download", help="télécharger les logs")
    out_opt(p)
    p.add_argument("files", nargs="*",
                   help="numéros (de 'list') ou noms ; par défaut : les nouveaux")
    p.add_argument("--force", action="store_true",
                   help="tout reprendre, même ce qui est déjà exporté")
    p.add_argument("--kmz", action="store_true",
                   help="créer aussi un .kmz (même contenu que GPS Log File Utility)")
    p.add_argument("--kmz-detaille", action="store_true",
                   help="créer un .kmz enrichi (trace, parcours horodaté, début/fin)")
    p.add_argument("--raw", action="store_true",
                   help="garder aussi le binaire brut (.bin)")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("erase", help="effacer TOUS les logs")
    out_opt(p)
    p.add_argument("--yes", action="store_true", help="pas de confirmation")
    p.set_defaults(func=cmd_erase)

    p = sub.add_parser("interval", help="lire ou régler l'intervalle (s)")
    p.add_argument("value", nargs="?", type=int,
                   help="1, 5, 10, 15, 30, 60, 120 ou 300")
    p.set_defaults(func=cmd_interval)

    p = sub.add_parser("decode", help="convertir un .bin sans le GP-E2")
    p.add_argument("bin")
    p.add_argument("-o", "--output")
    p.add_argument("--serial", default="", help="n° de série pour l'en-tête du .log")
    p.add_argument("--kmz", action="store_true", help=".kmz format Canon")
    p.add_argument("--kmz-detaille", action="store_true", help=".kmz enrichi")
    p.set_defaults(func=cmd_decode)

    args = ap.parse_args(argv)
    DEBUG = args.debug
    try:
        args.func(args)
    except PTPError as e:
        sys.exit(f"Erreur PTP : {e}")


if __name__ == "__main__":
    main()
