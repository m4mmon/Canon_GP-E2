#!/usr/bin/env python3
"""fauxgpe2 - se fait passer pour un Canon GP-E2 auprès d'un boîtier (EOS 700D) en USB.

Le Raspberry Pi est l'HÔTE USB (dr_mode=host + adaptateur OTG) et parle PTP au boîtier,
comme le ferait le GP-E2 avec le câble mini-A ↔ mini-B.

Fichiers (format « clé = valeur », # pour les commentaires), relus à chaque envoi :
  heure.conf     décalage appliqué à l'heure du Pi
  position.conf  position et qualité du signal à envoyer
  session.conf   paramètres de protocole (séquence d'init, options d'essai)

Protocole (déduit du firmware du 700D, à confirmer) :
  0x9114 SetRemoteMode, 0x91D5 état de connexion, 0x91D9 infos GPS (128 o style EXIF),
  0x91DA satellites, 0x91DD boussole, 0x91DE batterie, 0x91DB heure, 0x91D8 1PPS,
  0x91D7 test RTC, 0x9116 GetEvent (intervalle = propriété 0xD14D, événements 0xC1xx).

Dépendance : pyusb (sudo apt install python3-usb). Lancer avec sudo.
"""

import argparse
import datetime as dt
import os
import struct
import subprocess
import sys
import time

# --- Codes PTP ---------------------------------------------------------------
OC_NAMES = {
    0x1001: "GetDeviceInfo", 0x1002: "OpenSession", 0x1003: "CloseSession",
    0x9108: "GetDeviceInfoEx", 0x9114: "SetRemoteMode", 0x9115: "SetEventMode",
    0x9116: "GetEvent", 0x911D: "KeepDeviceOn", 0x9127: "RequestDevicePropValue",
    0x91D5: "GpsConnectStatus", 0x91D7: "RtcCheck", 0x91D8: "Gps1PPS",
    0x91D9: "GpsInfo", 0x91DA: "GpsSatellites", 0x91DB: "GpsUtcTime",
    0x91DC: "Gps91DC", 0x91DD: "GpsCompass", 0x91DE: "GpsBattery",
}
# Seuls ces opcodes peuvent partir vers le boîtier (jamais de mise à jour du firmware,
# de formatage, d'effacement, de déclenchement…).
ALLOWED_OPCODES = set(OC_NAMES)

OC_OPEN, OC_CLOSE, OC_GET_EVENT = 0x1002, 0x1003, 0x9116
OC_KEEP_ON = 0x911D
OC_CONNECT, OC_RTC, OC_PPS, OC_INFO = 0x91D5, 0x91D7, 0x91D8, 0x91D9
OC_SATS, OC_TIME, OC_COMPASS, OC_BATTERY = 0x91DA, 0x91DB, 0x91DD, 0x91DE

RC_OK, RC_BUSY, RC_ALREADY_OPEN = 0x2001, 0x2019, 0x201E
RC_NAMES = {0x2001: "OK", 0x2002: "General_Error", 0x2003: "Session_Not_Open",
            0x2005: "Operation_Not_Supported", 0x2006: "Parameter_Not_Supported",
            0x2019: "Device_Busy", 0x201E: "Session_Already_Open"}
CT_COMMAND, CT_DATA, CT_RESPONSE, CT_EVENT = 1, 2, 3, 4

PROP_INTERVAL = 0xD14D
EV_PROP_CHANGED, EV_AVAIL_LIST = 0xC189, 0xC18A
EV_NAMES = {0xC189: "PropValueChanged", 0xC18A: "AvailListChanged",
            0xC1EB: "GPS_EB", 0xC1EC: "GPS_EC(1PPS)", 0xC1ED: "GPS_ED(heure ?)",
            0xC1EE: "GPS_EE"}

LOG = None
DEBUG = False


def log(msg):
    line = f"{dt.datetime.now():%H:%M:%S.%f}"[:-3] + " " + msg
    print(line, flush=True)
    if LOG:
        LOG.write(line + "\n")
        LOG.flush()


def dbg(msg):
    if DEBUG:
        log("  [debug] " + msg)


def hexs(b, limit=96):
    s = b[:limit].hex(" ")
    return s + (f" … (+{len(b) - limit} o)" if len(b) > limit else "")


