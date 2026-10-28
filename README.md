# Cue Assist

Keeps an Eos cue list in step with the CUE tags on Designer's current track: adds the cues Eos is missing,
labels them from the timeline notes, and deletes Eos cues that are no longer on the timeline.

## Use
1. Open **Cue Assist** from Designer's plugin menu. The first time, the **⚙** settings open:
   - **Eos IP**: the console (or Nomad) to talk to.
   - **Video list**: the Eos cue list the disguise cues live in.
   - **Main list**: the main (lighting) cue list **Compare** checks against (default 1).
   - **OSC**: **v1.1 SLIP** (default, Eos TCP port 3037) or **v1.0 packet length** (port 3032). Switching it moves
     the port to match if the port was the other one's default.
   - **Channel**: the Eos channel patched as the disguise fixture (the profile with Cue XX / YY / ZZ).
   - **Test** connects and shows the Eos version.
2. **Refresh** reads the timeline and the Eos list and shows what will happen. It changes nothing.
   - `+` green: Write adds this cue
   - `~` blue: Write changes this cue's label
   - `−` red: Delete removes this Eos cue (no CUE tag on the timeline)
   - `!` amber: a CUE tag that was skipped, with the reason
3. **Write** does the `+` and `~` lines. **Delete** does the `−` lines and needs a second click (**Confirm**) within 4 s.
4. **Compare main list ↔ video list** (under the buttons) is read-only. It reads both Eos lists and matches main
   cue N with video cue N (numbers as Eos reads them, so 1.50 = 1.5), then lists every video cue that has no cue with
   the same number in the main list. With 100 cues per list it takes ~25 s (Eos sends ~125 ms per cue); Designer
   stays free meanwhile.
5. **→ LIVE / → BLIND** (right of Delete) picks the mode the Eos console is put in after a Write or Delete that sent
   anything; click to switch. It presses Live or Blind for the console's own OSC user (the screen you look at),
   not the plugin's background user 0. The window then Refreshes by itself.

If the timeline or Eos changes between Refresh and Write/Delete, nothing is sent and the log asks you to Refresh.
Both run in batches of 20 cues and re-read Eos after each batch; a cue that isn't in Eos afterwards shows ✗ in the log.

## The warning
The red **TIMELINE CUES CHANGED** bar blinks when the CUE tags (or their notes) on the current track differ from
the last time the plugin synced this track with this Eos list: after a finished Write or Delete, or a Refresh
that found nothing to do. It also blinks on a track that has never been synced. The window checks the timeline
every 3 s (Designer only, no Eos traffic), and stops while the window is hidden.

## How a cue maps
| Timeline | Eos |
|---|---|
| CUE tag `XX.YY.ZZ` | cue `<list> / (XX×100 + YY).ZZ` |
| | `01.20.30` → 120.3 · `10.20.30` → 1020.3 · `92.30.41` → 9230.41 · `5` → 500 · `1.5` → 105 |
| the tag's numbers | channel *Channel*: Cue XX / YY / ZZ set to them, then **Record_Only** |
| note at the tag's time; if none, the last note since the previous CUE tag | cue label |

Skipped, with a `!` line: YY or ZZ over 99 (they don't fit the numbering), XX over 255 (the fixture can't send it),
and tag text that isn't a number.

The tags and notes are read from Designer's REST API (`/api/session/transport/annotations?name=<track>`) for the
track Designer's GUI is showing. Designer's Python returned empty tag text on r34.2, so it is only used as a fallback.

A cue with no note doesn't have its Eos label cleared. Labels lose `#`, `/`, quotes, tabs and line breaks, and stop
at 64 characters.

## Commands sent (OSC over TCP)
```
/eos/[user/N/]newcmd  "Chan <ch> Enter"
/eos/user/N/param/_cue  XX        (names read from the channel's wheels, see below)
/eos/[user/N/]newcmd  "Record_Only Cue <list> / <n> Enter"
/eos/[user/N/]newcmd  "Cue <list> / <n> Label <note> Enter"
/eos/[user/N/]newcmd  "Delete Cue <list> / <n> Enter Enter"
/eos/get/cue/<list>/count, /eos/get/cue/<list>/index/<i>     (reading the list)
```

**Write changes the live output.** Setting Cue XX/YY/ZZ on the channel is a live manual change, so if Designer is
taking transport from that fixture it will jump cues while Write runs. Turn off that DMX transport (or write on
an offline/Nomad copy) while you write. The values stay as manual data on the channel afterwards, the same as
if you'd typed them.

