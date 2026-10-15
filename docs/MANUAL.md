# UDM manual

Everything UDM does, page by page. For what UDM is and how to install it, see the
[README](../README.md).

A small download manager for Windows. Chrome hands each download to a local Python
downloader, which fetches it in parallel range requests and can pause, resume, queue,
limit speed and keep a history. Streams (`.m3u8`, `.mpd`) are saved with ffmpeg.

```
Chrome extension  --native messaging-->  host/host.py  --http 127.0.0.1:8765-->  downloader/downloader.py
```

Works in Chrome, Brave and Edge. No outside services.
Files are saved to `%USERPROFILE%\Downloads\udm` unless you change the folder in the settings.

Two ways to install:

- **`UDM-Setup.exe`** from the releases page. No Python needed. See [Installer](#installer-udm-setupexe)
  for how to build it, and [INSTALL.txt](../INSTALL.txt) for the steps people follow.
- **From source** (below, for working on UDM): needs Python 3, no extra packages.

## Install from source

1. Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked** and pick the `extension` folder.
   Its ID is always `nlpicpbemdokbpgkigobcbobbgbmbpfa` (fixed by the `key` in `manifest.json`).
2. Register the native host (writes one key under `HKCU\Software\Google\Chrome\NativeMessagingHosts`,
   which Brave reads too):
   ```
   python host\install_host.py nlpicpbemdokbpgkigobcbobbgbmbpfa
   ```
3. Start the downloader and leave the window open:
   ```
   python downloader\downloader.py
   ```

To unregister: `python host\install_host.py --remove`.

## Auto start

Instead of step 3, let Windows start the downloader at login, in the background with no window:

```
python autostart.py on        start at login, and start it now
python autostart.py off       stop starting at login, and stop it now
python autostart.py status
```

It adds one value under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` and runs the
downloader with `pythonw.exe`. Progress goes to `%USERPROFILE%\Downloads\udm\udm.log` (each line
starts with the time; downloads are named by file name and site only, see [Privacy](#privacy)).
If you move the `udm` folder, run `off` before moving and `on` after.

## Tray icon

While the downloader runs, the hedgehog sits in the notification area near the clock (Windows may
put new icons under the `^` arrow first; drag it onto the taskbar to keep it visible).

- **Hover**: how many downloads are running, their total speed, and how many are queued.
- **Right-click**: **Open download manager**, **Pause all**, **Start all**, **Quit**.
  Double-click also opens the download manager.
- **Open download manager** starts Chrome (found through `App Paths` in the registry) with
  `chrome-extension://<id>/manager.html`; the id is read from `host\com.udm.host.json`.
- **Start all** from the tray, like the scheduler, cannot send fresh cookies; sites that need a
  login may fail, then use Retry on the manager page.
- **Quit** saves the history and stops the downloader. Downloads that were running come back as
  paused next time.

It is plain `ctypes` (`downloader\tray.py`), no extra packages. The icon is `downloader\udm.ico`
(16, 32 and 48 px, made from the extension's icons).

## Installer (UDM-Setup.exe)

`build.bat` builds `dist\UDM-Setup.exe`, one file to give to other people. They need nothing
else: no Python, no admin rights. `INSTALL.txt` (also in the installed folder) has their steps,
including the SmartScreen warning ("More info", "Run anyway").

**What it does.** Installs for the current user to `%LOCALAPPDATA%\UDM`:

```
downloader\  udm-downloader.exe (+ _internal\), ffmpeg.exe, ffprobe.exe (if present), udm.ico
host\        udm-host.exe (+ _internal\), com.udm.host.json
extension\   the extension, for Load unpacked
README.md, INSTALL.txt, ffmpeg-LICENSE.txt, steps.html, uninstall.py, unins000.exe
```

It registers the native host for Chrome, Brave and Edge (`HKCU\Software\Google\Chrome\...`,
`HKCU\Software\BraveSoftware\Brave-Browser\...`, `HKCU\Software\Microsoft\Edge\...`
`\NativeMessagingHosts\com.udm.host`), adds autostart (the `udm` value under `...\Run`, the same one
`autostart.py` uses), starts the downloader (tray icon), and adds a Start menu folder **UDM** with
**UDM folder**, **UDM readme**, **Start UDM downloader** and **Uninstall UDM**. Finish opens the
extension folder and `steps.html`, a short page in the browser with the 3 steps (open
`chrome://extensions`, turn on Developer mode, Load unpacked) and the folder to pick. The wizard
follows Windows light or dark mode.

**Updates.** Run a newer `UDM-Setup.exe` over the old install. It closes the downloader the way the
tray's Quit does (running downloads come back paused), replaces the program files, and starts the
downloader again. History, settings and downloads are in `Downloads\udm`, which setup never
touches, so they stay. The last page then says: open `chrome://extensions` and press the reload
arrow on UDM.

**Uninstall** (Windows Settings > Apps, or Start menu > UDM > Uninstall UDM) stops the downloader,
removes autostart, the three registry keys, the Start menu folder and `%LOCALAPPDATA%\UDM`. It asks
two questions, both answered **No** unless you choose otherwise:
**Also delete the files UDM downloaded?** (finished downloads in the list with their subtitles,
and the part files of unfinished ones; other files stay) and **Delete your UDM history, stats and
settings?** (`history.json`, `settings.json`, `stats.json`, `thumbs`, `udm.log`). A kept
`Downloads\udm` is picked up again by a later install. The extension itself is removed in
`chrome://extensions`.

**The extension ID is fixed** (`nlpicpbemdokbpgkigobcbobbgbmbpfa`), so the installed
`host\com.udm.host.json` can name it on every PC. It comes from the public key in `manifest.json`.
The private key (a `.pem` file) is kept by the maintainer, outside this repository and never in
the installer (`.gitignore` excludes `*.pem`). It is only needed to pack the extension as a `.crx`
or for the Chrome Web Store, which must keep the same ID. A fork that wants its own ID replaces the
`key` in `manifest.json` and the ID in `installer\com.udm.host.json`.

**Build it.** Needed only on the PC that builds:

- Python 3 with PyInstaller: `python -m pip install pyinstaller`
- Inno Setup 6: https://jrsoftware.org/isdl.php (the default install is fine; a per-user install
  in `%LOCALAPPDATA%\Programs\Inno Setup 6` is found too)
- `downloader\ffmpeg.exe` (and `ffprobe.exe` if you want it in the installer). They are not in the
  repository: get the "essentials" build from https://www.gyan.dev/ffmpeg/builds/ and copy the
  exes from its `bin` folder. If you use another build, update `downloader\ffmpeg-LICENSE.txt`.

Then, in the `udm` folder:

```
build.bat
```

It builds `build\dist\udm-downloader` (no console window) and `build\dist\udm-host` with
PyInstaller, then `dist\UDM-Setup.exe` with Inno Setup (`installer\udm.iss`); the version comes
from `extension\manifest.json`. Run it again after any change. To use another Python or Inno Setup:
`set PYTHON=C:\path\python.exe` or `set ISCC=C:\path\ISCC.exe` before `build.bat`.
`build\` and `dist\` can be deleted at any time.

The installed downloader is the same `downloader.py`. Built into an exe it finds `ffmpeg.exe`,
`udm.ico` and the host folder next to the exe, and since there is no `python.exe`,
`udm-downloader.exe -c <code>` runs the small Python windows (the After done countdown, Save As)
and the uninstaller's `uninstall.py`.

## After an update: restart the downloader and reload the extension

With `UDM-Setup.exe`, the installer does step 1 and 2; only step 3 and 4 are left.

1. Stop the downloader.
   - Started in a console window: press Ctrl+C in it (or close the window).
   - Started by autostart: `python autostart.py off`
2. Start it again: `python downloader\downloader.py`, or `python autostart.py on`.
3. Open `chrome://extensions` and press the reload arrow on the **Uniguri Download Manager** card.
   Chrome may ask to accept new permissions (right-click menu, alarms, clipboard read for clipboard watch).
4. Close and reopen any manager or settings tab that was open.

Running downloads are cut off by step 1. They come back as **paused** and continue with **Resume**.

## The pages

- **Popup** (click the toolbar icon): the videos found on the current tab (**MP3** and **Download**),
  the last 4 downloads with their buttons, **Download all links** and **Open download manager**.
  The **Private** switch at the top turns private mode on and off; while it is on it shows as a
  `PRIVATE` badge. In an incognito window an `INCOGNITO` badge says downloads from it are private.
- **Download manager** (`manager.html`, opened from the popup): every download in a table.
- **Settings** (`settings.html`): the **Settings** button on the manager page, or right-click the
  toolbar icon > **Options**.
- **Stats** (`stats.html`): the **Stats** button on the manager page.

## Download manager page

Table columns: name, size, progress bar, speed, time left, status, date added, buttons.

| Button | What it does |
| --- | --- |
| Pause / Resume | Direct files: stops the part threads, keeps the `.part` file and the progress json, continues from there. |
| Stop / Restart | Streams only. ffmpeg cannot pause, so **Stop** ends the download and **Restart** downloads it again from the beginning. |
| Cancel | Stops the job and deletes its part files. The job stays in the list as `canceled`. |
| Retry | For failed and canceled jobs. Same url, referrer and user agent, with fresh cookies from the browser. A failed direct file continues from its saved parts. |
| Refresh link | For a direct download that failed with 403, 404 or 410 after it had started (an expired link). See below. |
| Checksum / Verify | Finished files. **Checksum** calculates the SHA-256 (not automatic) and shows it under the status; **Verify** asks for the expected hash and says `matches` or `does not match`. |
| Limit | Speed limit for this one download, in KB/s. 0 = none. |
| Up / Down | Moves a queued download one place earlier or later. |
| Open file / Open folder | Opens the finished file, or shows it in Explorer. |
| Copy URL | Puts the download's url on the clipboard. |
| Delete | Asks: **Remove from list only**, or **Also delete the file from disk**. |
| Forget site | Deletes every download, stat line, thumbnail and log line from the row's site (asks first, with the counts). See [Privacy](#privacy). |

Above the table:

- **All / Downloading / Paused / Done / Failed** filters, with counts. Canceled jobs show under All.
- **Search by name**.
- **Start all / Pause all**.
- **Clear finished** (takes done and canceled jobs off the list; the files stay).
- In the header: **Private mode** (a switch; a `PRIVATE` badge next to the title while it is on),
  **Theme** (System, Light, Dark), **Stats**, **Settings**, and **?** (keyboard shortcuts).
  A private download has a `private` tag next to its name.
- **Add URL**: paste an `http` or `https` link and press Enter.
- **Downloads at once**, **Parts per download**, **Speed limit**: the same values as on the settings page.
  While the bandwidth schedule is limiting, it says so next to the speed limit (`now 500 KB/s (schedule)`).
- **When all are done**: Do nothing, Sleep, Shut down, or Close the downloader. See below.

**Refresh link.** Download links from file hosts often expire. When a direct download fails with
403, 404 or 410 after some bytes were saved, the row gets **Refresh link**. It opens the page the
download came from in a new tab. Start the download there again: the extension catches the next
request for the same file (same file name, or same size) in any tab within 10 minutes, drops the
browser's own copy, and gives the new link to the downloader. The downloader asks the new link for
its size. If it matches, the download continues from its saved parts; if not, a notification says
`link not refreshed: size does not match` and nothing changes. One refresh waits at a time.
Streams cannot be refreshed this way (they always start again); use Retry.

**Already downloaded?** Before a download starts, the downloader looks for a finished download whose
file is still on disk with the same url, or with the same file name and the same size (the size is
asked from the server only when a name matches). If there is one, nothing starts and you are asked:
**Download again** (a new copy, `name (2).ext`), **Open existing file**, or **Cancel**. The popup
and **Add URL** ask in a dialog; downloads caught from the browser and the right-click menu ask with
a notification (closing it = Cancel).

**Thumbnails.** Finished videos (`mp4 mkv avi mov webm flv wmv m4v ts mpg mpeg 3gp`) show a small
picture in the name column: a frame 3 seconds in (the first frame for a shorter video), 160 px
wide, made by ffmpeg the first time the row is shown. They are kept in the `thumbs` folder next to
`history.json` (`thumbs\<job id>.jpg`) and made again if the file changes. Delete and Clear
finished remove them.

**Keyboard** (press **?** or the **?** button for this list):

| Key | Does |
| --- | --- |
| Up / Down | Move between rows (clicking a row also selects it) |
| Space | Pause or resume the selected download |
| Delete | Delete it (asks first, as the Delete button does) |
| Enter | Open the finished file |
| Ctrl+F | Search by name |

The keys press the row's own buttons, so they do exactly what the buttons do. They do nothing
while you type in a box or while a dialog is open.

**Stats** (button in the header): total downloaded, number of files, average speed (bytes / time
spent downloading), top sites (the page a download came from, or the file's own site), and a
column per day for the last 30 days (hover or focus a column for its numbers; **Show as a table**
lists them). The stats come from `stats.json` next to `history.json`: one line per finished
download, so Clear finished and Delete do not change them. The first time this version starts,
`stats.json` is filled from the finished downloads in the history. Each top site has **Forget**
(see [Privacy](#privacy)). With **Record which site each download came from** off, the Top sites
section is not shown.

A download on a server without range support runs as one connection. It can be paused, but
Resume starts it again from the beginning.

## Queue and limits

- At most 3 downloads run at the same time (change it with **Downloads at once**). The rest
  have the status `queued, place N` and start in order when a place is free.
- **Speed limit** is for all direct downloads together; **Limit** on a row is for one download.
  Both are in KB/s, 0 = off. Streams are not limited (ffmpeg does its own downloading).
- **Parts per download** (default 8) is the most connections one download uses at the same time.
- **One file, no merge.** A direct download is written into one file, `name.part`, which gets its
  full size at the start (a full disk shows right away). Every connection writes its part at its own
  place in it, and the finished file is only renamed, so there is no merge step at the end.
  `name.progress.json` says which bytes are there. Progress saved by an older UDM (`name.part0`,
  `name.part1`, ...) is copied into the one file once, when it is resumed.
- **No separate size check.** The first request asks for the whole file. Its answer gives the size
  and says whether ranges work, and the same connection goes on downloading the first part.
- **Dynamic segmentation.** Right after that first answer the file is cut into even parts, one
  connection each. When a part finishes early, the part with the most time left (bytes left / its
  own speed) is split in half and a new connection takes the second half; several free connections
  are used at once. A part is only split when it has at least 512 KB and a while to go (1 second,
  or 6 times what the first answer took on that site: a new connection needs its own answer and
  starts slow). So tiny files use one connection. The number of connections never goes over
  **Parts per download**; the number of parts can. A resumed download whose other parts are done
  uses all its connections on what is left.
- A part whose connection breaks is tried again up to 3 times. After that the job fails with the
  reason; **Retry** continues from what is saved.

## When all are done, and the bandwidth schedule

**When all are done** (manager page) runs once, when a download ends by itself (done or failed) and
nothing else is running or queued. Pausing everything does not count. Then the choice goes back to
**Do nothing**, so it never repeats by surprise (also not after a restart).

- **Sleep** and **Shut down** first show a small window near the clock, on top of everything:
  `Shutting down in 60 s` with **Cancel** (closing the window also cancels). It follows the Windows
  light/dark setting. It is shown by the downloader, so it also works when Chrome is closed.
  Sleep uses `SetSuspendState` (Windows may hibernate instead if that is how it is set up);
  shut down is `shutdown /s /t 0`.
- **Close the downloader** saves the history and exits the downloader.

**Bandwidth schedule** (settings page, under Downloads): limit all direct downloads to, say,
500 KB/s from 09:00 to 18:00. Outside that time the normal **Speed limit** is used. The range may
pass midnight (22:00 to 06:00). Streams are not limited, as before.

## Saving files

- Files go into subfolders by type: `video`, `music`, `documents`, `programs`, `compressed`,
  and `other` for everything else. The extensions of each folder are editable on the settings
  page, folders can be added or removed, and the sorting can be turned off.
- If the name is taken, the file is saved as `name (2).ext`, `name (3).ext`. Nothing is overwritten.
- **Ask where to save** (settings page): a Windows Save As window opens before each download, with
  the suggested folder and name. Closing the window cancels the download.
- Before a direct download starts, the free disk space is checked. If it is not enough the job
  fails with `not enough disk space: 53.6 MB needed, 1.0 MB free on C:`. Streams are not checked,
  because their size is not known in advance.

## History and settings files

Both are in `%USERPROFILE%\Downloads\udm` (they stay there even if you change the download folder).

- `history.json`: every job with name, url, referrer, size, status, date added, date finished,
  save path and error. It is loaded when the downloader starts. Downloads that were cut off show
  as `paused` (streams as `stopped`) and can be resumed.
- Cookies are never written to disk. When you press Resume or Retry, the extension sends the
  browser's current cookies for that url.
- `settings.json`: written by the settings page. The downloader reads it at startup, and again
  within a few seconds if you edit it by hand. A setting missing from it (a file from an older
  version) gets its default, and the downloader writes it back to the file.
- `stats.json`: one line per finished download (day, time, size, seconds, site), for the stats page.
- `thumbs\`: the thumbnails of finished videos.
- `udm.log` (only when the downloader runs without a console, as autostart starts it): what the
  downloader did, one line each, starting with the time. Downloads are named by file name and
  site only: full urls (with their tokens) and cookies are never written.

Private downloads leave nothing in any of these files. [Privacy](#privacy) has the ways to delete
what is there.

The capture rules, clipboard watch, theme and last picked stream quality are kept by the extension
(`chrome.storage.local`), not in these files.

## Backup

Settings page, **Backup**:

- **Export** first asks what to include: **History, stats and settings**, or **Only settings**.
  It saves `udm-backup-<date>.json` through the browser (to its usual download folder). Settings and
  capture rules are always in it; with the first choice also the history (every download in the
  list, as in `history.json`) and the stats (`stats.json`). Private downloads and cookies are never
  in it.
- **Import** reads such a file and asks first, for example *Replace your history, stats, settings
  and capture rules?*, with the backup's date and what it holds. **Replace** swaps what the backup
  has; **Cancel** changes nothing. A settings only backup leaves your history and stats as they
  are. Files on disk are not touched, and downloads that were running in the backup come back as
  paused. Private mode stays as it is now (a backup never switches it on).
- Importing a history is refused while anything is running or queued (pause it or let it finish).
  The file is checked first: a file that is not a UDM backup, or a broken row or stat line in it,
  is refused with the reason and nothing is changed.
- The theme is not part of the backup.

## Privacy

Settings page, **Privacy**. Everything here deletes for real: `history.json` and `stats.json` are
rewritten without the rows, thumbnail files are deleted, and `udm.log` is rewritten without the
lines. Running, queued and paused downloads are never touched.

- **Clear data...** opens a dialog: a time range (**Last hour**, **Last 24 hours**, **Last 7 days**,
  **Last 30 days**, **All time**) and what to clear: **Download history**, **Stats**,
  **Thumbnails**, **Log file (udm.log)**, each with how many items it would remove right now.
  **Also delete the downloaded files** (off by default) deletes the files of the downloads that
  leave the history, and their subtitle files. **Clear** asks once more, with the counts, before
  anything is deleted. A download counts by when it finished (or was added), a stat line by when
  its download finished, a thumbnail by when it was made, a log line by its time. Log lines from
  older versions have no time and count as old (only **All time** takes them). A download that
  leaves the history takes its thumbnail with it.
- **Forget site** (a button on each row of the manager page, and **Forget** next to each top site
  on the stats page) deletes every download from that site (the page's or the file's site,
  subdomains included), its stat lines, thumbnails and log lines, and the log lines that name those
  downloads. It asks first, with the counts. The files on disk stay.
- **Private mode** (settings page, the popup, and the manager header; it changes right away): new
  downloads are not saved to the history, stats, thumbnails or log, and get no Windows
  notification (Windows keeps old notifications in a list). They still download as usual and show
  in the list with a `private` tag, until they finish or are canceled, or the downloader restarts.
  A private download that fails stays in the list (only in memory) so you can Retry it. When the
  downloader quits, unfinished private downloads are stopped and their part files deleted.
- **Incognito**: a download from an incognito (InPrivate) window is always private, also with
  private mode off: browser downloads, the right-click menu, and the popup's buttons. It is sent
  with the incognito window's own cookies. Chrome runs extensions in incognito only when you allow
  it (`chrome://extensions` > Uniguri Download Manager > Details > **Allow in Incognito**); without
  that, incognito downloads stay in the browser and UDM never sees them. If the downloader cannot
  take an incognito download, it is not given back to the browser (that would put it in the normal
  window's download list); a notification says it failed instead.
- **Auto clean**: **Never** (default), or after **7**, **30** or **90 days**. History, stats,
  thumbnails and log lines older than that are deleted when the downloader starts, once a day, and
  when you change the setting. Files on disk stay.
- **Record which site each download came from** (on by default): turned off, new stat lines have no
  site, and the stats page hides Top sites. Lines already there keep theirs (use Forget or Clear
  data for those).
- **The log** never has full urls or cookies: a download is named by its file name and site, like
  `job 12: report.pdf from files.example.com (page example.com)`, and any address in an error
  message is cut down to its site. Nothing at all is written about a private download.

## Settings page

| Setting | Default |
| --- | --- |
| Download folder | `%USERPROFILE%\Downloads\udm` |
| Ask where to save before each download | off |
| Sort files into subfolders by type, and the extension list of each folder | on |
| Downloads at the same time | 3 |
| Parts per download | 8 |
| Speed limit, KB/s | 0 (off) |
| Windows notification when a download finishes or fails | on |
| Bandwidth schedule: limit KB/s from a time to a time | off (500 KB/s, 09:00 to 18:00) |
| Scheduler: start the queue at a time / stop at a time | off |
| Capture rules: sites to ignore, file types, minimum size, clipboard watch | none (every download is caught), clipboard watch off |
| Theme: System, Light or Dark | System |
| Private mode (changes right away) | off |
| Record which site each download came from | on |
| Auto clean | Never |

Press **Save**. A folder that cannot be created or a wrong time is refused with a message. The
capture rules are saved too, also while the downloader is off. The theme and private mode change
right away, without Save.

**Scheduler.** At the start time every paused download is resumed (stopped streams restart).
At the stop time every running and queued download is paused. The downloader must be running at
that time. A scheduled start cannot ask the browser for fresh cookies, so sites that need a
login may fail then; use Retry.

## Capturing downloads

**Capture rules** (settings page, **Capture**) decide which browser downloads UDM takes. The others
are downloaded by the browser as usual.

| Rule | Meaning |
| --- | --- |
| Sites to ignore | One per line, like `example.com`. Matches the download's site or the page's site, and their subdomains. A pasted address is cut down to its site. |
| File types to catch | Extensions, like `zip exe mp4 pdf`. Empty = every type (the old behavior). The type comes from the file name the server sends, so links without an extension work too. |
| Minimum file size, KB | Smaller downloads stay in the browser. 0 = any size. A download whose size the server does not tell is taken. |
| Clipboard watch | Off by default. See below. |

The rules are kept by the extension (`chrome.storage.local`), not by the downloader, so they also
apply while the downloader is off (then everything goes to the browser anyway). The extension
catches a download when the browser knows its file name and size (`downloads.onDeterminingFilename`).

- **Alt+click** a link: the browser downloads it itself, once. The next normal click goes to UDM again.
- **Download all links** (popup): lists every `http`/`https` link on the current page once, with a
  checkbox, the file name and its type. Filter by **Video, Music, Documents, Compressed, Programs,
  Images** (counts on each; the extensions come from your folder lists) and by text. **Select all**
  checks the links shown; **Download selected** adds the checked ones that are shown to the queue.
  Ones already downloaded are counted and ask with a notification. A tab that was open before the
  extension was loaded must be reloaded first.
- **Clipboard watch**: when you copy an `http`/`https` link whose file type is in **File types to
  catch** (with no types there: in your folder lists), a notification asks **Download** (closing it =
  no). Links to web pages, plain text and ignored sites never ask. It works while Chrome is running:
  a hidden extension page reads the clipboard once a second, only while the option is on (the
  extension asks for the `clipboardRead` and `offscreen` permissions for this).

## Other features

- **Right-click menu**: right-click a link, image or video > **Download with UDM**. On a
  picture or video it takes the media, otherwise the link. Blob videos cannot be taken this way;
  use the popup list.
- **Notifications**: a Windows notification with the file name when a download finishes, and one
  with the error when it fails. Chrome must be running for these.
- **All control goes through the native host**, the same way as downloads. The downloader only
  answers `application/json` requests without an `Origin` header, so web pages cannot talk to it.
  The extension itself takes control messages only from its own pages, not from content scripts.

## Test

- **Downloader only:** `python downloader\downloader.py https://www.python.org/ftp/python/3.12.0/python-3.12.0-amd64.exe`
  (saved to the `programs` folder; this mode does not write history).
- **Resume:** start the same command, press Ctrl+C part-way, run it again. It continues from the `.part` file and its progress json.
- **Whole chain:** with the downloader running, click any download link in Chrome. The browser's
  download disappears and the download shows in the popup and on the manager page.

If the downloader is not running, the extension gives the download back to Chrome.
Hand-off errors show under `chrome://extensions` > Uniguri Download Manager > **service worker**.

For testing only, the downloader reads a few environment variables: `UDM_PORT` (another port; the
native host follows it when the browser was started with it), `UDM_DIR` (another data and download
folder), `UDM_DRYRUN=1` (After done prints the sleep or shut down command instead of running it),
and `UDM_COUNTDOWN` (seconds of the countdown, default 60). Normal use needs none of them.

## Video detection

The extension watches each tab's responses for:

- stream playlists: `.m3u8` (HLS) and `.mpd` (DASH)
- media files of 500 KB or more: content type `video/*` or `audio/*`, or a URL ending in `.mp4` or `.webm`

The icon badge shows how many it found. Click the icon for the list and press **Download**, or
**MP3** to keep only the audio. The list is cleared when the tab navigates.

### Quality, subtitles and audio only

- **Quality picker.** Pressing **Download** on an HLS stream (`.m3u8`) first reads the master
  playlist. If it has more than one quality, or subtitle tracks, a picker opens under the row:
  each quality shows its resolution, bitrate and estimated size (bitrate x length, marked `~`).
  The highest is selected the first time. Your choice is remembered and preselected next time
  (the same height, or the closest lower one; picking the top quality remembers "highest").
  A playlist with one quality and no subtitles downloads right away, as before.
- **Subtitles.** Each subtitle track of the stream has a checkbox. Picked tracks are saved next
  to the video as `<name>.<language>.vtt`, or `.srt` if you choose `.srt` in the picker. The
  WebVTT pieces of a track are joined into one file. The finished row says `2 subtitles saved`.
  Subtitles are never put inside the mp4. Only HLS subtitle tracks are read, not DASH ones.
- **Audio only.** The **MP3** button saves just the audio as an mp3 (ffmpeg, `libmp3lame`, VBR
  quality 2) into the `music` folder. For a stream ffmpeg reads only the audio. For a direct video
  file the video is downloaded first (as `name.mp3.video`, with parts, pause and resume as usual),
  converted, and then deleted.

Every download shows a progress bar with percent, size (`120.4 MB / 486.3 MB`), speed averaged over
the last 5 seconds, and time left. For streams the total length comes from `ffprobe.exe` if it sits
next to `ffmpeg.exe`, otherwise from the `#EXTINF` lines of the playlist. Total size and time left
are estimates and are marked with `~`. If the length cannot be found (a live stream) the row shows
time downloaded and speed.

**Blob videos.** Many players show `blob:...` as the video address. That address only exists inside
the browser, so it cannot be downloaded. The real stream is the `.m3u8` or `.mpd` the player loaded,
and that is what the list shows. When a page uses a blob video the popup says so. If several streams
are listed, the first is usually the master playlist with every quality.

**Streams need ffmpeg.** Direct files use the range downloader. Streams go to
`ffmpeg -c copy` (no re-encoding) with the referrer, cookies and user agent, and are saved as
`<page title>.mp4`. Given a master playlist, ffmpeg keeps the quality picked in the popup, or the
highest resolution when nothing was picked. The downloader
prints `ffmpeg not found, run: winget install ffmpeg` at startup if it is missing. It looks on
PATH, then for an `ffmpeg.exe` next to `downloader.py` (installed: next to `udm-downloader.exe`).

**Stream errors.** ffmpeg runs with its reconnect options, so a 5xx answer or a dropped connection
is tried again by ffmpeg itself for about 10 seconds. If it still fails with a 5xx or network error,
the whole stream is tried again up to 3 times, after 5, 15 and 30 seconds; the status shows
`retrying (2/3)`. 403, 404 and other errors fail right away. When all retries fail the error ends
with "the link may have expired. reload the video page, press play, and download it again from the
popup."

Try it:

- Plain mp4: open https://www.w3schools.com/html/html5_video.asp, wait for badge `1`, click the icon, **Download**.
- Blob video: open https://hlsjs.video-dev.org/demo/, wait for the badge, click the icon. The popup
  shows the blob note; press **Download** on the first row (`x36xhzz.m3u8`, about 490 MB at 1080p).
- Downloader only: `python downloader\downloader.py https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8`

Limits: DRM-protected video (Netflix, Disney+ and similar) cannot be saved, and YouTube does not
use playlists this can read.

## How it works

- `extension/background.js` listens for new downloads, applies the capture rules, cancels the
  browser's copy and sends url, referrer, cookies and user agent to the native host. It also passes
  every button press (pause, cancel, settings ...) on as one message, adds the right-click menu,
  raises the notifications, and runs Refresh link and clipboard watch.
- `extension/popup.*`, `manager.*`, `settings.*`, `stats.*` are the pages; `common.js` holds what
  they share (number formats, job buttons, dialogs). `theme.css` and `theme.js` give every page the
  same look and the theme setting. `clipboard.*` is the hidden page of clipboard watch.
  `content.js` runs in web pages: blob video detection, Alt+click, and the link list.
- `host/host.py` reads length-prefixed JSON from stdin and POSTs each message to the downloader.
  It waits up to 60 seconds for an answer (reading a stream playlist can take a while).
- `downloader/tray.py` shows the tray icon and its menu (ctypes); `downloader.py` starts it.
- `build.bat`, `installer/` build `UDM-Setup.exe`: `udm.iss` (Inno Setup script),
  `com.udm.host.json` (the installed host manifest, with the fixed ID), `steps.html` (the page
  opened at Finish), `uninstall.py` (deletes downloads and data when the uninstaller is told to).
- `downloader/downloader.py` keeps the job list, the queue, `history.json` and `settings.json`.
  `log()` writes each log line (time first, addresses cut down to their site, nothing for private
  jobs). `clear_data()` deletes by time range, kind and site; Clear data, Forget site and Auto clean
  all use it, and its dry run gives the counts the dialogs show.
  For a direct file it asks for everything from the first missing byte with a `Range` header. A `206`
  reply means ranges work: that connection goes on as the first part, the file is split into parts
  that all write into one pre-sized `name.part`, progress goes to `name.progress.json`, and the
  finished file is renamed. Otherwise it does a normal single download, reading the same reply.
  Messages it does not know are refused, so they can never start a download by accident.

## Credits

The hedgehog logo is Uniguri, a character from ZUTOMAYO (ずっと真夜中でいいのに。). All rights belong to ZUTOMAYO. This is an unofficial fan project and is not affiliated with or endorsed by ZUTOMAYO. Official channel: https://www.youtube.com/c/ZUTOMAYO
