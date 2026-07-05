"""SIP/RTP endpoint for Twilio inbound calls — proven core from tmp/spike_sip_twilio.py.

Registers OUTBOUND to a Twilio SIP domain (no open ports — the registration +
keepalive holds the NAT pinhole), answers incoming INVITEs, and hands each call to
an `on_call(caller, RtpSession)` callback. All the hard-won 2026-07-02 findings are
baked in: STUN public address in Contact/SDP, full Via-stack echo, Min-Expires 600,
407 Proxy-Auth, stale-binding wipe. UDP transport (SIP ALG must be off; TLS is a
later milestone). Pure stdlib SIP + numpy-free here (codec lives in codec.py).

RtpSession is the transport seam the TwilioConversationSource rides:
  read(timeout)  -> inbound μ-law payload (bytes) or None
  write(ulaw160) -> queue an outbound 20ms frame (a paced thread sends at 20ms)
  flush()        -> drop queued outbound audio (barge-in)
  hangup()       -> end the call (sends BYE)
"""
import logging
import os
import queue
import random
import re
import select
import socket
import struct
import threading
import time

from .codec import SILENCE_FRAME, FRAME_SAMPLES

logger = logging.getLogger(__name__)

# Call-teardown guards. Inbound RTP flows continuously during a live call (Twilio
# sends silence frames too), so a gap means the caller is gone even if the BYE was
# lost. Without these the serve thread wedges in _call_io_loop forever, blocking
# re-registration until a restart. Krem idles a while mid-call, so 60s not 30s.
_CALL_INACTIVITY_SEC = 60      # no inbound RTP this long -> assume hangup
_CALL_MAX_SEC = 7200           # hard 2h backstop against a runaway/wedged call

STUN_MAGIC = 0x2112A442
_PTIME = 0.02          # 20ms RTP frames

# Twilio US1 SIP signaling edges (docs: 54.172.60.0/23, gateways .0-.3). A
# restricted-cone NAT only passes inbound from addresses we've SENT to — the
# registrar pinhole covers one edge, but INVITEs arrive from ANY of them.
# CRLF keepalives (RFC 5626 convention) hold a pinhole to each. Found live
# 2026-07-04: router demoted the mapping to restricted after restart churn —
# green keepalives, Twilio error 32011 "Request timeout" on every INVITE.
_TWILIO_EDGES = [("54.172.60.0", 5060), ("54.172.60.1", 5060),
                 ("54.172.60.2", 5060), ("54.172.60.3", 5060)]
_EDGE_PUNCH_SEC = 30


def _rand_hex(n=16):
    return "".join(random.choice("0123456789abcdef") for _ in range(n))


def _md5(s):
    import hashlib
    return hashlib.md5(s.encode()).hexdigest()


def _local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def stun_public_addr(sock, host="stun.l.google.com", port=19302, timeout=3):
    """Discover this socket's public NAT mapping (IP, port) via STUN, or None."""
    old = sock.gettimeout()
    try:
        dest = (socket.gethostbyname(host), port)
    except Exception:
        return None
    txid = bytes(random.randint(0, 255) for _ in range(12))
    req = struct.pack("!HHI", 0x0001, 0x0000, STUN_MAGIC) + txid
    try:
        for _ in range(3):
            sock.settimeout(timeout)
            sock.sendto(req, dest)
            try:
                deadline = time.time() + timeout
                while time.time() < deadline:
                    data, _ = sock.recvfrom(2048)
                    if len(data) < 20:
                        continue
                    _, mlen, magic = struct.unpack("!HHI", data[:8])
                    if magic != STUN_MAGIC or data[8:20] != txid:
                        continue
                    off, end = 20, 20 + mlen
                    while off + 4 <= end:
                        atype, alen = struct.unpack("!HH", data[off:off + 4])
                        aval = data[off + 4:off + 4 + alen]
                        if atype == 0x0020 and len(aval) >= 8:
                            pport = struct.unpack("!H", aval[2:4])[0] ^ (STUN_MAGIC >> 16)
                            praw = bytes(b ^ m for b, m in zip(aval[4:8], struct.pack("!I", STUN_MAGIC)))
                            return socket.inet_ntoa(praw), pport
                        if atype == 0x0001 and len(aval) >= 8:
                            return socket.inet_ntoa(aval[4:8]), struct.unpack("!H", aval[2:4])[0]
                        off += 4 + alen + ((4 - alen % 4) % 4)
                    break
            except socket.timeout:
                continue
    finally:
        sock.settimeout(old)
    return None


