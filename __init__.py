# -*- coding: utf-8 -*-
"""
Cue Assist -- Designer-side helper (Python 2.7, runs inside Designer).

Keeps an Eos cue list in step with the CUE tags on Designer's current track.
The plugin window calls these through Designer's /api/session/python/execute.
Every public function returns a JSON string.

    info()                               version, current track, machine, caps
    start(kind, *args)                   begin an Eos job, then call step() until
    step()                               it answers running=false (see JOBS):
        every job starts (ip, port, osc), osc = 'slip' (OSC 1.1) | 'len' (OSC 1.0)
        plan(..., clist, entries)            what Write and Delete would do
        write(..., clist, entries, chan, user, pxx, pyy, pzz, expect, max_ops)
        delete(..., clist, entries, user, expect, max_ops)
        compare(..., main_list, video_list)  video cues with no main cue of that number
        set_mode(..., 'live' | 'blind')      the console's own screen
        ping(...)                            is Eos there (and its version)
    probe()                              raw dump of the track's cue entries

WHERE THE CUES COME FROM
    The window reads the track's tags and notes from Designer's REST API
    (/api/session/transport/annotations?name=<track>) and passes them in as
    `entries`: one line per time, "seconds<TAB>CUE tag value<TAB>note".
    Python's own Tag.text came back empty on r34.2 (27 Oct), while the REST
    API gives the value ("10.20.30"). entries=None falls back to reading
    track.cues here.

HOW A CUE MAPS
    Designer CUE tag  XX.YY.ZZ  (the disguise transport fixture's Cue XX / YY / ZZ)
    -> Eos cue  <list> / (XX*100 + YY).ZZ     01.20.30 -> 120.3, 92.30.41 -> 9230.41
       recorded with channel <chan> holding Cue XX / YY / ZZ (Record_Only, so
       nothing else goes in). Commands run as Eos user 0 by default: user 1
       sat in "BLIND: Setup" and recorded nothing (27 Oct). user "local" runs
       them as the console's own OSC user, as Eos reports it.
    -> labelled with the note at the tag's time; if there is none, the last
       note after the previous CUE tag.
    YY and ZZ must be 0-99 to fit that numbering; other tags are skipped and
    reported.

EOS LINK
    OSC over TCP straight from Designer's Python, framed as OSC 1.1 (SLIP,
    Eos port 3037, the default) or OSC 1.0 (packet length, port 3032), as set
    in the window. Eos needs OSC RX and TX on in
    Setup > System > Show Control. This Python runs on Designer's main thread,
    so nothing here waits for Eos: one connection stays open between calls
    and every job runs in short steps (see jobs). Eos sends about one message
    every 31 ms, four per cue, so reading a list takes ~125 ms per cue.

Documented API used (developer.disguise.one d3.pyi):
    guisystem.track                        current track
    track.cues                             TimeSequence[Cue]: n(), getT(i), getV(i)
    track.cueBeats() / cueAtBeat(beat)     fallback walk of the same entries
    cue.getTags() -> [Tag]; tag.type (TagType: TC 0, CUE 1, MIDI 2), tag.text
    cue.note / cue.getNote()
    track.beatToTime(beat)
    d3NetManager.localMachine.hostname / .role
"""
try:
    from d3 import *
except:
    pass


class Problem(ValueError):
    """The plugin's own errors. Never raise Exception here: `from d3 import *`
    replaces it with d3's Exception class, which can't be built from a string
    (see gus_layertools)."""

VERSION = '0.9.3'

TAG_CUE = 1                       # TagType.CUE
DEFAULT_PORT = 3037
OSC_SLIP, OSC_LEN = u'slip', u'len'   # OSC 1.1 (SLIP framing, Eos TCP 3037) / OSC 1.0 (packet length, 3032)
DEFAULT_OSC = OSC_SLIP
CONNECT_TIMEOUT = 0.75            # seconds; the one wait that holds Designer (unreachable IP)
SEND_TIMEOUT = 2.0                # seconds
READ_TIMEOUT = 3.0                # seconds before Eos' first answer to a read
CUE_READ_TIME = 0.25              # seconds per cue on top (Eos: ~125 ms per cue)
MODE_TIMEOUT = 2.0                # seconds for Live / Blind to show on the console
PING_TIMEOUT = 3.0                # a fresh connection took 1.6 s to answer a ping
STEP_GUESS = 0.05                 # seconds between steps (the window's STEP_MS)
WOULD_BLOCK = (11, 35, 10035)     # EAGAIN (Linux, macOS), WSAEWOULDBLOCK: nothing to read yet
LABEL_MAX = 64
LABEL_STRIP = u'#/\r\n\t"'        # characters Eos' command line could read as syntax
PARAM_DEFAULTS = (u'Cue', u'Cue 2', u'Cue 3')   # disguise d3_Server_Timeline_Module_v14: XX, YY, ZZ
DEFAULT_USER = u'0'               # Eos background user: always in Live, never the programmer's command line
LOCAL_USER = u'local'             # user setting for the console's own OSC user (whatever Eos reports)
WHEEL_TIMEOUT = 1.5               # seconds to wait for the channel's wheel names
SETTLE_TRIES, SETTLE_WAIT = 5, 0.4  # checks that the cues sent landed, and the wait between them
LIST_TRIES = 3                    # reads of a cue list that changed while being read
EOS_MODES = ('live', 'blind')     # /eos/key/<mode>, no argument = press + release
DMX_MAX = 255
try:
    CUE_IN_TEXT = __import__('re').compile(u'\\d+(\\.\\d+){0,2}')   # a cue number inside str(Tag)
except:
    CUE_IN_TEXT = None

try:
    unicode
except NameError:
    unicode = str
    basestring = str
    long = int


# ---------------------------------------------------------------- JSON

def _esc(s):
    out = []
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == '\\':
            out.append('\\\\')
        elif o < 32 or o > 126:
            out.append('\\u%04x' % o if o < 65536 else '?')
        else:
            out.append(ch)
    return ''.join(out)