# --- Fichiers de configuration « clé = valeur » ------------------------------
def read_conf(path):
    conf = {}
    try:
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                if "=" not in line:
                    log(f"⚠ {path}:{n} ligne ignorée (pas de '=') : {line}")
                    continue
                k, v = line.split("=", 1)
                conf[k.strip().lower()] = v.strip()
    except FileNotFoundError:
        log(f"⚠ {path} absent : valeurs par défaut")
    return conf


class Conf:
    def __init__(self, d, name):
        self.d, self.name = d, name

    def str(self, k, default=""):
        return self.d.get(k, default)

    def float(self, k, default=0.0):
        try:
            return float(self.d[k].replace(",", ".")) if k in self.d else default
        except ValueError:
            log(f"⚠ {self.name} : {k} = {self.d[k]!r} n'est pas un nombre, {default} utilisé")
            return default

    def int(self, k, default=0):
        try:
            return int(self.d[k], 0) if k in self.d else default
        except ValueError:
            log(f"⚠ {self.name} : {k} = {self.d[k]!r} n'est pas un entier, {default} utilisé")
            return default

    def bool(self, k, default=False):
        if k not in self.d:
            return default
        return self.d[k].lower() in ("1", "oui", "yes", "true", "vrai", "on")

    def hex(self, k):
        v = self.d.get(k, "").replace(" ", "").replace(":", "")
        if not v:
            return None
        try:
            return bytes.fromhex(v)
        except ValueError:
            log(f"⚠ {self.name} : {k} n'est pas de l'hexadécimal valide, ignoré")
            return None

    def oplist(self, k, default=""):
        """« 9114:9 ; 91D5:1 » → [(0x9114, [9]), (0x91D5, [1])]"""
        out = []
        for item in self.d.get(k, default).split(";"):
            item = item.strip()
            if not item:
                continue
            op, _, params = item.partition(":")
            out.append((int(op, 16), [int(p, 0) for p in params.split(",") if p.strip()]))
        return out


# --- Transport USB (le Pi est l'hôte) -----------------------------------------
class PTPError(Exception):
    pass


class PTPTimeout(PTPError):
    pass


class USBTransport:
    def __init__(self, vid, pid, timeout_ms=5000):
        try:
            import usb.core
            import usb.util
        except ImportError:
            sys.exit("pyusb manquant : sudo apt install python3-usb")
        self.usb = usb
        if pid:
            dev = usb.core.find(idVendor=vid, idProduct=pid)
        else:  # premier appareil Canon de classe « Still Image »
            dev = next((d for d in usb.core.find(find_all=True, idVendor=vid)
                        if any(i.bInterfaceClass == 6 for c in d for i in c)), None)
        if dev is None:
            raise PTPError(f"boîtier introuvable ({vid:04x}:{pid or 0:04x}). Allumé, câble OTG, "
                           "Pi en mode hôte ?")
        self.dev, self.timeout = dev, timeout_ms
        intf = dev.get_active_configuration()[(0, 0)]
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
        self.mps = self.ep_out.wMaxPacketSize or 512
        log(f"Boîtier trouvé : {dev.idVendor:04x}:{dev.idProduct:04x}, "
            f"paquets bulk de {self.mps} o")

    def write(self, data):
        self.ep_out.write(data, self.timeout)
        if len(data) % self.mps == 0:
            self.ep_out.write(b"", self.timeout)      # paquet de longueur nulle

    def read(self, timeout_ms=None):
        try:
            return bytes(self.ep_in.read(self.mps * 128, timeout_ms or self.timeout))
        except self.usb.core.USBTimeoutError:
            raise PTPTimeout("pas de réponse du boîtier (délai dépassé)")

    def drain(self, timeout_ms=300):
        """Récupère ce qui arriverait en retard (réponse tardive)."""
        out = b""
        try:
            while True:
                out += bytes(self.ep_in.read(self.mps * 128, timeout_ms))
        except Exception:
            pass
        return out

    def close(self):
        try:
            self.usb.util.release_interface(self.dev, self.intf_num)
            self.usb.util.dispose_resources(self.dev)
        except Exception:
            pass


