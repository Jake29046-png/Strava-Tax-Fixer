Strava Tax Fixer - Web Version (works on iPhone, Android, any browser)
=========================================================================

This is the same distance/time/elevation/heart-rate/duration/cadence/power/
calories editor as the Windows desktop app, but running as a website
instead - so it works on your iPhone (or any phone/computer) with no app
install, no App Store, and no ongoing cost.

How it works: it loads Python itself into your browser (via a project
called Pyodide, which compiles the real CPython interpreter to
WebAssembly) and runs the exact same editing code the desktop app uses.
Your activity file never leaves your device - there's no server involved
at all once the page has loaded. Everything happens locally, in the tab.

Files:
  index.html      - the page itself
  app.js          - all the JavaScript (file handling, Pyodide setup, the
                    preview map/chart, triggering the download)
  web_bridge.py   - the JS <-> Python glue (JSON in, JSON out)
  fit_engine.py   - the FIT read/edit/write logic (same as the desktop app)
  xml_engine.py   - the TCX/GPX read/edit/write logic (same as the desktop app)
  app_icon.png    - used as the browser tab icon and the home-screen icon


STEP 1 - Put these files somewhere they can be reached over the internet
-------------------------------------------------------------------------
Pyodide requires the page to be served over http(s):// - opening
index.html directly from your file system (a file:// URL) will not work,
browsers block the necessary requests for security reasons.

The easiest free option is GitHub Pages:

  1. Create a free GitHub account if you don't have one (github.com).
  2. Create a new repository (e.g. "strava-tax-fixer"). Public is fine and
     free; the files aren't sensitive, they're just the app itself.
  3. Upload all 6 files in this folder to that repository (drag-and-drop
     works fine on github.com's web interface - use "Add file" ->
     "Upload files").
  4. In the repository's Settings tab -> Pages (left sidebar), under
     "Build and deployment", set Source to "Deploy from a branch",
     Branch to "main" (or "master"), folder "/ (root)", then Save.
  5. GitHub will give you a URL like:
       https://yourusername.github.io/strava-tax-fixer/
     It can take a minute or two to go live the first time.

Alternative: Cloudflare Pages (pages.cloudflare.com) works the same way
and is also free, if you'd rather not use GitHub.


STEP 2 - Open it on your iPhone and add it to your home screen
-------------------------------------------------------------------------
  1. Open the URL from Step 1 in Safari on your iPhone (must be Safari -
     "Add to Home Screen" only works from Safari, not Chrome/other
     browsers on iOS).
  2. Tap the Share button (square with an arrow, bottom of the screen).
  3. Tap "Add to Home Screen".
  4. Give it a name if you want, tap Add.

You'll now have an icon on your home screen that opens straight to the
app in full-screen mode, no browser address bar - it behaves like a
regular app from here on.

The first load takes a few seconds (downloading the Python runtime,
~10-15MB, one-time - your browser caches it after that, so it's instant
on reopening, even offline for everything except the map tiles in the
route preview, which do need an internet connection each time since
they're fetched from OpenStreetMap).


What it does
-------------------------------------------------------------------------
Identical behavior to the desktop app - same distance, start time,
elevation, heart rate, duration, cadence, power, and calories edits,
same safety re-validation before you can save, same "never touches your
original file" approach (it builds an edited copy and downloads that;
the file you loaded is left alone). See the desktop app's README for the
full explanation of what each field actually does to your file - it's
the exact same logic here, just running in a browser tab instead of a
Windows window.

One difference: instead of "Save As" to a folder, tapping "Save edited
copy..." triggers your browser's normal download - on iPhone that goes
to Files app (or wherever you've set Safari downloads to go), same as
downloading any other file from a website.


Limitations
-------------------------------------------------------------------------
- Needs an internet connection to load the first time (to download
  Pyodide and the app itself) and every time you use the route preview's
  map (to fetch OpenStreetMap tiles). Everything else - loading your
  file, applying edits, saving - works fully offline once the page has
  loaded once.
- Same format/field limitations as the desktop app (see its README):
  single-session files, standard message layouts only, GPX has no
  power/calories concept, etc.
- No drag-and-drop file picker on iPhone (that's a Mac/Windows-only
  browser feature) - tap the drop zone to open the normal file picker
  instead, which works fine.