def _dumps(v):
    if v is None:
        return 'null'
    if v is True:
        return 'true'
    if v is False:
        return 'false'
    if isinstance(v, (int, long, float)):
        return repr(v).rstrip('L')
    if isinstance(v, basestring):
        if isinstance(v, str) and not isinstance(v, unicode):
            try:
                v = v.decode('utf-8')
            except:
                v = v.decode('latin-1')
        return '"' + _esc(v) + '"'
    if isinstance(v, dict):
        return '{' + ','.join(_dumps(unicode(k)) + ':' + _dumps(x) for k, x in v.items()) + '}'
    if isinstance(v, (list, tuple)):
        return '[' + ','.join(_dumps(x) for x in v) + ']'
    return _dumps(unicode(repr(v)))


def _ok(**kw):
    kw['ok'] = True
    return _dumps(kw)


def _fail(e, **kw):
    kw['ok'] = False
    kw['error'] = unicode(e)
    return _dumps(kw)


def _errmsg(e):
    try:
        return u'%s: %s' % (type(e).__name__, e)
    except:
        return u'unprintable error'


def _guard(fn, *a):
    """Run fn; whatever is raised, answer with JSON. No sys/traceback imports:
    Designer blocks them."""
    try:
        return fn(*a)
    except Problem as e:
        return _dumps({'ok': False, 'error': unicode(e)})
    except BaseException as e:
        return _dumps({'ok': False, 'error': _errmsg(e)})
    except:
        return _dumps({'ok': False, 'error': u'non-standard error'})


def _mod(name):
    try:
        return __import__(name)
    except:
        return None


# ---------------------------------------------------------------- timeline

def _gs():
    try:
        return guisystem
    except NameError:
        return None


def _track():
    gs = _gs()
    return getattr(gs, 'track', None) if gs else None


def _cls(name):
    """Class from the d3 names this module received (never imports)."""
    c = globals().get(name)
    if c is None:
        try:
            c = __builtins__.get(name) if isinstance(__builtins__, dict) else getattr(__builtins__, name, None)
        except:
            c = None
    return c


def _name(o):
    for n in ('description', 'name'):
        try:
            v = getattr(o, n)
            if v:
                return unicode(v)
        except:
            pass
    return u''


def _entries(track):
    """[(beat, Cue)] for every cue entry on the track, in time order."""
    out = []
    try:
        seq = track.cues
        for i in range(seq.n()):
            out.append((float(seq.getT(i)), seq.getV(i)))
        return out
    except:
        out = []
    try:
        for b in track.cueBeats():
            c = track.cueAtBeat(b)
            if c is not None:
                out.append((float(b), c))
    except:
        pass
    out.sort(key=lambda x: x[0])
    return out


def _tag_value(t):
    """The number of a Tag. .text was empty on r34.2, so also try other
    names and the Tag's own string form."""
    for a in ('text', 'value', 'tagText'):
        try:
            v = unicode(getattr(t, a) or u'').strip()
            if v:
                return v
        except:
            pass
    for f in (unicode, repr) if CUE_IN_TEXT else ():
        try:
            m = CUE_IN_TEXT.search(f(t))
            if m:
                return m.group(0)
        except:
            pass
    return u''


def _cue_tag_text(cue):
    """Value of the CUE tag on a cue entry ('' if unreadable), or None if it
    has none."""
    tags = []
    tt = _cls('TagType')
    try:
        tags.append(cue.getTag(tt.CUE))
    except:
        pass
    try:
        tags.extend(cue.getTags())
    except:
        pass
    found = None
    for t in tags:
        try:
            if t is None or int(t.type) != TAG_CUE:
                continue
        except:
            continue
        v = _tag_value(t)
        if v:
            return v
        found = u''
    return found


def _note(cue):
    for get in (lambda: cue.note, lambda: cue.getNote()):
        try:
            v = get()
            if v:
                return unicode(v).strip()
        except:
            pass
    return u''


def _parse_cue(text):
    """'1', '1.5', '01.20.30', 'CUE 1.5' -> (1, 5, 0) or None."""
    t = text.strip()
    if t.upper().startswith(u'CUE'):
        t = t[3:].strip()
    parts = t.split(u'.')
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts] + [0, 0]
    return tuple(nums[:3])


def _eos_key(s):
    """Eos cue number as a comparable key: '1.50' -> '1.5', '3.0' -> '3'."""
    s = unicode(s).strip()
    if u'.' in s:
        a, b = s.split(u'.', 1)
        b = b.rstrip(u'0')
        a = a.lstrip(u'0') or u'0'
        return a + (u'.' + b if b else u'')
    return s.lstrip(u'0') or u'0'


def _clean_label(s):
    s = u''.join(u' ' if ch in LABEL_STRIP else ch for ch in s)
    return u' '.join(s.split())[:LABEL_MAX]


def _secs(track, beat):
    try:
        return float(track.beatToTime(beat))
    except:
        return None


def _parse_entries(text):
    """Window-supplied lines "seconds<TAB>tag<TAB>note" -> [(seconds, tag or None, note)]."""
    out = []
    for line in unicode(text).split(u'\n'):
        if not line.strip():
            continue
        f = (line.split(u'\t') + [u'', u''])[:3]
        try:
            t = float(f[0])
        except:
            continue
        out.append((t, f[1].strip() or None, f[2].strip()))
    out.sort(key=lambda x: x[0])
    return out


def _track_entries(track):
    """Same shape as _parse_entries, read from track.cues in Python."""
    return [(_secs(track, beat), _cue_tag_text(c), _note(c)) for beat, c in _entries(track)]


def _eos_number(x, y, z):
    """XX.YY.ZZ -> Eos cue number (XX*100 + YY).ZZ: 1.20.30 -> 120.3."""
    return _eos_key(u'%d.%02d' % (x * 100 + y, z))