class DryTransport:
    """Mode --essai-a-vide : rien n'est envoyé, chaque opération répond OK."""
    def __init__(self):
        self.pending = None

    def drain(self, timeout_ms=300):
        return b""

    def write(self, data):
        ctype, code, tid = struct.unpack_from("<HHI", data, 4)
        if ctype == CT_COMMAND:
            self.pending = (code, tid)

    def read(self, timeout_ms=None):
        code, tid = self.pending
        if code == OC_GET_EVENT:
            body = struct.pack("<II", 8, 0)
            return struct.pack("<IHHI", 12 + len(body), CT_DATA, code, tid) + body + \
                struct.pack("<IHHI", 12, CT_RESPONSE, RC_OK, tid)
        return struct.pack("<IHHI", 12, CT_RESPONSE, RC_OK, tid)

    def close(self):
        pass


class PTP:
    def __init__(self, t):
        self.t, self.tid, self._buf = t, 0, b""

    def _send(self, ctype, code, payload):
        pkt = struct.pack("<IHHI", 12 + len(payload), ctype, code, self.tid) + payload
        dbg(f"→ {('CMD', 'DATA')[ctype - 1]} 0x{code:04X} tid={self.tid} {hexs(pkt)}")
        self.t.write(pkt)

    def _recv(self, timeout_ms=None):
        buf = self._buf
        while len(buf) < 12:
            buf += self.t.read(timeout_ms)
        length = struct.unpack_from("<I", buf)[0]
        while len(buf) < length:
            buf += self.t.read(timeout_ms)
        self._buf = buf[length:]
        c = buf[:length]
        ctype, code, tid = struct.unpack_from("<HHI", c, 4)
        dbg(f"← type={ctype} 0x{code:04X} tid={tid} {hexs(c)}")
        return ctype, code, tid, c[12:]

    def transaction(self, op, params=(), data_out=None, timeout_ms=None):
        if op not in ALLOWED_OPCODES:
            raise PTPError(f"opcode 0x{op:04X} interdit par l'outil")
        self._send(CT_COMMAND, op, b"".join(struct.pack("<I", p & 0xFFFFFFFF) for p in params))
        if data_out is not None:
            self._send(CT_DATA, op, data_out)
        data = b""
        while True:
            try:
                ctype, code, tid, payload = self._recv(timeout_ms)
            except PTPTimeout:
                self.tid += 1          # la transaction est perdue : on passe à la suivante
                late = self.t.drain()
                self._buf = b""
                if late:
                    log(f"  données tardives ignorées : {hexs(late)}")
                raise
            if ctype == CT_DATA:
                data += payload
            elif ctype == CT_RESPONSE:
                n = len(payload) // 4
                rparams = list(struct.unpack(f"<{n}I", payload[:n * 4]))
                break
            elif ctype == CT_EVENT:
                log(f"  événement (conteneur) 0x{code:04X} {hexs(payload)}")
        self.tid += 1
        return code, rparams, data


# --- Construction des données -------------------------------------------------
def now_shifted(heure):
    h = Conf(heure, "heure.conf")
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(
        hours=h.float("decalage_heures"), seconds=h.float("decalage_secondes"))


def rat(num, den):
    return struct.pack("<II", int(num) & 0xFFFFFFFF, int(den) & 0xFFFFFFFF)


def angle(value, fmt):
    """Degrés décimaux → 3 rationnels. dm : d/1, minutes décimales /10000, 0/1 (style NMEA) ;
    dms : d/1, m/1, secondes /1000."""
    v = abs(value)
    d = int(v)
    minutes = (v - d) * 60
    if fmt == "dms":
        m = int(minutes)
        s = round((minutes - m) * 60 * 1000)
        if s >= 60000:
            m, s = m + 1, s - 60000
        return rat(d, 1) + rat(m, 1) + rat(s, 1000)
    mm = round(minutes * 10000)
    if mm >= 600000:
        d, mm = d + 1, mm - 600000
    return rat(d, 1) + rat(mm, 10000) + rat(0, 1)


def asciiz(s, size):
    return s.encode("ascii", "replace")[:size - 1].ljust(size, b"\0")