## Settings → Advanced
- **TCP port**: default 3037 (OSC 1.1 SLIP); 3032 for OSC 1.0. Eos needs **OSC RX** and **OSC TX** on in
  *Setup > System > Show Control*.
- **User**: the Eos user the commands run as (`/eos/user/N/...`). A user sitting in Blind or Setup records nothing;
  Write and Delete press Live for it first.
  - **Eos local user** (default): the console's own OSC user, whatever Eos reports when the plugin connects (the
    user set in *Setup > System > Show Control*). The commands then show on that user's command line, and it's left
    in Live/Blind by the **→ LIVE / → BLIND** button as usual. The menu shows the number, e.g. *Eos local user (1)*.
  - **Number** with **User no.** beside it, e.g. **0**, the background user: always in Live and never touches the
    programmer's command line.

  The log shows which user number a Write or Delete ran as.

## Eos user line
The line under the track line shows the Eos IP, the user the commands run as, and that user's mode
(`Eos 10.101.100.101 · user 1 (local) · BLIND`, LIVE in yellow, BLIND in blue). The window checks it every 5 s
while it's idle and visible, which opens the Eos connection as soon as the window opens. If Eos can't be reached it
shows **not connected** in red (hover for why) and checks again after 30 s, since connecting to an IP with nothing on
it holds Designer for up to 0.75 s.

Eos has no OSC query for its user (`/eos/get/user` gets no answer), so the local user is the one Eos names when the
connection opens; a later `/eos/out/user` replaces it only if it names a user the plugin hasn't sent commands as.
The mode comes from Eos' command-line echo for that user (`/eos/out/user/<n>/cmd`).
- **Cue XX / YY / ZZ**: the fixture's parameter names as Eos shows them on the wheels. Defaults `Cue`, `Cue 2`,
  `Cue 3` match *disguise d3_Server_Timeline_Module_v14*. Write first selects the channel, reads its wheel labels back,
  and builds each address from the exact label (lower case, spaces → `_`, a leading space included: that profile's
  ` Cue` is `/param/_cue`; `/param/cue` would be read as the Cue key). If a name isn't on the channel, Write stops
  before recording and lists the names it has.

## Icon
`icon.svg` is the Plugin Launcher icon. Designer's launcher loads `icon.svg` from the plugin's folder (not in
`d3plugin.json`; found in `plugin-launcher.js`) and falls back to the generic gear if it's missing. It's drawn at
24 × 24 px on the 50 × 50 gear tile, so it's one flat colour (`#D0D5DD`, like Designer's own) on a 16 × 16 viewBox.

## Header
- **?** dumps the track's tags and notes as the REST API gives them, then what Designer's Python sees.
  Use it if a CUE tag isn't being picked up.
- **this PC / director**: which Designer the window reads the timeline from (same as Layer Tools).
- The dot is green while Designer answers.

## Notes
- The Eos connection is made from Designer's Python, which runs on Designer's main thread, so the plugin never waits
  on Eos there. One connection stays open between clicks (a new one is slow to answer for ~1.5 s), and every
  Refresh / Write / Delete / Test / Live-Blind runs as a job the window steps every 50 ms; each step holds Designer
  for a few ms (measured 27 Oct: under 6 ms, versus 0.7–4 s frozen before). Designer refuses `import threading`.
  The one wait left is connecting to an unreachable IP (gives up after 0.75 s). If Designer's Python blocks the
  `socket` module the log says so on open.
- Eos itself is the slow part: it sends about one message every 31 ms, four per cue, so a Refresh takes ~125 ms per
  cue in the list (100 cues ≈ 12 s), with Designer free the whole time. Write and Delete check only the cues they
  touched, then Refresh.
- The window talks to this machine's Designer at `127.0.0.1` (also when **director** is this machine): by name each
  request took 2 s (`localhost`) to 4 s (`<machine name>.local`), by IP 20 ms. The log shows the time per request
  on connect and warns over 500 ms; for a director on another machine, use its IP.
- Live / Blind is pressed for the console's own OSC user by number (`/eos/user/1/key/live`): on an open connection
  that has sent user-0 commands, Eos runs un-numbered messages as user 0. Write and Delete press Live for the
  plugin's own user first, since a user in Blind or Setup records nothing.
- Python 2.7, no `sys`/`traceback` imports (Designer blocks them). Edits to `__init__.py` apply when the window is
  reopened (it registers itself from this folder as `gus_ca`).