def _timeline_data(entries=None):
    """Designer's CUE tags as the cues Eos should have.
    Returns (track name, cues, warnings); cues are dicts with key (Eos number)."""
    track = _track()
    if entries is None:
        if track is None:
            raise Problem('no current track (guisystem.track)')
        rows = _track_entries(track)
    else:
        rows = _parse_entries(entries)
    cues, warnings, used = [], [], {}
    pending_note = u''
    for sec, text, note in rows:
        if text is None:
            if note:
                pending_note = note       # note between cues: label for the next one
            continue
        label = _clean_label(note or pending_note)
        pending_note = u''
        where = u'CUE %s @ %s' % (text or u'(empty)', _fmt_time(sec))
        nums = _parse_cue(text)
        if nums is None:
            warnings.append(u'%s: not a cue number, skipped' % where)
            continue
        x, y, z = nums
        if y > 99 or z > 99:
            warnings.append(u'%s: YY and ZZ must be 0-99 to make an Eos number; skipped' % where)
            continue
        if x > DMX_MAX:
            warnings.append(u'%s: XX is over %d, the transport fixture can\'t send it; skipped' % (where, DMX_MAX))
            continue
        num = _eos_number(x, y, z)
        if num in used:
            warnings.append(u'%s: Eos cue %s is already CUE %s; skipped' % (where, num, used[num]))
            continue
        used[num] = text
        cues.append({'key': num, 'num': num, 'tag': text, 'xx': x, 'yy': y, 'zz': z,
                     'time': sec, 'label': label})
    return (_name(track) if track is not None else u''), cues, warnings


def _fmt_time(sec):
    if sec is None:
        return u'?'
    m, s = divmod(max(0.0, sec), 60.0)
    return u'%d:%05.2f' % (int(m), s)


def _signature(cues, warnings):
    """Changes whenever a CUE tag or its label is added, removed or edited."""
    return _hash(u'|'.join(u'%s=%s' % (c['tag'], c['label']) for c in cues) + u'#' + unicode(len(warnings)))


def _hash(text):
    h1, h2 = 1, 0
    for ch in text:
        h1 = (h1 + ord(ch)) % 65521
        h2 = (h2 + h1) % 65521
    return u'%04x%04x' % (h2, h1)


# ---------------------------------------------------------------- OSC

def _b(s):
    if isinstance(s, unicode):
        s = s.encode('utf-8')
    return bytearray(s)


def _pad(buf):
    buf.append(0)
    while len(buf) % 4:
        buf.append(0)
    return buf


def _i32(v):
    v = int(v) & 0xFFFFFFFF
    return bytearray([(v >> 24) & 255, (v >> 16) & 255, (v >> 8) & 255, v & 255])


def _osc(addr, *args):
    out = _pad(_b(addr))
    tags, data = u',', bytearray()
    for a in args:
        if isinstance(a, bool):
            tags += u'T' if a else u'F'
        elif isinstance(a, (int, long)):
            tags += u'i'
            data += _i32(a)
        elif isinstance(a, float):
            st = _mod('struct')
            if st is None:
                tags += u'i'
                data += _i32(int(round(a)))
            else:
                tags += u'f'
                data += bytearray(st.pack('>f', a))
        else:
            tags += u's'
            data += _pad(_b(unicode(a)))
    return bytes(out + _pad(_b(tags)) + data)


def _find(buf, val, i=0):
    """Index of byte val in buf from i, or -1 (py2 bytearray.index may not take an int)."""
    for j in range(i, len(buf)):
        if buf[j] == val:
            return j
    return -1


def _rd_str(buf, i):
    j = _find(buf, 0, i)
    if j < 0:
        raise Problem('unterminated OSC string')
    s = bytes(buf[i:j]).decode('utf-8', 'replace')
    j += 1
    while j % 4:
        j += 1
    return s, j


def _rd_i32(buf, i):
    v = (buf[i] << 24) | (buf[i + 1] << 16) | (buf[i + 2] << 8) | buf[i + 3]
    return v - 0x100000000 if v & 0x80000000 else v


def _parse_osc(buf):
    """bytearray -> [(address, [args])] (bundles flattened)."""
    if buf[:8] == bytearray(b'#bundle\x00'):
        out, i = [], 16
        while i + 4 <= len(buf):
            n = _rd_i32(buf, i)
            out.extend(_parse_osc(buf[i + 4:i + 4 + n]))
            i += 4 + n
        return out
    addr, i = _rd_str(buf, 0)
    if i >= len(buf):
        return [(addr, [])]
    tags, i = _rd_str(buf, i)
    args = []
    st = _mod('struct')
    for t in tags[1:]:
        if t == u'i':
            args.append(_rd_i32(buf, i))
            i += 4
        elif t == u'f':
            args.append(st.unpack('>f', bytes(buf[i:i + 4]))[0] if st else None)
            i += 4
        elif t in u'hd':
            args.append(None)
            i += 8
        elif t == u's':
            s, i = _rd_str(buf, i)
            args.append(s)
        elif t == u'b':
            n = _rd_i32(buf, i)
            i += 4 + n
            while i % 4:
                i += 1
            args.append(None)
        elif t == u'T':
            args.append(True)
        elif t == u'F':
            args.append(False)
        else:
            args.append(None)
    return [(addr, args)]


SLIP_END, SLIP_ESC, SLIP_ESC_END, SLIP_ESC_ESC = 0xC0, 0xDB, 0xDC, 0xDD