def _parse(data):
    text = data.decode(errors="replace")
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    headers, vias, rrs = {}, [], []
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            k, v = k.strip().lower(), v.strip()
            if k == "via":
                vias.append(v)
            if k == "record-route":
                rrs.append(v)
            if k not in headers:
                headers[k] = v
    headers["_vias"] = vias
    headers["_rrs"] = rrs
    return lines[0], headers, body


def _via_lines(h):
    return [f"Via: {v}" for v in (h.get("_vias") or [h.get("via", "")])]


def _ua_public_addr(h):
    """Far UA's public signaling addr, from the bottom Via's received=/rport=
    (stamped by Twilio's proxy). With no Record-Route in the INVITE, the ACK and
    the hangup BYE come DIRECT from this host — the NAT only passes them if we've
    sent to it first (the hole-punch)."""
    vias = h.get("_vias") or []
    if not vias:
        return None
    v = vias[-1]
    m_ip = re.search(r"received=([\d.]+)", v)
    m_pt = re.search(r"rport=(\d+)", v)
    if m_ip:
        return (m_ip.group(1), int(m_pt.group(1)) if m_pt else 5060)
    m = re.search(r"SIP/2\.0/UDP\s+([\d.]+)(?::(\d+))?", v)
    if m and not m.group(1).startswith(("10.", "192.168.", "172.")):
        return (m.group(1), int(m.group(2) or 5060))
    return None