def build_gps_info(pos, sess, t):
    """Structure de 128 o (PROP_GPS_INFORMATION), style EXIF, petit-boutiste."""
    p, s = Conf(pos, "position.conf"), Conf(sess, "session.conf")
    lat, lon = p.float("latitude", 43.62737), p.float("longitude", 1.467825)
    alt = p.float("altitude", 78)
    fmt = s.str("format_angle", "dm")
    b = bytearray(128)
    b[0x00:0x04] = asciiz("N" if lat >= 0 else "S", 4)
    b[0x04:0x1C] = angle(lat, fmt)
    b[0x1C:0x20] = asciiz("E" if lon >= 0 else "W", 4)
    b[0x20:0x38] = angle(lon, fmt)
    b[0x38] = 0 if alt >= 0 else 1
    b[0x3C:0x44] = rat(round(abs(alt) * 10), 10)
    b[0x44:0x5C] = rat(t.hour, 1) + rat(t.minute, 1) + \
        rat(t.second * 1000 + t.microsecond // 1000, 1000)
    # 0x5C–0x73, disposition déduite de l'EXIF (essai du 19/10/2026) :
    # 0x5C Satellites (2 o texte), 0x5E Status (2 o), 0x60 MeasureMode (2 o + 2 o de bourrage),
    # 0x64 DOP (rationnel), 0x6C MapDatum (8 o texte, ex. « WGS-84 »).
    sats = p.str("satellites", "10").encode("ascii", "replace")[:2].ljust(2, b"\0")
    blk = sats + asciiz(p.str("statut", "A"), 2) + asciiz(p.str("mode_mesure", "3"), 4) + \
        rat(round(p.float("dop", 4) * 100), 100) + asciiz(p.str("datum", "WGS-84"), 8)
    raw = p.hex("brut_5c")
    if raw is not None:
        blk = raw[:24].ljust(24, b"\0")
    b[0x5C:0x74] = blk
    b[0x74:0x7F] = asciiz(f"{t:%Y:%m:%d}", 11)
    n = s.int("longueur_infos", 128)
    body = bytes(b[:n]) if n <= 128 else bytes(b) + bytes(n - 128)
    return struct.pack("<I", len(body) + 4) + body


def build_sats(pos):
    p = Conf(pos, "position.conf")
    return struct.pack("<IIII", 16, p.int("sat_status", 1), p.int("sat_niveau", 3),
                       p.int("sat_etat", 3))


def build_compass(pos):
    p = Conf(pos, "position.conf")
    ref = (p.str("boussole_ref", "M") or "M")[0]
    num = round(p.float("boussole", 190) * 100) % 36000
    return struct.pack("<IBBHII", 16, ord(ref), p.int("boussole_octet5", 0), 0, num, 100)


def build_time(sess, t):
    """Données de 0x91DB. Le handler du 700D lit [u32 longueur][charge utile] et passe la charge
    utile à mktime() (struct tm DryOS : 11 entiers de 32 bits), puis ajoute 1 s et écrit l'horloge."""
    s = Conf(sess, "session.conf")
    fmt = s.str("heure_format", "tm")
    if fmt == "tm":
        if s.str("heure_tm_base", "utc") == "locale":
            t = t.astimezone()          # fuseau du Pi
        tm = (t.second, t.minute, t.hour, t.day, t.month - 1, t.year - 1900,
              (t.weekday() + 1) % 7, t.timetuple().tm_yday - 1, 0, 0, 0)
        body = struct.pack("<11i", *tm)
        return struct.pack("<I", len(body) + 4) + body
    # Tout autre format ferait lire à mktime() de la mémoire hors des données : refusé.
    raise PTPError(f"heure_format = {fmt} refusé (risque de blocage du boîtier) : utiliser « tm »")


# --- Événements EOS (GetEvent 0x9116) ------------------------------------------
def parse_events(data):
    """Enregistrements [u32 taille][u32 type][charge utile…], terminés par taille 8 / type 0."""
    out, o = [], 0
    while o + 8 <= len(data):
        size, etype = struct.unpack_from("<II", data, o)
        if size < 8 or (size == 8 and etype == 0):
            break
        out.append((etype, data[o + 8:o + size]))
        o += size
    return out


# --- Boucle principale ----------------------------------------------------------
class FakeGPE2:
    def __init__(self, args):
        self.args = args
        self.dir = args.conf_dir
        self.interval = None
        self.next_send = 0.0
        self.timeouts = 0

    def confs(self):
        return (read_conf(os.path.join(self.dir, "heure.conf")),
                read_conf(os.path.join(self.dir, "position.conf")),
                read_conf(os.path.join(self.dir, "session.conf")))

    def op(self, op, params=(), data=None, label=""):
        name = OC_NAMES.get(op, f"0x{op:04X}")
        s = Conf(read_conf(os.path.join(self.dir, "session.conf")), "session.conf")
        tmo = s.int("delai_91db_ms", 15000) if op == OC_TIME else s.int("delai_ms", 5000)
        try:
            code, rp, d = self.ptp.transaction(op, params, data, tmo)
        except PTPTimeout:
            log(f"⚠ {name}({', '.join(hex(p) for p in params)}){label} : PAS DE RÉPONSE en {tmo} ms "
                f"(transaction abandonnée, on continue)")
            self.timeouts += 1
            if self.timeouts >= s.int("max_echecs", 3):
                raise PTPError(f"{self.timeouts} opérations sans réponse d'affilée : boîtier bloqué ?")
            return None, [], b""
        self.timeouts = 0
        extra = f" params_réponse={[hex(x) for x in rp]}" if rp else ""
        extra += f" données={len(d)} o" if d and op != OC_GET_EVENT else ""
        level = log if (op != OC_GET_EVENT or code != RC_OK or DEBUG) else (lambda m: None)
        level(f"{name}({', '.join(hex(p) for p in params)}){label} → 0x{code:04X} "
              f"{RC_NAMES.get(code, '?')}{extra}")
        return code, rp, d

    def handle_events(self, data, sess):
        s = Conf(sess, "session.conf")
        triggers = {int(x, 16) for x in s.str("position_sur_evenement", "").replace(",", " ").split()}
        for etype, pl in parse_events(data):
            if etype == EV_PROP_CHANGED and len(pl) >= 8:
                code = struct.unpack_from("<I", pl)[0]
                val = pl[4:]
                if code == PROP_INTERVAL:
                    v = struct.unpack_from("<I", val)[0]
                    if 1 <= v <= 3600:
                        if v != self.interval:
                            log(f"★ Intervalle demandé par le boîtier : {v} s (0xD14D)")
                            self.interval = v
                            self.next_send = min(self.next_send, time.time() + v)
                    else:
                        log(f"⚠ 0xD14D = 0x{v:X} ignoré (valeur hors plage)")
                elif s.bool("journal_proprietes"):
                    log(f"  prop 0x{code:04X} = {hexs(val, 32)}")
            elif etype == EV_AVAIL_LIST:
                if s.bool("journal_proprietes"):
                    log(f"  liste 0x{struct.unpack_from('<I', pl)[0]:04X}")
            else:
                log(f"★ Événement 0x{etype:04X} {EV_NAMES.get(etype, '')} : {hexs(pl)}")
                if etype == 0xC1ED and Conf(sess, "").str("heure_envoi", "jamais") in ("c1ed", "les_deux"):
                    self.send_time()
                if etype in triggers:
                    self.next_send = 0
                self.react(s.str(f"sur_{etype:04x}"), etype)

    def react(self, script, etype):
        """Réaction configurable à un événement : « sur_c1eb = HEURE ; SEC ; 91D8:1 ».
        Jetons : HEURE (91DB avec l'heure actuelle), POS (envoi immédiat de la position),
        SEC (attendre le début de la seconde suivante), PAUSE:ms, ou opcode[:p1,p2…] en hexa."""
        if not script:
            return
        log(f"  réaction à 0x{etype:04X} : {script}")
        for item in script.split(";"):
            item = item.strip()
            if not item:
                continue
            tok, _, arg = item.partition(":")
            tok = tok.strip().upper()
            try:
                if tok in ("HEURE", "91DB"):
                    self.send_time()
                elif tok == "POS":
                    self.send_position()
                    self.next_send = time.time() + (self.interval or 15)
                elif tok == "SEC":
                    now = time.time()
                    time.sleep(1 - (now % 1))
                    log("  début de seconde " + f"{dt.datetime.now():%H:%M:%S.%f}"[:-3])
                elif tok == "PAUSE":
                    time.sleep(int(arg) / 1000)
                else:
                    self.op(int(tok, 16), [int(x, 0) for x in arg.split(",") if x.strip()])
            except (ValueError, PTPError) as e:
                log(f"⚠ réaction « {item} » ignorée : {e}")

    def send_time(self):
        """Le 700D écrit dans son horloge l'heure reçue + 1 s, dès réception. On envoie donc
        l'heure de la seconde en cours juste avant la seconde suivante (heure_instant_ms),
        plus une correction manuelle éventuelle (heure_correction_s)."""
        heure, _, sess = self.confs()
        s = Conf(sess, "session.conf")
        instant = s.float("heure_instant_ms", 900) / 1000
        if 0 <= instant < 1:
            frac = now_shifted(heure).microsecond / 1e6
            time.sleep((instant - frac) % 1)
        t = now_shifted(heure) + dt.timedelta(seconds=s.float("heure_correction_s", 0))
        self.op(OC_TIME, data=build_time(sess, t), label=f" heure {t:%Y-%m-%d %H:%M:%S} UTC")

    def send_position(self):
        heure, pos, sess = self.confs()
        s = Conf(sess, "session.conf")
        t = now_shifted(heure)
        info = build_gps_info(pos, sess, t)
        p = Conf(pos, "position.conf")
        self.op(OC_INFO, data=info,
                label=f" {p.float('latitude', 43.62737):.6f}, {p.float('longitude', 1.467825):.6f}, "
                      f"{p.float('altitude', 78):.0f} m, {t:%H:%M:%S} UTC")
        dbg("infos GPS : " + info.hex(" "))
        if s.bool("envoyer_satellites", True):
            self.op(OC_SATS, data=build_sats(pos))
        if s.bool("envoyer_batterie", True):
            self.op(OC_BATTERY, (p.int("batterie", 2),))
        if s.bool("envoyer_boussole", False):
            self.op(OC_COMPASS, data=build_compass(pos))
        if s.bool("garder_allume", False):
            self.op(OC_KEEP_ON)

    def check_ntp(self):
        try:
            r = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                               capture_output=True, text=True, timeout=5)
            if r.stdout.strip() != "yes":
                log("⚠ L'heure du Pi n'est PAS synchronisée (NTP) : l'heure envoyée sera fausse.")
        except Exception:
            log("⚠ Impossible de vérifier la synchronisation NTP (timedatectl).")

    def run(self):
        heure, pos, sess = self.confs()
        s = Conf(sess, "session.conf")
        self.check_ntp()
        if self.args.essai_a_vide:
            t = DryTransport()
        else:
            t = USBTransport(s.int("vid", 0x04A9), s.int("pid", 0x3272))
        self.ptp = PTP(t)
        try:
            for op, params in s.oplist("init", "1002:1 ; 1001 ; 9114:9 ; 9115:1 ; 91D5:1"):
                if op == OC_OPEN:
                    self.ptp.tid = 0
                self.op(op, params)
            if s.bool("test_rtc", True):
                self.op(OC_RTC)
            if s.str("heure_envoi", "c1ed") in ("demarrage", "les_deux"):
                self.send_time()
            if s.bool("pps", False):
                self.op(OC_PPS, (1,))
            self.interval = None
            period = s.float("periode_evenements", 1.0)
            self.next_send = time.time()
            log("Boucle démarrée (Ctrl+C pour arrêter).")
            while True:
                heure, pos, sess = self.confs()
                s = Conf(sess, "session.conf")
                code, _, data = self.op(OC_GET_EVENT)
                if code == RC_OK:
                    self.handle_events(data, sess)
                if self.interval is None:
                    self.interval = s.int("intervalle_defaut", 15)
                    log(f"Intervalle : {self.interval} s (défaut ; pas encore reçu de 0xD14D)")
                if time.time() >= self.next_send:
                    self.send_position()
                    self.next_send = time.time() + self.interval
                time.sleep(period)
        except KeyboardInterrupt:
            log("Arrêt demandé.")
        finally:
            try:
                for op, params in Conf(sess, "").oplist("fin", "91D5:0 ; 1003"):
                    self.op(op, params)
            except Exception as e:
                log(f"(fin de session : {e})")
            t.close()


def main():
    global LOG, DEBUG
    ap = argparse.ArgumentParser(description="Faux GP-E2 : envoie une position GPS à un boîtier "
                                             "Canon en USB (le Pi est l'hôte).")
    ap.add_argument("-c", "--conf-dir", default=".",
                    help="dossier contenant heure.conf, position.conf, session.conf")
    ap.add_argument("-j", "--journal", help="copie du journal dans ce fichier")
    ap.add_argument("--debug", action="store_true", help="trace PTP complète en hexadécimal")
    ap.add_argument("--essai-a-vide", action="store_true",
                    help="sans boîtier : affiche ce qui serait envoyé (avec --debug)")
    args = ap.parse_args()
    DEBUG = args.debug
    if args.journal:
        LOG = open(args.journal, "a", encoding="utf-8")
    try:
        FakeGPE2(args).run()
    except PTPError as e:
        log(f"Erreur : {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