class _Eos(object):
    """One TCP connection to Eos, never waited on. It stays open between calls
    (see _conn): a fresh connection answered erratically for its first ~1.5 s,
    up to 1.6 s for a ping (27 Oct). Reads only take what has already arrived
    (pump), so no call holds Designer's main thread while Eos thinks."""

    def __init__(self, ip, port, osc):
        sock = _mod('socket')
        if sock is None:
            raise Problem('Designer\'s Python blocks the socket module, so the plugin can\'t reach Eos')
        if not ip:
            raise Problem('set the Eos IP in settings (cog)')
        self.key = (ip, port, osc)
        self.port = port
        self.slip = osc == OSC_SLIP
        try:
            self.s = sock.create_connection((ip, port), CONNECT_TIMEOUT)
        except BaseException as e:
            raise Problem(u'no Eos at %s:%d (%s). Check the IP, and that OSC RX/TX are on in Eos Show Control'
                          % (ip, port, _errmsg(e)))
        self.s.setblocking(0)
        self.buf = bytearray()
        self.inbox = []                # parsed messages not looked at yet
        self.dead = False
        self.users = set()             # Eos users this job sent commands as
        self.errors = []               # their command-line errors, as Eos echoed them
        # The console's own OSC user: the first /eos/out/user Eos sends, as soon
        # as a connection opens. Later ones follow whatever /eos/user/<n> this
        # connection last used (/eos/out/user [0] after a user-0 command, 27 Oct),
        # so only one naming a user we never sent as replaces it. Eos has no
        # query for it (/eos/get/user gets no answer, 28 Oct).
        self.console_user = None
        self.user_seen = False
        self.sent_users = set()        # /eos/user/<n> numbers this connection has sent to
        self.modes = {}                # user -> 'LIVE' / 'BLIND' / ..., from its command-line echo
        self.framing_ok = False        # Eos' first bytes matched the OSC setting (_check_framing)

    def close(self):
        self.dead = True
        try:
            self.s.close()
        except:
            pass

    def send(self, addr, *args):
        if addr.startswith(u'/eos/user/'):
            self.sent_users.add(addr.split(u'/')[3])
        msg = bytearray(_osc(addr, *args))
        if self.slip:
            out = bytearray([SLIP_END])
            for c in msg:
                if c == SLIP_END:
                    out += bytearray([SLIP_ESC, SLIP_ESC_END])
                elif c == SLIP_ESC:
                    out += bytearray([SLIP_ESC, SLIP_ESC_ESC])
                else:
                    out.append(c)
            out.append(SLIP_END)
        else:
            out = _i32(len(msg)) + msg
        try:
            self.s.settimeout(SEND_TIMEOUT)
            self.s.sendall(bytes(out))
            self.s.setblocking(0)
        except BaseException as e:
            self.close()
            raise Problem(u'lost the connection to Eos (%s); try again to reconnect' % _errmsg(e))

    def _frames(self):
        out = []
        if self.slip:
            while True:
                j = _find(self.buf, SLIP_END)
                if j < 0:
                    break
                raw, self.buf = self.buf[:j], self.buf[j + 1:]
                if not raw:
                    continue
                frame, k = bytearray(), 0
                while k < len(raw):
                    c = raw[k]
                    if c == SLIP_ESC and k + 1 < len(raw):
                        k += 1
                        c = SLIP_END if raw[k] == SLIP_ESC_END else SLIP_ESC
                    frame.append(c)
                    k += 1
                out.append(frame)
        else:
            while len(self.buf) >= 4:
                n = _rd_i32(self.buf, 0)
                if n < 0 or len(self.buf) < 4 + n:
                    break
                out.append(self.buf[4:4 + n])
                self.buf = self.buf[4 + n:]
        return out

    def pump(self):
        """Move whatever Eos has sent into inbox, without waiting for more."""
        while True:
            try:
                data = self.s.recv(65536)
            except BaseException as e:
                code = getattr(e, 'errno', None)
                if code is None and getattr(e, 'args', None):
                    code = e.args[0]
                if code in WOULD_BLOCK:
                    break                      # nothing more right now
                self.close()
                raise Problem(u'lost the connection to Eos (%s); try again to reconnect' % _errmsg(e))
            if not data:
                self.close()
                raise Problem('Eos closed the connection; try again to reconnect')
            self.buf += bytearray(data)
        self._check_framing()
        for f in self._frames():
            try:
                msgs = _parse_osc(f)
            except BaseException:
                continue
            for addr, args in msgs:
                self._note_error(addr, args)
                if addr == u'/eos/out/user' and args:
                    n = unicode(args[0])
                    if not self.user_seen or n not in self.sent_users:
                        self.user_seen = True
                        self.console_user = n
                elif addr.startswith(u'/eos/out/user/') and addr.endswith(u'/cmd') and args \
                        and isinstance(args[0], basestring):
                    self.modes[addr.split(u'/')[4]] = args[0].split(u':', 1)[0].strip()
                self.inbox.append((addr, args))

    def _check_framing(self):
        """Eos frames its first message the way its TCP format is set: SLIP
        starts with 0xC0, OSC 1.0 with a 4-byte length (first byte 0). On the
        wrong setting Eos ignores everything and the plugin only sees silence
        (27 Oct: this Eos answers SLIP on both 3032 and 3037), so say so."""
        if self.framing_ok or len(self.buf) < 4:
            return
        slip_seen = self.buf[0] == SLIP_END
        if slip_seen != self.slip:
            self.close()
            raise Problem(u'Eos is talking OSC %s on this port, but the plugin is set to OSC %s. Set OSC to %s in '
                          u'settings (cog), or change the TCP format in Eos Setup > System > Show Control'
                          % ((u'v1.1 SLIP', u'v1.0 packet length', u'v1.1 SLIP') if slip_seen else
                             (u'v1.0 packet length', u'v1.1 SLIP', u'v1.0 packet length')))
        self.framing_ok = True

    def _note_error(self, addr, args):
        """Keep Eos' 'Error' echoes for our users: /eos/out/user/<n>/cmd ['LIVE: ... Error: ...']."""
        if not args or not isinstance(args[0], basestring) or u'Error' not in args[0]:
            return
        if any(addr == u'/eos/out/user/%s/cmd' % u for u in self.users) and args[0] not in self.errors:
            self.errors.append(args[0])

    def cmd(self, user, text):
        p = _prefix(user)
        self.users.add(p.rsplit(u'/', 1)[-1] if p != u'/eos' else u'')
        self.send(p + u'/newcmd', text)


_CONN = {}


def _osc_mode(osc):
    o = unicode(osc or u'').strip().lower()
    if o in (OSC_LEN, u'1.0', u'osc 1.0', u'length'):
        return OSC_LEN
    return OSC_SLIP