def _challenge(value):
    value = value.split(" ", 1)[1] if value.lower().startswith("digest") else value
    out = {}
    for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', value):
        out[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return out


class RtpSession:
    """One live call's media. A paced thread sends 20ms frames (silence when idle,
    keeping RTP + the NAT pinhole alive); inbound frames land in a queue."""

    def __init__(self, sock, remote):
        self._sock = sock
        self._remote = remote
        self._seq = random.randint(0, 20000)
        self._ts = random.randint(0, 100000)
        self._ssrc = random.randint(0, 0xFFFFFFFF)
        self._out = queue.Queue()
        self._in = queue.Queue(maxsize=200)
        self._alive = threading.Event()
        self._alive.set()
        self._ended = threading.Event()
        self._sender = None
        self._hangup_after_drain = False   # armed by the <<HANG UP>> sentinel hook

    def start(self):
        self._sender = threading.Thread(target=self._send_loop, daemon=True, name="twilio-rtp-tx")
        self._sender.start()

    def feed_inbound(self, ulaw):
        try:
            self._in.put_nowait(ulaw)
        except queue.Full:
            try:
                self._in.get_nowait()
                self._in.put_nowait(ulaw)
            except queue.Empty:
                pass

    def read(self, timeout=0.5):
        try:
            return self._in.get(timeout=timeout)
        except queue.Empty:
            return None

    def write(self, ulaw160):
        if self._alive.is_set():
            self._out.put(ulaw160)

    def flush(self):
        while True:
            try:
                self._out.get_nowait()
            except queue.Empty:
                break

    def outbound_idle(self):
        return self._out.empty()

    def _send_loop(self):
        next_t = time.time()
        while self._alive.is_set():
            try:
                frame = self._out.get_nowait()
            except queue.Empty:
                frame = SILENCE_FRAME
            hdr = struct.pack("!BBHII", 0x80, 0x00, self._seq & 0xFFFF,
                              self._ts & 0xFFFFFFFF, self._ssrc)
            try:
                self._sock.sendto(hdr + frame, self._remote)
            except OSError:
                break
            self._seq += 1
            self._ts += FRAME_SAMPLES
            next_t += _PTIME
            time.sleep(max(0, next_t - time.time()))

    def stop(self):
        self._alive.clear()
        self._ended.set()

    def wait_ended(self, timeout=None):
        return self._ended.wait(timeout)


class SipEndpoint:
    def __init__(self, domain, user, password, on_call,
                 sip_port=5062, rtp_port=10080, expires=600, accept_call=None):
        self.domain = domain
        self.user = user
        self.password = password
        self.on_call = on_call
        # Optional pre-answer gate: accept_call(caller) -> bool. False = 603
        # Decline before any 180/200 — the caller a rule doesn't want never
        # reaches a session (no RTP, no chat, carrier plays the right tone).
        self.accept_call = accept_call
        self.sip_port = sip_port
        self.rtp_port = rtp_port
        self.expires = expires
        self.ip = _local_ip()
        self._stop = threading.Event()
        self._registered = False
        self.sip = None
        self.rtp = None
        self.call_id = f"{_rand_hex(20)}@{self.ip}"
        self.from_tag = _rand_hex(10)
        self.cseq = 0
        self.pub_sip = None
        self.contact = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    def serve_forever(self):
        """Bind, register, and loop: keepalive + wait for INVITE. Blocks until stop()."""
        self.sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sip.bind(("0.0.0.0", self.sip_port))
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp.bind(("0.0.0.0", self.rtp_port))
        self.registrar = (socket.gethostbyname(self.domain), 5060)
        pub = stun_public_addr(self.sip)
        ip, port = pub if pub else (self.ip, self.sip_port)
        if not pub:
            logger.warning("[TWILIO] STUN failed — using LAN addr (calls will fail behind NAT)")
        self.pub_sip = (ip, port)
        self.contact = f"<sip:{self.user}@{ip}:{port}>"

        self._deregister_all()
        if not self._register():
            logger.error("[TWILIO] SIP registration failed — voice endpoint not listening")
            return
        logger.info(f"[TWILIO] registered as {self.user}@{self.domain} (public {ip}:{port})")
        self._punch_edges()

        next_reg = time.time() + self.expires / 2
        next_punch = time.time() + _EDGE_PUNCH_SEC
        while not self._stop.is_set():
            try:
                if time.time() >= next_reg:
                    # NAT-drift guard: routers re-map long-lived UDP bindings (proven
                    # live 2026-07-03: green keepalives, unroutable INVITEs). Re-STUN
                    # each keepalive; if the public mapping moved, wipe the stale
                    # binding and register the new Contact. STUN failure -> keep the
                    # old contact (never clobber with a LAN addr).
                    try:
                        pub = stun_public_addr(self.sip)
                        if pub and pub != self.pub_sip:
                            logger.warning(f"[TWILIO] public mapping moved {self.pub_sip} -> {pub} — re-binding")
                            self.pub_sip = pub
                            self.contact = f"<sip:{self.user}@{pub[0]}:{pub[1]}>"
                            self._deregister_all()
                    except Exception as e:
                        logger.debug(f"[TWILIO] keepalive STUN check failed: {e}")
                    ok = self._register()
                    logger.info(f"[TWILIO] keepalive re-register {'ok' if ok else 'FAILED'} — {self.user}@{self.domain}")
                    next_reg = time.time() + self.expires / 2
                if time.time() >= next_punch:
                    self._punch_edges()
                    next_punch = time.time() + _EDGE_PUNCH_SEC
                r, _, _ = select.select([self.sip], [], [], 0.5)
                if not r:
                    continue
                data, addr = self.sip.recvfrom(65535)
                start, h, body = _parse(data)
                if start.startswith("INVITE"):
                    self._handle_call(start, h, body, addr)
                elif start.startswith("OPTIONS"):
                    self._reply(200, "OK", h, addr)
                else:
                    # TEMP probe (BYE-loss investigation): SIP arriving OUTSIDE a
                    # call — e.g. a late BYE retransmit after inactivity teardown.
                    logger.info(f"[TWILIO-SIPIDLE] {start} from {addr[0]}:{addr[1]}")
            except Exception as e:
                # A stray/malformed packet or transient socket error must NEVER kill
                # this thread — a dead endpoint silently drops ALL future calls until
                # a restart (the bug that made her stop answering ~10min after boot).
                # Log, back off briefly, keep serving.
                logger.error(f"[TWILIO] serve loop error (continuing): {e}", exc_info=True)
                time.sleep(0.2)
        # Deregister HERE, on the serve thread — it must stay the socket's ONLY
        # reader. A second reader (the old stop()-side deregister) races it for
        # the reply datagram; the loser blocks forever in recvfrom and wedges
        # shutdown until systemd SIGKILLs (three wedges Jul 2-3, 2026).
        try:
            self._deregister_all()
        except Exception:
            pass
        for s in (self.sip, self.rtp):
            try:
                s.close()
            except Exception:
                pass

    def stop(self):
        """Signal the serve thread to exit; it deregisters on its way out.
        Never read the socket from here — single-reader rule (see serve_forever)."""
        self._stop.set()

    def _punch_edges(self):
        """Hold NAT pinholes open to every Twilio signaling edge (see
        _TWILIO_EDGES). Write-only — never reads the socket."""
        targets = list(_TWILIO_EDGES)
        if getattr(self, "registrar", None):
            targets.append(self.registrar)
        for addr in targets:
            try:
                self.sip.sendto(b"\r\n\r\n", addr)
            except OSError:
                pass

    # ── one call ─────────────────────────────────────────────────────────────
    def _handle_call(self, start, h, sdp, addr):
        m = re.search(r"c=IN IP4 ([\d.]+)", sdp)
        p = re.search(r"m=audio (\d+)", sdp)
        if not (m and p):
            self._reply(488, "Not Acceptable Here", h, addr)
            return
        remote_rtp = (m.group(1), int(p.group(1)))
        caller = self._caller_number(h.get("from", ""))
        logger.info(f"[TWILIO] incoming call from {caller}")
        if self.accept_call is not None:
            try:
                accepted = bool(self.accept_call(caller))
            except Exception as e:
                logger.error(f"[TWILIO] accept_call check failed (declining): {e}")
                accepted = False
            if not accepted:
                logger.info(f"[TWILIO] no rule accepts {caller} — replying 603 Decline")
                self._reply(603, "Decline", h, addr)
                return
        # TEMP probe (BYE-loss investigation): does the INVITE carry Record-Route,
        # and where does the far UA live? Settles proxy-path vs direct-path routing.
        logger.info(f"[TWILIO-SIPCALL] INVITE Record-Route x{len(h.get('_rrs') or [])}: "
                    f"{h.get('_rrs')} | bottom Via: {(h.get('_vias') or [''])[-1]}")

        rpub = stun_public_addr(self.rtp)
        rip, rport = rpub if rpub else (self.ip, self.rtp_port)
        my_sdp = ("v=0\r\n" f"o=- 0 0 IN IP4 {rip}\r\n" "s=sapphire\r\n"
                  f"c=IN IP4 {rip}\r\n" "t=0 0\r\n"
                  f"m=audio {rport} RTP/AVP 0\r\n"
                  "a=rtpmap:0 PCMU/8000\r\na=ptime:20\r\na=sendrecv\r\n")
        self._reply(180, "Ringing", h, addr)
        self._reply(200, "OK", h, addr, sdp=my_sdp)

        session = RtpSession(self.rtp, remote_rtp)
        # Outbound-call correlation: <Dial><Sip> URI params arrive as X- headers
        # (if Twilio passes them through — the daemon logs which).
        session.x_header = h.get("x-sapphire-call")
        session.start()
        ua = _ua_public_addr(h)
        if ua:
            # NAT hole-punch: open our router's path from the far UA's host so its
            # direct end-to-end ACK/BYE can reach this socket. Re-punched in the
            # io loop to hold the mapping for the whole call.
            try:
                self.sip.sendto(b"\r\n\r\n", ua)
                logger.info(f"[TWILIO] hole-punched SIP path to UA at {ua[0]}:{ua[1]}")
            except OSError as e:
                logger.warning(f"[TWILIO] hole-punch failed: {e}")
        dialog = {"h": h, "addr": addr, "remote": remote_rtp, "ua": ua}

        # Run the conversation on its own thread; this thread pumps RTP in + watches
        # the SIP socket for BYE until the session ends.
        conv = threading.Thread(target=self._run_on_call, args=(caller, session),
                                daemon=True, name="twilio-oncall")
        conv.start()
        self._call_io_loop(session, dialog)
        session.stop()
        conv.join(timeout=5)

    def _run_on_call(self, caller, session):
        try:
            self.on_call(caller, session)
        except Exception as e:
            logger.error(f"[TWILIO] on_call error: {e}", exc_info=True)
        finally:
            session.stop()

    def _call_io_loop(self, session, dialog):
        """Route inbound RTP -> session; watch SIP for BYE. Ends when the session
        dies, the caller hangs up (BYE), the inbound audio stops (lost-BYE guard),
        or the call runs past the hard cap — so the serve thread ALWAYS returns to
        re-register instead of wedging here forever."""
        started = time.time()
        last_rtp = started
        last_punch = started
        while session._alive.is_set() and not self._stop.is_set():
            now = time.time()
            if now - last_punch > 15:
                if dialog.get("ua"):
                    try:
                        self.sip.sendto(b"\r\n\r\n", dialog["ua"])
                    except OSError:
                        pass
                self._punch_edges()          # keep ALL edges open during calls too
                last_punch = now
            if now - last_rtp > _CALL_INACTIVITY_SEC:
                logger.info(f"[TWILIO] no inbound audio for {_CALL_INACTIVITY_SEC}s — ending call (assumed hangup, BYE not seen)")
                session.stop()
                self._send_bye(dialog)
                return
            if now - started > _CALL_MAX_SEC:
                logger.warning(f"[TWILIO] call exceeded {_CALL_MAX_SEC}s cap — force-ending")
                session.stop()
                self._send_bye(dialog)
                return
            r, _, _ = select.select([self.rtp, self.sip], [], [], 0.2)
            for s in r:
                data, addr = s.recvfrom(65535)
                if s is self.rtp:
                    if len(data) > 12 and (data[0] & 0xC0) == 0x80:
                        _gap = time.time() - last_rtp          # TEMP probe: is Twilio RTP
                        if _gap > 3:                            # continuous during silence?
                            logger.info(f"[TWILIO-RTPGAP] inbound RTP resumed after {_gap:.1f}s gap")
                        last_rtp = time.time()
                        session.feed_inbound(data[12:])
                else:
                    start, h, _ = _parse(data)
                    # TEMP probe (BYE-loss investigation): every SIP message that
                    # reaches the socket during a call, with its source addr.
                    logger.info(f"[TWILIO-SIPCALL] {start} from {addr[0]}:{addr[1]}")
                    if start.startswith("BYE"):
                        logger.info("[TWILIO] caller hung up (BYE)")
                        self._reply(200, "OK", h, addr)
                        session.stop()
                        return
                    if start.startswith("INVITE"):
                        # Already on a call — reject the newcomer cleanly so the carrier
                        # plays a busy tone instead of a routing failure. (One call at a
                        # time until the serve-loop refactor lands.)
                        logger.info("[TWILIO] second call while busy — replying 486 Busy Here")
                        self._reply(486, "Busy Here", h, addr)
                    elif start.startswith("OPTIONS"):
                        self._reply(200, "OK", h, addr)
        # We (Sapphire) ended it -> send BYE to the caller leg.
        self._send_bye(dialog)

    # ── SIP message helpers ──────────────────────────────────────────────────
    def _send(self, text, addr):
        self.sip.sendto(text.encode(), addr)

    def _reply(self, code, reason, req_h, addr, sdp=None):
        to = req_h.get("to", "")
        if code != 100 and "tag=" not in to and not str(code).startswith("1"):
            to = f"{to};tag={self.from_tag}"
        elif code == 180 and "tag=" not in to:
            to = f"{to};tag={self.from_tag}"
        # RFC 3261 12.1.1: dialog-forming responses MUST echo Record-Route in
        # order — without it Twilio sends the 200's ACK and the hangup BYE
        # direct-to-Contact from a host the NAT blocks (the lost-BYE root cause).
        lines = [f"SIP/2.0 {code} {reason}", *_via_lines(req_h),
                 *[f"Record-Route: {r}" for r in (req_h.get("_rrs") or [])],
                 f"From: {req_h.get('from', '')}", f"To: {to}",
                 f"Call-ID: {req_h.get('call-id', '')}", f"CSeq: {req_h.get('cseq', '')}",
                 f"Contact: {self.contact}", "Allow: INVITE, ACK, BYE, CANCEL, OPTIONS"]
        body = sdp or ""
        if sdp:
            lines.append("Content-Type: application/sdp")
        lines.append(f"Content-Length: {len(body)}")
        self._send("\r\n".join(lines) + "\r\n\r\n" + body, addr)

    def _send_bye(self, dialog):
        h, addr = dialog["h"], dialog["addr"]
        target = self._contact_uri(h) or f"sip:{self.user}@{self.domain}"
        self.cseq += 1
        lines = [
            f"BYE {target} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.ip}:{self.sip_port};branch=z9hG4bK{_rand_hex(12)};rport",
            # In-dialog requests ride the Record-Route set from the INVITE — without
            # it the proxy has no dialog context and 481s the BYE (spike run 2-3).
            *[f"Route: {r}" for r in (h.get("_rrs") or [])],
            "Max-Forwards: 70",
            f"From: {h.get('to', '')};tag={self.from_tag}",
            f"To: {h.get('from', '')}",
            f"Call-ID: {h.get('call-id', '')}", f"CSeq: {self.cseq} BYE",
            "Content-Length: 0",
        ]
        self._send("\r\n".join(lines) + "\r\n\r\n", addr)

    def _reg_msg(self, auth=None, auth_header="Authorization", wipe=False):
        self.cseq += 1
        uri = f"sip:{self.domain}"
        contact = "Contact: *" if wipe else f"Contact: {self.contact}"
        exp = 0 if wipe else self.expires
        lines = [
            f"REGISTER {uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.ip}:{self.sip_port};branch=z9hG4bK{_rand_hex(12)};rport",
            "Max-Forwards: 70",
            f"From: <sip:{self.user}@{self.domain}>;tag={self.from_tag}",
            f"To: <sip:{self.user}@{self.domain}>",
            f"Call-ID: {self.call_id}", f"CSeq: {self.cseq} REGISTER",
            contact, f"Expires: {exp}", "Allow: INVITE, ACK, BYE, CANCEL, OPTIONS",
            "User-Agent: sapphire-twilio/1.0",
        ]
        if auth:
            lines.append(f"{auth_header}: {self._digest('REGISTER', uri, auth)}")
        lines.append("Content-Length: 0")
        return "\r\n".join(lines) + "\r\n\r\n"

    def _digest(self, method, uri, ch):
        realm, nonce = ch.get("realm", ""), ch.get("nonce", "")
        ha1 = _md5(f"{self.user}:{realm}:{self.password}")
        ha2 = _md5(f"{method}:{uri}")
        parts = [f'username="{self.user}"', f'realm="{realm}"', f'nonce="{nonce}"',
                 f'uri="{uri}"', 'algorithm=MD5']
        if "auth" in (ch.get("qop") or ""):
            cnonce, nc = _rand_hex(8), "00000001"
            parts += [f'response="{_md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")}"',
                      'qop=auth', f'nc={nc}', f'cnonce="{cnonce}"']
        else:
            parts.append(f'response="{_md5(f"{ha1}:{nonce}:{ha2}")}"')
        if ch.get("opaque"):
            parts.append(f'opaque="{ch["opaque"]}"')
        return "Digest " + ", ".join(parts)

    def _reg_wait(self):
        deadline = time.time() + 5
        while time.time() < deadline:
            r, _, _ = select.select([self.sip], [], [], deadline - time.time())
            if not r:
                continue
            data, _ = self.sip.recvfrom(65535)
            start, h, _ = _parse(data)
            if start.startswith("SIP/2.0") and start.split(" ")[1] != "100":
                return start, h
            if start.startswith("OPTIONS"):
                self._reply(200, "OK", h, self.registrar)
        return None

    def _register(self):
        challenge, auth_header = None, "Authorization"
        for _ in range(4):
            self._send(self._reg_msg(challenge, auth_header), self.registrar)
            resp = self._reg_wait()
            if resp is None:
                return False
            start, h = resp
            code = int(start.split(" ", 2)[1])
            if code in (401, 407):
                if challenge is not None:
                    logger.error("[TWILIO] SIP credentials rejected")
                    return False
                key = "www-authenticate" if code == 401 else "proxy-authenticate"
                challenge = _challenge(h.get(key, ""))
                auth_header = "Authorization" if code == 401 else "Proxy-Authorization"
                continue
            if code == 423:
                self.expires = int(h.get("min-expires", "600"))
                challenge, auth_header = None, "Authorization"
                continue
            if 200 <= code < 300:
                self._registered = True
                return True
            return False
        return False

    def _deregister_all(self):
        challenge, auth_header = None, "Authorization"
        for _ in range(3):
            self._send(self._reg_msg(challenge, auth_header, wipe=True), self.registrar)
            resp = self._reg_wait()
            if resp is None:
                return
            start, h = resp
            code = int(start.split(" ", 2)[1])
            if code in (401, 407):
                key = "www-authenticate" if code == 401 else "proxy-authenticate"
                challenge = _challenge(h.get(key, ""))
                auth_header = "Authorization" if code == 401 else "Proxy-Authorization"
                continue
            return

    def _caller_number(self, from_hdr):
        m = re.search(r"sip:(\+?\d+)@", from_hdr) or re.search(r'"(\+?\d+)"', from_hdr)
        return m.group(1) if m else "unknown"

    def _contact_uri(self, h):
        m = re.search(r"<(sip:[^>]+)>", h.get("contact", ""))
        return m.group(1) if m else None