def _conn(ip, port, osc=DEFAULT_OSC):
    """The open connection to ip:port, or a new one. Messages that arrived
    while no job was running are thrown away."""
    ip = unicode(ip or u'').strip()
    port = int(port or DEFAULT_PORT)
    osc = _osc_mode(osc)
    eos = _CONN.get('eos')
    if eos is not None and (eos.dead or eos.key != (ip, port, osc)):
        eos.close()
        eos = None
    if eos is not None:
        try:
            eos.pump()
        except Problem:
            eos = None
    if eos is None:
        eos = _Eos(ip, port, osc)
        _CONN['eos'] = eos
    eos.inbox, eos.errors, eos.users = [], [], set()
    return eos


# ---------------------------------------------------------------- jobs
# Every Eos operation is a generator that yields whenever it has to wait for
# Eos. The window calls start() once and then step() every STEP_MS until the
# job is done, so each call into Designer's Python takes milliseconds instead
# of the 0.7-4 s that blocking reads froze Designer for (27 Oct). Designer
# refuses `import threading`, so this is the way to keep it responsive.
# Python 2.7: no `yield from` and no return values from generators, so a
# sub-step runs as `for _ in sub(..., out): yield` and leaves its result in out.

_CLOCK = [0.0]                   # used only if Designer's Python has no time module


def _now():
    t = _mod('time')
    return t.time() if t is not None else _CLOCK[0]


def _wait(eos, done, timeout, out):
    """Feed incoming messages to done(addr, args) until it returns True
    (out['ok'] = True) or timeout runs out (out['ok'] = False)."""
    end = _now() + timeout
    while True:
        eos.pump()
        msgs, eos.inbox = eos.inbox, []
        for i, (addr, args) in enumerate(msgs):
            if done(addr, args):
                eos.inbox = msgs[i + 1:] + eos.inbox
                out['ok'] = True
                return
        if _now() > end:
            out['ok'] = False
            return
        yield


def _sleep(secs):
    end = _now() + secs
    while _now() < end:
        yield


def _g_count(eos, clist, out):
    base = u'/eos/out/get/cue/%s/count' % clist
    st = {}

    def got(addr, args):
        if addr == base and args:
            st['count'] = int(args[0])
            return True
        return False
    eos.send(u'/eos/get/cue/%s/count' % clist)
    r = {}
    for _ in _wait(eos, got, READ_TIMEOUT, r):
        yield
    if not r['ok']:
        raise Problem(u'Eos didn\'t answer the cue count for list %s (is OSC TX on?)' % clist)
    out['count'] = st['count']


def _g_list_once(eos, clist, out):
    """out: cues {key: {'num', 'label'}}, n = the count asked first, or None
    if not every cue came (cues went away mid-read)."""
    base = u'/eos/out/get/cue/%s/' % clist
    r = {}
    for _ in _g_count(eos, clist, r):
        yield
    n = r['count']
    cues, seen = {}, set()
    out['cues'], out['n'] = cues, n
    if n <= 0:
        return
    for i in range(n):
        eos.send(u'/eos/get/cue/%s/index/%d' % (clist, i))

    def got(addr, args):
        # /eos/out/get/cue/<list>/<cue>/<part>/list/<index>/<arg count>  [index, uid, label, ...]
        if not addr.startswith(base):
            return False
        p = addr[len(base):].split(u'/')
        if len(p) >= 3 and p[2] == u'list':
            seen.add(args[0] if args else p[3] if len(p) > 3 else len(seen))
            if p[1] == u'0':
                label = args[2] if len(args) > 2 and isinstance(args[2], basestring) else u''
                cues[_eos_key(p[0])] = {'num': p[0], 'label': label}
        return len(seen) >= n
    w = {}
    out['stage'] = u'reading %d Eos cues' % n
    for _ in _wait(eos, got, READ_TIMEOUT + n * CUE_READ_TIME, w):
        yield
    if not w['ok']:
        out['n'] = None


def _g_list(eos, clist, out):
    """{key: {'num', 'label'}} for every cue in Eos cue list clist, in out['cues'].
    Read again if the list changed while it was being read: straight after a
    write Eos gave the old count, the new cue sorted in first and the last cue
    fell off the end, so it looked missing and the next batch was refused
    (27 Oct). So the count is asked again at the end and must still match.
    (The last number in a reply's address is its argument count, not the
    list's.)"""
    clist = unicode(clist).strip()
    for attempt in range(LIST_TRIES):
        r = {}
        for _ in _g_list_once(eos, clist, r):
            out['stage'] = r.get('stage', u'')
            yield
        if r['n'] is None:
            continue
        c = {}
        for _ in _g_count(eos, clist, c):
            yield
        if c['count'] == r['n']:
            out['cues'] = r['cues']
            return
    raise Problem(u'Eos cue list %s kept changing while it was read; press Refresh' % clist)


def _g_cues(eos, clist, nums, out):
    """Ask Eos about just these cue numbers. out['cues']: {key: label}, or
    {key: False} when Eos says there is no such cue (/eos/out/get/cue/<list>/<n>
    with no arguments); a key is missing if Eos didn't answer in time.
    Much cheaper than re-reading the list: Eos sends one message every ~31 ms
    and four per cue (list, fx, links, actions)."""
    base = u'/eos/out/get/cue/%s/' % clist
    res, st = {}, {'got': 0}
    out['cues'] = res
    if not nums:
        return
    for n in nums:
        eos.send(u'/eos/get/cue/%s/%s' % (clist, n))

    def got(addr, args):
        if not addr.startswith(base):
            return False
        p = addr[len(base):].split(u'/')
        if len(p) == 1:
            res[_eos_key(p[0])] = False
            st['got'] += 1
        elif len(p) >= 3 and p[1] == u'0' and p[2] == u'list':
            res[_eos_key(p[0])] = args[2] if len(args) > 2 and isinstance(args[2], basestring) else u''
            st['got'] += 1
        return st['got'] >= len(nums)
    w = {}
    for _ in _wait(eos, got, READ_TIMEOUT + len(nums) * CUE_READ_TIME, w):
        yield


def _g_verify(eos, clist, done, kind, out):
    """Ask Eos about each cue sent until all of them show as wanted, or
    SETTLE_TRIES run out. Eos answers /eos/get before it has run the command
    lines queued ahead of it (27 Oct), so it can take a second try.
    out['failed']: the ops that never showed."""
    pending = list(done)
    for attempt in range(SETTLE_TRIES):
        if not pending:
            break
        if attempt:
            for _ in _sleep(SETTLE_WAIT):
                yield
        r = {}
        for _ in _g_cues(eos, clist, [d['num'] for d in pending], r):
            yield
        still = []
        for d in pending:
            have = r['cues'].get(_eos_key(d['num']))
            if kind == 'delete':
                landed = have is False
            else:
                landed = have is not None and have is not False and \
                    (not d['label'] or _clean_label(have) == d['label'])
            if not landed:
                still.append(d)
        pending = still
    out['failed'] = pending


def _g_param_addrs(eos, user, chan, names, out):
    """Select chan and build each parameter's OSC address from the wheel label
    Eos reports for it, in out['addrs']. Eos makes the OSC name from the exact
    label, lower case, spaces to '_', a leading space included: the v14
    disguise profile's ' Cue' is /param/_cue, and /param/cue is read as the
    Cue key (syntax error, 27 Oct). Raises before anything is recorded if a
    name is missing."""
    wheels = {}
    want = [_norm(n) for n in names]

    def got(addr, args):
        if addr.startswith(u'/eos/out/active/wheel/') and args and isinstance(args[0], basestring):
            label = args[0]
            i = label.rfind(u'[')
            name = (label[:i] if i >= 0 else label).rstrip()   # keep a leading space
            if name.strip():
                wheels[_norm(name)] = name
        return all(w in wheels for w in want)
    eos.cmd(user, u'Chan %s Enter' % chan)
    r = {}
    for _ in _wait(eos, got, WHEEL_TIMEOUT, r):
        yield
    missing = [n for n, w in zip(names, want) if w not in wheels]
    if missing:
        raise Problem(u'channel %s has no %s parameter (it has: %s). Set the names under Advanced; nothing was recorded'
                      % (chan, u', '.join(u'"%s"' % m for m in missing),
                         u', '.join(sorted(n.strip() for n in wheels.values())) or u'no wheels reported; is it patched?'))
    out['addrs'] = [_prefix(user) + u'/param/' + wheels[w].lower().replace(u' ', u'_') for w in want]


def _cue_ref(clist, num):
    """'2 / 0.1'. Eos 2.7+ needs the spaces: 'Cue 2/0.1' is misread and gives
    a syntax error (seen 27 Oct, 'Delete Cue 2/0.1'). Commands end in Enter,
    not '#', which newer Eos no longer takes as Enter over OSC."""
    return u'%s / %s' % (unicode(clist).strip(), num)


def _prefix(user):
    u = unicode(user if user is not None else u'').strip() or DEFAULT_USER
    return u'/eos/user/%d' % int(u) if u.isdigit() else u'/eos'


def _norm(name):
    return u' '.join(unicode(name).lower().split())


# ---------------------------------------------------------------- plan

def _make_plan(cues, eos_cues):
    adds, relabels, deletes = [], [], []
    keys = set()
    for c in cues:
        keys.add(c['key'])
        have = eos_cues.get(c['key'])
        if have is None:
            adds.append(c)
        elif c['label'] and _clean_label(have['label']) != c['label']:
            relabels.append(dict(c, old=have['label'], eos=have['num']))
    for k, e in eos_cues.items():
        if k not in keys:
            deletes.append({'key': k, 'num': e['num'], 'label': e['label']})
    deletes.sort(key=lambda d: _num_sort(d['num']))
    return adds, relabels, deletes


def _num_sort(num):
    try:
        return float(num)
    except:
        return 0.0


def _plan_hashes(adds, relabels, deletes):
    w = _hash(u'|'.join([u'+%s:%d.%d.%d:%s' % (a['num'], a['xx'], a['yy'], a['zz'], a['label']) for a in adds] +
                        [u'~%s:%s' % (r['num'], r['label']) for r in relabels]))
    d = _hash(u'|'.join(u'-%s' % x['num'] for x in deletes))
    return w, d


def _plan_out(p):
    wh, dh = _plan_hashes(p['adds'], p['relabels'], p['deletes'])
    strip = lambda c: dict((k, c[k]) for k in c if k in ('num', 'tag', 'label', 'time', 'old', 'xx', 'yy', 'zz'))
    return dict(track=p['track'], d3count=len(p['cues']), eosCount=len(p['eos']),
                adds=[strip(a) for a in p['adds']], relabels=[strip(r) for r in p['relabels']],
                deletes=p['deletes'], warnings=p['warnings'], writeHash=wh, deleteHash=dh,
                signature=_signature(p['cues'], p['warnings']))


def _g_plan(eos, clist, entries, out):
    """Timeline vs Eos list: out gets track, cues, warnings, eos, adds, relabels, deletes."""
    track, cues, warnings = _timeline_data(entries)
    r = {}
    for _ in _g_list(eos, clist, r):
        out['stage'] = r.get('stage', u'')
        yield
    adds, relabels, deletes = _make_plan(cues, r['cues'])
    out.update(track=track, cues=cues, warnings=warnings, eos=r['cues'],
               adds=adds, relabels=relabels, deletes=deletes)


def _job_plan(out, ip, port, osc, clist, entries=None):
    eos = _conn(ip, port, osc)
    p = {}
    for _ in _g_plan(eos, unicode(clist).strip(), entries, p):
        out['stage'] = p.get('stage', u'')
        yield
    out['result'] = _plan_out(p)


def _g_console_user(eos, out):
    """The console's own OSC user (its local user), from the first
    /eos/out/user a connection gets. Waits for it on a fresh connection."""
    if eos.console_user is None:
        w = {}
        for _ in _wait(eos, lambda a, x: eos.console_user is not None, MODE_TIMEOUT, w):
            yield
        if eos.console_user is None:
            raise Problem('Eos didn\'t say which user it runs OSC as; reconnect and try again')
    out['user'] = eos.console_user


def _g_user(eos, user, out):
    """User setting -> Eos user number: 'local' is the console's own user,
    anything else is used as given (blank = DEFAULT_USER)."""
    if unicode(user if user is not None else u'').strip().lower() == LOCAL_USER:
        out['stage'] = u'asking Eos for its local user'
        for _ in _g_console_user(eos, out):
            yield
    else:
        out['user'] = unicode(user if user is not None else u'').strip() or DEFAULT_USER


def _live(eos, user):
    """Put the plugin's own Eos user in Live before it records or deletes: a
    user in Blind or Setup records nothing (27 Oct), and a Live/Blind press on
    a kept-open connection once landed on user 0 (see _job_set_mode)."""
    eos.send(_prefix(user) + u'/key/live')


def _job_write(out, ip, port, osc, clist, entries, chan, user, pxx, pyy, pzz, expect=u'', max_ops=0):
    chan = unicode(chan).strip()
    clist = unicode(clist).strip()
    if not chan:
        raise Problem('set the disguise fixture channel in settings (cog)')
    params = [unicode(p).strip() or d for p, d in zip((pxx, pyy, pzz), PARAM_DEFAULTS)]
    eos = _conn(ip, port, osc)
    u = {}
    for _ in _g_user(eos, user, u):
        out['stage'] = u.get('stage', u'')
        yield
    user = u['user']
    _live(eos, user)
    p = {}
    for _ in _g_plan(eos, clist, entries, p):
        out['stage'] = p.get('stage', u'')
        yield
    wh, _ = _plan_hashes(p['adds'], p['relabels'], p['deletes'])
    if expect and wh != expect:
        raise Problem('the timeline or Eos changed since Refresh; press Refresh and check the readout again')
    budget = int(max_ops or 0) or 1000000
    adds, relabels = p['adds'][:budget], p['relabels'][:max(0, budget - len(p['adds']))]
    addrs = []
    if adds:
        out['stage'] = u'checking channel %s' % chan
        w = {}
        for _ in _g_param_addrs(eos, user, chan, params, w):
            yield
        addrs = w['addrs']
    done = []
    for a in adds:
        target = _cue_ref(clist, a['num'])
        eos.cmd(user, u'Chan %s Enter' % chan)
        for addr, v in zip(addrs, (a['xx'], a['yy'], a['zz'])):
            eos.send(addr, float(v))
        # Two-word keys take '_' over OSC: 'Record Only' was read as plain Record (27 Oct)
        eos.cmd(user, u'Record_Only Cue %s Enter' % target)
        if a['label']:
            eos.cmd(user, u'Cue %s Label %s Enter' % (target, a['label']))
        done.append({'op': 'add', 'num': a['num'], 'label': a['label']})
    for r in relabels:
        eos.cmd(user, u'Cue %s Label %s Enter' % (_cue_ref(clist, r['eos']), r['label']))
        done.append({'op': 'label', 'num': r['num'], 'label': r['label']})
    if done:
        eos.cmd(user, u'')              # leave that user's command line empty
    out['stage'] = u'checking %d cues' % len(done)
    v = {}
    for _ in _g_verify(eos, clist, done, 'write', v):
        yield
    out['result'] = dict(done=done, failed=v['failed'], eosErrors=eos.errors, user=user,
                         left=len(p['adds']) + len(p['relabels']) - len(done))


def _job_delete(out, ip, port, osc, clist, entries, user, expect=u'', max_ops=0):
    clist = unicode(clist).strip()
    eos = _conn(ip, port, osc)
    u = {}
    for _ in _g_user(eos, user, u):
        out['stage'] = u.get('stage', u'')
        yield
    user = u['user']
    _live(eos, user)
    p = {}
    for _ in _g_plan(eos, clist, entries, p):
        out['stage'] = p.get('stage', u'')
        yield
    _, dh = _plan_hashes(p['adds'], p['relabels'], p['deletes'])
    if expect and dh != expect:
        raise Problem('the timeline or Eos changed since Refresh; press Refresh and check the readout again')
    done = []
    for d in p['deletes'][:int(max_ops or 0) or len(p['deletes'])]:
        eos.cmd(user, u'Delete Cue %s Enter Enter' % _cue_ref(clist, d['num']))
        done.append({'op': 'delete', 'num': d['num'], 'label': d['label']})
    out['stage'] = u'checking %d cues' % len(done)
    v = {}
    for _ in _g_verify(eos, clist, done, 'delete', v):
        yield
    out['result'] = dict(done=done, failed=v['failed'], eosErrors=eos.errors, user=user,
                         left=len(p['deletes']) - len(done))


def _job_compare(out, ip, port, osc, main_list, video_list):
    """Read-only. Main cue N against video cue N: every video cue whose
    number has no cue in the main list. Numbers compare as Eos reads them
    (1.50 = 1.5)."""
    main_list, video_list = unicode(main_list).strip(), unicode(video_list).strip()
    if not main_list or not video_list:
        raise Problem('set both the main list and the video list in settings (cog)')
    if main_list == video_list:
        raise Problem(u'main list and video list are both %s' % main_list)
    eos = _conn(ip, port, osc)
    lists = {}
    for name, clist in (('main', main_list), ('video', video_list)):
        r = {}
        for _ in _g_list(eos, clist, r):
            out['stage'] = u'%s list %s: %s' % (name, clist, r.get('stage', u''))
            yield
        lists[name] = r['cues']
    main, video = lists['main'], lists['video']
    missing = [dict(num=v['num'], label=v['label']) for k, v in video.items() if k not in main]
    missing.sort(key=lambda c: _num_sort(c['num']))
    out['result'] = dict(mainList=main_list, videoList=video_list, mainCount=len(main),
                         videoCount=len(video), matched=len(video) - len(missing), missing=missing)


def _job_set_mode(out, ip, port, osc, mode):
    """Press Live or Blind for the console's own OSC user (the screen the
    operator looks at), not the plugin's background user. The user is named
    in the address: once a connection has sent /eos/user/0/..., Eos runs
    its un-numbered messages as user 0 too, so a plain /eos/key/blind put the
    background user in Blind (27 Oct). Confirmed from that user's command-line
    echo ('LIVE: ...' / 'BLIND: ...')."""
    mode = unicode(mode).strip().lower()
    if mode not in EOS_MODES:
        raise Problem(u'mode must be live or blind, not %s' % mode)
    eos = _conn(ip, port, osc)
    for _ in _g_console_user(eos, {}):
        yield
    want = mode.upper() + u':'
    echo = u'/eos/out/user/%s/cmd' % eos.console_user
    st = {'now': None}

    def got(addr, args):
        if addr == echo and args and isinstance(args[0], basestring):
            st['now'] = args[0].split(u':', 1)[0].strip()
            return args[0].startswith(want)
        return False
    eos.send(u'/eos/user/%s/key/%s' % (eos.console_user, mode))
    r = {}
    for _ in _wait(eos, got, MODE_TIMEOUT, r):
        yield
    out['result'] = dict(mode=mode, confirmed=r['ok'], now=st['now'])


def _job_ping(out, ip, port, osc):
    eos = _conn(ip, port, osc)
    st = {'pong': False, 'version': None}

    def got(addr, args):
        if addr == u'/eos/out/ping':
            st['pong'] = True
        elif addr == u'/eos/out/get/version' and args:
            st['version'] = unicode(args[0])
        return st['pong'] and st['version'] is not None
    eos.send(u'/eos/ping', u'cueassist')
    eos.send(u'/eos/get/version')
    r = {}
    for _ in _wait(eos, got, PING_TIMEOUT, r):
        yield
    if not st['pong'] and st['version'] is None:
        raise Problem('connected, but Eos sent nothing back: turn on OSC TX in Eos Show Control')
    out['result'] = dict(version=st['version'], framing=u'OSC 1.1 SLIP' if eos.slip else u'OSC 1.0 packet length')


def _eos_status_impl(ip, port, osc):
    eos = _CONN.get('eos')
    key = (unicode(ip or u'').strip(), int(port or DEFAULT_PORT), _osc_mode(osc))
    if _JOB.get('gen') is not None and eos is not None and not eos.dead and eos.key == key:
        eos.pump()                     # a job owns the connection: leave its inbox alone
    else:
        eos = _conn(ip, port, osc)
        eos.pump()
    return _ok(user=eos.console_user, modes=eos.modes)


JOBS = {'plan': _job_plan, 'write': _job_write, 'delete': _job_delete, 'compare': _job_compare,
        'set_mode': _job_set_mode, 'ping': _job_ping}
_JOB = {}


def _start_impl(kind, *args):
    _stop()
    if kind not in JOBS:
        return _fail(u'no job %s' % kind)
    out = {}
    _JOB.update(kind=kind, out=out, gen=JOBS[kind](out, *args))
    return _step_impl()


def _stop():
    gen = _JOB.get('gen')
    _JOB.clear()
    if gen is not None:
        try:
            gen.close()
        except:
            pass


def _step_impl():
    gen = _JOB.get('gen')
    if gen is None:
        return _fail('no job running', running=False)
    out = _JOB['out']
    _CLOCK[0] += STEP_GUESS
    try:
        next(gen)
    except StopIteration:
        _JOB.clear()
        res = dict(out.get('result') or {})
        res['running'] = False
        return _ok(**res)
    except BaseException as e:
        _JOB.clear()
        return _fail(e if isinstance(e, Problem) else _errmsg(e), running=False)
    return _ok(running=True, stage=out.get('stage', u''))


def _run_sync(kind, *args):
    """Whole job in one call. For scripts and tests only: it holds Designer
    the whole time, which is what the jobs are there to avoid."""
    t = _mod('time')
    r = _start_impl(kind, *args)
    while u'"running":true' in r:
        if t:
            t.sleep(STEP_GUESS)
        r = _step_impl()
    return r


# ---------------------------------------------------------------- entry points

def _machine():
    try:
        m = d3NetManager.localMachine
    except:
        return (None, None)
    host, role = None, None
    try:
        host = unicode(m.hostname)
    except:
        pass
    try:
        r = m.role
        role = unicode(r.toString() if hasattr(r, 'toString') else r)
    except:
        pass
    return (host, role)


def _info_impl():
    t = _track()
    host, role = _machine()
    return _ok(version=VERSION, track=_name(t) if t is not None else None, machine=host, role=role,
               caps={'socket': _mod('socket') is not None, 'time': _mod('time') is not None,
                     'struct': _mod('struct') is not None})


def _probe_impl():
    track = _track()
    if track is None:
        return _fail('no current track')
    rows = []
    for beat, c in _entries(track)[:200]:
        tags = []
        try:
            for t in c.getTags():
                tags.append({'type': _try(lambda: t.type.toString()), 'text': _try(lambda: t.text),
                             'str': _try(lambda: unicode(t)), 'repr': _try(lambda: repr(t)),
                             'members': [a for a in dir(t) if not a.startswith('_')]})
        except BaseException as e:
            tags.append({'error': u'getTags failed: ' + _errmsg(e)})
        rows.append({'time': _fmt_time(_secs(track, beat)), 'tags': tags, 'read': _cue_tag_text(c), 'note': _note(c)})
    return _ok(track=_name(track), entries=rows)


def _try(fn):
    try:
        return unicode(fn())
    except BaseException as e:
        return u'! ' + _errmsg(e)


def info():
    return _guard(_info_impl)


def start(kind, *args):
    """Begin an Eos job (plan, write, delete, set_mode, ping) and run its first step."""
    return _guard(_start_impl, kind, *args)


def step():
    """Run the current job on with whatever Eos has sent; running=false when finished."""
    return _guard(_step_impl)


def probe():
    return _guard(_probe_impl)


def eos_status(ip, port=DEFAULT_PORT, osc=DEFAULT_OSC):
    """For the window's poll: the console's local user and each user's mode,
    from the open connection. Opens one if there is none (up to
    CONNECT_TIMEOUT on an unreachable IP), then returns without waiting."""
    return _guard(_eos_status_impl, ip, port, osc)
